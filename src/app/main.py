from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Protocol

from src.app.dashboard import TerminalDashboard, TerminalDashboardConfig
from src.core.config import RuntimeConfig
from src.core.concurrency import SpscRingBuffer
from src.core.units import NS_PER_MS, NS_PER_S
from src.core.session import (
    ShiftSession,
    ThreadSafeTraderProxy,
    TraderLike,
    build_shift_session,
)
from src.core.session_clock import SessionClock
from src.data.book_cache import BookCache, OrderBookTypeLike
from src.data.market_data import MarketDataLoop, MarketDataLoopConfig, MarketDataUpdateEvent
from src.data.state import MarketState
from src.execution.order_state import OrderCommand, OrderIntentAction
from src.execution.runtime import TradingRuntimeStack, build_trading_runtime_stack
from src.risk.kill_switch import SafeMode
from src.strategy.base import StrategyDecisionTrace, StrategyEngine, log_strategy_trace
from src.strategy.market_maker import TopOfBookMarketMaker, TopOfBookMarketMakerConfig
from src.telemetry.logger import EventLoggerLike, NullEventLogger
from src.telemetry.metrics import build_session_metrics
from src.telemetry.recorder import SessionTelemetry, build_session_telemetry


@dataclass(slots=True)
class AppConfig:
    runtime: RuntimeConfig


@dataclass(slots=True)
class AppLoopStats:
    iterations: int = 0
    last_cycle_started_ns: int = 0
    last_cycle_completed_ns: int = 0
    last_market_data_event_seq: int = 0
    last_reconcile_poll_ns: int = 0


class StopSignalLike(Protocol):
    def is_set(self) -> bool: ...


