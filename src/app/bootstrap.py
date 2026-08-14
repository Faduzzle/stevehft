"""Startup wiring: connect to the broker, build the market-data and trading
stacks, and hand back a running `AppRuntime`.

Kept separate from `main.py`'s `AppRuntime` (lifecycle + run loop) because
this module is one-shot dependency wiring — it runs once at process start
and never again — while `AppRuntime` is the long-lived object the run loop
drives every cycle.
"""

from __future__ import annotations

from typing import Any, Callable

from src.app.dashboard import TerminalDashboard, TerminalDashboardConfig
from src.app.main import AppConfig, AppRuntime
from src.core.concurrency import SpscRingBuffer
from src.core.config import RuntimeConfig
from src.core.session import ThreadSafeTraderProxy, TraderLike, build_shift_session
from src.core.session_clock import SessionClock
from src.data.book_cache import BookCache, OrderBookTypeLike
from src.data.market_data import MarketDataLoop, MarketDataLoopConfig, MarketDataUpdateEvent
from src.data.state import MarketState
from src.risk.kill_switch import SafeMode


def create_shift_trader(username: str) -> Any:
    try:
        import shift  # type: ignore
    except ImportError as exc:
        raise RuntimeError("SHIFT Python package is not installed") from exc
    return shift.Trader(username)


def resolve_shift_order_book_type() -> OrderBookTypeLike:
    try:
        import shift  # type: ignore
    except ImportError as exc:
        raise RuntimeError("SHIFT Python package is not installed") from exc

    order_book_type = getattr(shift, "OrderBookType", None)
    if order_book_type is None:
        raise RuntimeError("SHIFT OrderBookType is unavailable")
    return order_book_type


def build_runtime(
    runtime_config: RuntimeConfig,
) -> AppRuntime:
    runtime_config.validate()
    return AppRuntime(
        config=AppConfig(runtime=runtime_config),
        session_clock=SessionClock(
            runtime_config.strategy,
            runtime_config.risk,
        ),
        dashboard=TerminalDashboard(
            config=TerminalDashboardConfig(
                enabled=runtime_config.dashboard.enabled,
                redraw_min_interval_ms=runtime_config.dashboard.redraw_interval_ms,
            ),
        ),
    )


def bootstrap_once(
    runtime: AppRuntime,
    *,
    trader_factory: Callable[[str], TraderLike] = create_shift_trader,
    order_book_type: OrderBookTypeLike | None = None,
) -> MarketState:
    if not runtime.started:
        runtime.start()

    config = runtime.config.runtime
    trader = ThreadSafeTraderProxy(trader_factory(config.username))
    session = build_shift_session(
        trader,
        username=config.username,
        initiator_cfg=config.initiator_cfg,
        event_logger=runtime.event_logger,
    )
    connected = session.connect(config.password)
    if not connected:
        raise RuntimeError("failed to connect to SHIFT")

    runtime.attach_session(session)
    try:
        runtime.initialize_trading_state()

        if not config.market_data.symbols:
            discovered_symbols = session.discover_symbols()
            if not discovered_symbols:
                raise RuntimeError("trader.get_stock_list() returned no tradable symbols")
            config.market_data.symbols = discovered_symbols

        session.subscribe_symbols(
            config.market_data.symbols,
            subscribe_all_books=config.market_data.subscribe_all_books,
        )

        market_state = MarketState()
        market_data_events = SpscRingBuffer[MarketDataUpdateEvent](capacity=1024)
        book_cache = BookCache(
            market_state,
            order_book_type=order_book_type,
            order_ledger=(
                runtime.trading.order_ledger if runtime.trading is not None else None
            ),
            event_logger=runtime.event_logger,
        )
        market_data_loop = MarketDataLoop(
            trader,
            book_cache,
            MarketDataLoopConfig(
                symbols=config.market_data.symbols,
                max_depth=config.market_data.book_depth_levels,
                update_interval_ms=config.market_data.update_interval_ms,
            ),
            update_events=market_data_events,
            event_logger=runtime.event_logger,
            before_refresh=lambda: session.ensure_connected_and_subscribed(
                config.password,
            ),
        )
        runtime.attach_market_data(
            book_cache=book_cache,
            market_data_loop=market_data_loop,
            market_data_events=market_data_events,
        )
        market_state = runtime._poll_once(force_reconcile=True)
        if runtime.trading is not None:
            baseline_offsets_shares = (
                runtime.trading.order_ledger.apply_position_baseline(
                    runtime.trading.portfolio_ledger.positions,
                )
            )
            if baseline_offsets_shares:
                runtime.event_logger.log(
                    "startup_position_baseline_applied",
                    baseline_offsets_shares=baseline_offsets_shares,
                )
            _ensure_startup_reconciled(runtime, max_attempts=3)
            if runtime.market_state is not None:
                market_state = runtime.market_state
    except Exception as exc:
        runtime.event_logger.log(
            "bootstrap_failed",
            error=repr(exc),
            connected=session.status.connected,
            subscribed_symbols=session.status.subscribed_symbols,
        )
        try:
            session.disconnect()
        finally:
            runtime.session = None
            runtime.book_cache = None
            runtime.market_data_loop = None
            runtime.market_data_events = None
            runtime.market_state = None
            runtime.trading = None
        raise

    runtime.event_logger.log(
        "bootstrap_complete",
        symbols=config.market_data.symbols,
        connected=session.status.connected,
        subscribed_symbols=session.status.subscribed_symbols,
        trading_stack_ready=runtime.trading is not None,
    )
    return market_state


def _ensure_startup_reconciled(
    runtime: AppRuntime,
    *,
    max_attempts: int,
) -> None:
    if runtime.trading is None:
        raise RuntimeError("trading state is unavailable during startup reconciliation")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")

    min_normal_polls = min(2, max_attempts)
    normal_polls = 0
    for attempt in range(1, max_attempts + 1):
        runtime._poll_once(force_reconcile=True)
        mode = runtime.trading.kill_switch.mode
        if mode == SafeMode.NORMAL:
            normal_polls += 1
        else:
            normal_polls = 0
        if normal_polls >= min_normal_polls:
            runtime.event_logger.log(
                "startup_reconciliation_ready",
                attempts=attempt,
                safe_mode=mode.value,
                normal_polls=normal_polls,
            )
            return
        runtime.event_logger.log(
            "startup_reconciliation_wait",
            attempts=attempt,
            safe_mode=mode.value,
        )
        if mode == SafeMode.KILL_SWITCH or attempt == max_attempts:
            raise RuntimeError(
                "startup reconciliation did not reach normal mode "
                f"after {attempt} attempt(s): {mode.value}"
            )


def run_bootstrap_once(
    runtime_config: RuntimeConfig,
    *,
    trader_factory: Callable[[str], TraderLike] = create_shift_trader,
    order_book_type: OrderBookTypeLike | None = None,
    stop_after_bootstrap: bool = True,
) -> MarketState:
    runtime = build_runtime(runtime_config)
    resolved_order_book_type = order_book_type or resolve_shift_order_book_type()
    try:
        return bootstrap_once(
            runtime,
            trader_factory=trader_factory,
            order_book_type=resolved_order_book_type,
        )
    finally:
        if stop_after_bootstrap:
            runtime.stop()