@dataclass(slots=True)
class AppRuntime:
    config: AppConfig
    session_clock: SessionClock | None = None
    telemetry: Optional[SessionTelemetry] = None
    session: Optional[ShiftSession] = None
    book_cache: Optional[BookCache] = None
    market_data_loop: Optional[MarketDataLoop] = None
    market_data_events: Optional[SpscRingBuffer[MarketDataUpdateEvent]] = None
    market_data_thread: Optional[threading.Thread] = None
    market_data_stop_signal: Optional[threading.Event] = None
    market_state: Optional[MarketState] = None
    trading: TradingRuntimeStack | None = None
    strategy: StrategyEngine | None = None
    dashboard: TerminalDashboard = field(default_factory=TerminalDashboard)
    loop_stats: AppLoopStats = field(default_factory=AppLoopStats)
    _last_session_open: bool | None = None
    _last_broker_time: datetime | None = None
    started: bool = False

    @property
    def event_logger(self) -> EventLoggerLike:
        if self.telemetry is None:
            return NullEventLogger()
        return self.telemetry.event_logger

    def start(self) -> None:
        if self.started:
            return

        telemetry_config = self.config.runtime.telemetry
        if telemetry_config.enable_event_logging:
            self.telemetry = build_session_telemetry(
                telemetry_config.session_dir,
                flush_every=telemetry_config.logger_flush_every,
                max_queue_size=telemetry_config.logger_max_queue_size,
            )
            self.telemetry.start()

        self.started = True
        self.event_logger.log(
            "app_started",
            session_dir=str(telemetry_config.session_dir),
            event_logging_enabled=telemetry_config.enable_event_logging,
            runtime_config=self.config.runtime.summary(),
        )

    def stop(self) -> None:
        if not self.started:
            return

        self.event_logger.log(
            "app_stopping",
            session_dir=str(self.config.runtime.telemetry.session_dir),
        )

        self.stop_market_data_stream()
        self._cancel_live_orders_before_disconnect()

        if self.session is not None:
            self.session.disconnect()
            self.session = None

        if self.telemetry is not None:
            self.telemetry.stop()
            self.telemetry = None

        self.trading = None
        self.strategy = None
        self.book_cache = None
        self.market_data_loop = None
        self.market_data_events = None
        self.dashboard.reset()
        self._last_session_open = None
        self._last_broker_time = None
        self.started = False

    def attach_session(self, session: ShiftSession) -> None:
        self.session = session
        self._last_broker_time = None
        if self.session_clock is not None:
            self.session_clock.set_now_provider(
                self._broker_or_wall_clock_now,
            )

    def attach_market_state(self, market_state: MarketState) -> None:
        self.market_state = market_state

    def attach_market_data(
        self,
        *,
        book_cache: BookCache,
        market_data_loop: MarketDataLoop,
        market_data_events: SpscRingBuffer[MarketDataUpdateEvent] | None = None,
    ) -> None:
        self.book_cache = book_cache
        self.market_data_loop = market_data_loop
        self.market_data_events = market_data_events

    def initialize_trading_state(self) -> None:
        if self.session is None:
            raise RuntimeError("session must be attached before trading state is initialized")
        if self.trading is None:
            self.trading = build_trading_runtime_stack(
                self.session.trader,
                self.config.runtime,
                event_logger=self.event_logger,
            )

    def attach_strategy(self, strategy: StrategyEngine) -> None:
        self.strategy = strategy

    def attach_default_market_maker(
        self,
        config: TopOfBookMarketMakerConfig | None = None,
    ) -> TopOfBookMarketMaker:
        if self.market_state is None:
            raise RuntimeError("market state must be initialized before strategy attachment")
        if self.trading is None:
            raise RuntimeError("trading state must be initialized before strategy attachment")
        strategy = TopOfBookMarketMaker(
            self.market_state,
            config=config
            or TopOfBookMarketMakerConfig(
                tick_size=self.config.runtime.strategy.tick_size,
                max_gross_position_lots=(
                    self.config.runtime.risk.max_gross_position_lots
                ),
            ),
            order_ledger=self.trading.order_ledger,
            session_clock=self.session_clock,
            kill_switch=self.trading.kill_switch,
        )
        self.attach_strategy(strategy)
        self.event_logger.log(
            "default_strategy_attached",
            strategy_name=strategy.__class__.__name__,
        )
        return strategy

    def poll_once(self) -> MarketState:
        return self._poll_once(force_reconcile=False)

    def _poll_once(self, *, force_reconcile: bool) -> MarketState:
        if self.market_data_loop is None:
            raise RuntimeError("market data loop is not initialized")
        cycle_started_ns = time.monotonic_ns()
        self.loop_stats.last_cycle_started_ns = cycle_started_ns
        producer_market_state = self.market_data_loop.run_once()
        self.market_state = producer_market_state.clone()
        if self.strategy is not None:
            self.strategy.update_market_state(self.market_state)
        self._poll_reconcile_if_due(
            now_ns=time.monotonic_ns(),
            force=force_reconcile,
        )
        self.loop_stats.iterations += 1
        self.loop_stats.last_cycle_completed_ns = time.monotonic_ns()
        self.event_logger.log(
            "app_poll_cycle",
            iterations=self.loop_stats.iterations,
            cycle_started_ns=self.loop_stats.last_cycle_started_ns,
            cycle_completed_ns=self.loop_stats.last_cycle_completed_ns,
        )
        return self.market_state

    def _poll_reconcile_if_due(self, *, now_ns: int, force: bool = False) -> bool:
        if self.trading is None:
            return False
        interval_ns = self.config.runtime.risk.reconcile_interval_ms * NS_PER_MS
        if (
            not force
            and self.loop_stats.last_reconcile_poll_ns > 0
            and now_ns - self.loop_stats.last_reconcile_poll_ns < interval_ns
        ):
            return False
        self.trading.reconciler.poll_server_state()
        self.loop_stats.last_reconcile_poll_ns = now_ns
        return True

    def run_strategy_once(
        self,
        *,
        execute_orders: bool = True,
    ) -> tuple[list[OrderCommand], list[StrategyDecisionTrace]]:
        cycle_started_ns = time.monotonic_ns()
        if self.strategy is None or self.trading is None:
            return [], []
        session_progress = None
        if self.session_clock is not None:
            session_progress = self.session_clock.snapshot()
            if (
                session_progress.is_session_open
                and self._last_session_open is False
                and not self._has_live_risk()
            ):
                self.trading.order_ledger.reset_session_statistics()
                reset_strategy_state = getattr(
                    self.strategy,
                    "reset_adaptive_state",
                    None,
                )
                if callable(reset_strategy_state):
                    reset_strategy_state()
                self.event_logger.log(
                    "session_statistics_reset",
                    now_local=session_progress.now_local.isoformat(),
                    session_open_local=session_progress.session_open_local.isoformat(),
                )
            self._last_session_open = session_progress.is_session_open
            if (
                not session_progress.is_session_open
                and not self._has_live_risk()
            ):
                self.event_logger.log(
                    "strategy_cycle_skipped_session_closed",
                    now_local=session_progress.now_local.isoformat(),
                    session_open_local=session_progress.session_open_local.isoformat(),
                    session_close_local=session_progress.session_close_local.isoformat(),
                    minutes_to_close=session_progress.minutes_to_close,
                )
                return [], []

        targets, traces = self.strategy.generate_targets(
            portfolio_ledger=self.trading.portfolio_ledger,
        )
        targets_generated_ns = time.monotonic_ns()
        for trace in traces:
            log_strategy_trace(self.event_logger, trace)
        traces_logged_ns = time.monotonic_ns()

        reconciliation = self.trading.reconciler.build_reconciliation_actions(list(targets))
        reconciliation_built_ns = time.monotonic_ns()
        if execute_orders:
            for command in reconciliation.commands:
                self.trading.router.apply(command)
        orders_routed_ns = time.monotonic_ns()
        session_metrics = build_session_metrics(self.trading.order_ledger)
        metrics_built_ns = time.monotonic_ns()
        self.event_logger.log(
            "session_metrics",
            metrics=session_metrics,
        )
        self.event_logger.log(
            "strategy_cycle_complete",
            targets=len(targets),
            traces=len(traces),
            commands=len(reconciliation.commands),
            executed_orders=execute_orders,
            generate_targets_ms=(targets_generated_ns - cycle_started_ns)
            / NS_PER_MS,
            log_traces_ms=(traces_logged_ns - targets_generated_ns)
            / NS_PER_MS,
            build_reconciliation_ms=(
                reconciliation_built_ns - traces_logged_ns
            )
            / NS_PER_MS,
            route_orders_ms=(orders_routed_ns - reconciliation_built_ns)
            / NS_PER_MS,
            build_session_metrics_ms=(metrics_built_ns - orders_routed_ns)
            / NS_PER_MS,
            total_cycle_ms=(metrics_built_ns - cycle_started_ns)
            / NS_PER_MS,
            metrics=session_metrics,
        )
        self.dashboard.render(
            market_state=self.market_state,
            portfolio_ledger=self.trading.portfolio_ledger,
            traces=traces,
            session_metrics=session_metrics,
            session_progress=session_progress,
        )
        dashboard_rendered_ns = time.monotonic_ns()
        self.event_logger.log(
            "strategy_cycle_timing",
            dashboard_render_ms=(dashboard_rendered_ns - metrics_built_ns)
            / NS_PER_MS,
            total_with_dashboard_ms=(dashboard_rendered_ns - cycle_started_ns)
            / NS_PER_MS,
        )
        return list(reconciliation.commands), list(traces)

    def _has_live_risk(self) -> bool:
        if self.trading is None:
            return False
        for position in self.trading.portfolio_ledger.positions.values():
            if position.net_shares != 0:
                return True
        return bool(self.trading.order_ledger.live_orders())

    def control_cycle_once(
        self,
        *,
        execute_orders: bool = True,
    ) -> MarketState:
        market_state = self.poll_once()
        self.run_strategy_once(execute_orders=execute_orders)
        return market_state

    def run_cycles(self, count: int, *, execute_orders: bool = True) -> MarketState:
        if count <= 0:
            raise ValueError("count must be positive")
        interval_s = self.config.runtime.market_data.update_interval_ms / 1000.0
        market_state: MarketState | None = None
        for _ in range(count):
            cycle_start_ns = time.monotonic_ns()
            market_state = self.control_cycle_once(execute_orders=execute_orders)
            elapsed_s = (time.monotonic_ns() - cycle_start_ns) / NS_PER_S
            sleep_s = max(interval_s - elapsed_s, 0.0)
            if sleep_s > 0.0:
                time.sleep(sleep_s)
        return market_state if market_state is not None else self.market_state  # pragma: no cover

    def run_until_stopped(self, stop_signal: StopSignalLike, *, execute_orders: bool = True) -> None:
        interval_s = self.config.runtime.market_data.update_interval_ms / 1000.0
        while not stop_signal.is_set():
            cycle_start_ns = time.monotonic_ns()
            self.control_cycle_once(execute_orders=execute_orders)
            elapsed_s = (time.monotonic_ns() - cycle_start_ns) / NS_PER_S
            sleep_s = max(interval_s - elapsed_s, 0.0)
            if sleep_s > 0.0:
                self._wait_for_stop(stop_signal, sleep_s)

    def start_market_data_stream(self) -> None:
        if self.market_data_loop is None:
            raise RuntimeError("market data loop is not initialized")
        if self.market_data_thread is not None and self.market_data_thread.is_alive():
            return

        self.market_data_stop_signal = threading.Event()
        self.market_data_thread = threading.Thread(
            target=self.market_data_loop.run_until_stopped,
            args=(self.market_data_stop_signal,),
            name="market-data-loop",
            daemon=True,
        )
        self.market_data_thread.start()
        self.event_logger.log("market_data_stream_started")

    def stop_market_data_stream(self, *, join_timeout_s: float = 1.0) -> None:
        if self.market_data_stop_signal is not None:
            self.market_data_stop_signal.set()
        if self.market_data_thread is not None:
            self.market_data_thread.join(timeout=join_timeout_s)
        self.market_data_thread = None
        self.market_data_stop_signal = None

    def run_event_driven_until_stopped(
        self,
        stop_signal: StopSignalLike,
        *,
        execute_orders: bool = True,
    ) -> None:
        if self.market_data_events is None:
            raise RuntimeError("market data event queue is not initialized")
        if self.book_cache is None:
            raise RuntimeError("book cache is not initialized")

        interval_s = self.config.runtime.market_data.update_interval_ms / 1000.0
        self.start_market_data_stream()
        try:
            while not stop_signal.is_set():
                event = self.market_data_events.wait_pop(timeout_s=interval_s)
                if event is None:
                    if (
                        self.market_data_thread is not None
                        and not self.market_data_thread.is_alive()
                        and not stop_signal.is_set()
                    ):
                        self.event_logger.log(
                            "market_data_stream_dead",
                            last_event_seq=self.loop_stats.last_market_data_event_seq,
                        )
                        self.start_market_data_stream()
                    if self.trading is not None and self._has_live_risk():
                        self._poll_reconcile_if_due(now_ns=time.monotonic_ns())
                        if (
                            self.trading.kill_switch.mode != SafeMode.NORMAL
                            or self.session_clock is not None
                            and self.session_clock.snapshot().flatten_only_mode
                        ):
                            self.run_strategy_once(execute_orders=execute_orders)
                    elif self.trading is not None:
                        self.dashboard.render(
                            market_state=self.market_state,
                            portfolio_ledger=self.trading.portfolio_ledger,
                            traces=(),
                            session_metrics=build_session_metrics(
                                self.trading.order_ledger
                            ),
                        )
                    continue

                drained_events = self.market_data_events.drain()
                if drained_events:
                    event = drained_events[-1]
                    self.event_logger.log(
                        "app_market_data_backlog_drained",
                        drained_events=len(drained_events),
                        latest_event_write_seq=event.write_seq,
                        latest_event_iterations=event.iterations,
                    )

                self.market_state = event.market_state
                if self.strategy is not None:
                    self.strategy.update_market_state(event.market_state)
                self.loop_stats.iterations += 1
                self.loop_stats.last_cycle_started_ns = event.cycle_started_ns
                self.loop_stats.last_cycle_completed_ns = event.cycle_completed_ns
                self.loop_stats.last_market_data_event_seq = event.write_seq
                self._poll_reconcile_if_due(now_ns=time.monotonic_ns())
                self.run_strategy_once(execute_orders=execute_orders)
                self.event_logger.log(
                    "app_market_data_event_consumed",
                    event_write_seq=event.write_seq,
                    event_iterations=event.iterations,
                    symbols=event.symbols,
                    app_iterations=self.loop_stats.iterations,
                )
        finally:
            self.stop_market_data_stream()

    @staticmethod
    def _wait_for_stop(stop_signal: StopSignalLike, timeout_s: float) -> None:
        wait_fn = getattr(stop_signal, "wait", None)
        if callable(wait_fn):
            wait_fn(timeout_s)
            return
        time.sleep(timeout_s)

    def _broker_or_wall_clock_now(self):
        if self.session is None or self.session_clock is None:
            return datetime.now()
        try:
            broker_now = self.session.get_last_trade_time()
            self._last_broker_time = broker_now
            return broker_now
        except Exception as exc:
            if self._last_broker_time is not None:
                self.event_logger.log(
                    "session_broker_clock_cached_fallback",
                    error=repr(exc),
                    cached_broker_time=self._last_broker_time.isoformat(),
                )
                return self._last_broker_time
            wall_clock_now = datetime.now()
            self.event_logger.log(
                "session_broker_clock_fallback",
                error=repr(exc),
                wall_clock_now=wall_clock_now.isoformat(),
            )
            return wall_clock_now

    def _cancel_live_orders_before_disconnect(
        self,
        *,
        max_reconcile_attempts: int = 3,
        wait_s: float = 0.05,
    ) -> None:
        if self.trading is None:
            return

        live_orders = [
            order
            for order in self.trading.order_ledger.live_orders()
            if not order.pending_cancel
        ]
        if not live_orders:
            return

        self.event_logger.log(
            "shutdown_cancel_all_requested",
            live_orders=len(live_orders),
            order_ids=[order.order_id for order in live_orders],
        )
        for order in live_orders:
            try:
                self.trading.router.apply(
                    OrderCommand(
                        action=OrderIntentAction.CANCEL,
                        symbol=order.symbol,
                        side=order.side,
                        order_id=order.order_id,
                        reason="shutdown_cancel_all",
                    )
                )
            except Exception as exc:
                self.event_logger.log(
                    "shutdown_cancel_failed",
                    symbol=order.symbol,
                    side=order.side,
                    order_id=order.order_id,
                    error=repr(exc),
                )

        for _ in range(max(max_reconcile_attempts, 0)):
            if not self.trading.order_ledger.live_orders():
                break
            self.trading.reconciler.poll_server_state()
            time.sleep(max(wait_s, 0.0))


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
