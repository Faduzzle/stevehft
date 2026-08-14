from __future__ import annotations

import argparse
import os
import threading
from datetime import time as LocalTime
from pathlib import Path
from typing import Callable, Sequence

from src.app.bootstrap import (
    bootstrap_once,
    build_runtime,
    create_shift_trader,
    resolve_shift_order_book_type,
)
from src.app.main import AppRuntime
from src.app.preflight import run_preflight_checks
from src.core.session import TraderLike
from src.core.config import (
    DashboardConfig,
    MarketDataConfig,
    RiskConfig,
    RuntimeConfig,
    StrategyConfig,
    TelemetryConfig,
)
from src.data.book_cache import OrderBookTypeLike


def _load_default_credentials() -> tuple[str, str]:
    try:
        from credentials import my_password, my_username
    except Exception:
        return "", ""
    return my_username, my_password


def build_live_smoke_config(
    *,
    username: str,
    password: str,
    initiator_cfg: str | Path,
    symbols: Sequence[str],
    tick_size: float = 0.01,
    session_dir: str | Path = "runs/deployment",
    update_interval_ms: int = 100,
    reconcile_interval_ms: int = 250,
    max_position_lots_per_symbol: int = 5,
    max_gross_position_lots: int = 0,
    buying_power_reserve_fraction: float = 0.025,
    book_depth_levels: int = 5,
    enable_dashboard: bool = False,
    dashboard_redraw_interval_ms: int = 250,
    subscribe_all_books: bool = False,
    min_trades_per_day: int = 200,
    target_trades_per_day: int | None = None,
    session_timezone: str = "America/New_York",
    session_open_local: LocalTime = LocalTime(9, 30),
    session_close_local: LocalTime = LocalTime(16, 0),
) -> RuntimeConfig:
    if target_trades_per_day is not None:
        min_trades_per_day = target_trades_per_day
    config = RuntimeConfig(
        username=username,
        password=password,
        initiator_cfg=Path(initiator_cfg),
        telemetry=TelemetryConfig(session_dir=Path(session_dir), enable_event_logging=True),
        dashboard=DashboardConfig(
            enabled=enable_dashboard,
            redraw_interval_ms=dashboard_redraw_interval_ms,
        ),
        market_data=MarketDataConfig(
            symbols=tuple(symbols),
            book_depth_levels=book_depth_levels,
            update_interval_ms=update_interval_ms,
            subscribe_all_books=subscribe_all_books,
        ),
        risk=RiskConfig(
            max_position_lots_per_symbol=max_position_lots_per_symbol,
            max_gross_position_lots=max_gross_position_lots,
            buying_power_reserve_fraction=buying_power_reserve_fraction,
            reconcile_interval_ms=reconcile_interval_ms,
        ),
        strategy=StrategyConfig(
            tick_size=tick_size,
            min_trades_per_day=min_trades_per_day,
            session_timezone=session_timezone,
            session_open_local=session_open_local,
            session_close_local=session_close_local,
        ),
    )
    config.validate()
    return config


def run_live_shift_smoke(
    runtime_config: RuntimeConfig,
    *,
    cycles: int = 20,
    execute_orders: bool = False,
    skip_preflight: bool = False,
    trader_factory: Callable[[str], TraderLike] = create_shift_trader,
    order_book_type: OrderBookTypeLike | None = None,
) -> AppRuntime:
    if cycles <= 0:
        raise ValueError("cycles must be positive")

    if not skip_preflight:
        preflight = run_preflight_checks(
            runtime_config,
            execute_orders=execute_orders,
        )
        if not preflight.passed:
            raise ValueError(preflight.as_text())

    runtime = build_runtime(runtime_config)
    resolved_order_book_type = order_book_type or resolve_shift_order_book_type()
    try:
        bootstrap_once(
            runtime,
            trader_factory=trader_factory,
            order_book_type=resolved_order_book_type,
        )
        runtime.attach_default_market_maker()
        runtime.run_cycles(cycles, execute_orders=execute_orders)
        return runtime
    finally:
        runtime.stop()


def run_live_shift_smoke_until_stopped(
    runtime_config: RuntimeConfig,
    stop_signal: threading.Event,
    *,
    execute_orders: bool = False,
    skip_preflight: bool = False,
    trader_factory: Callable[[str], TraderLike] = create_shift_trader,
    order_book_type: OrderBookTypeLike | None = None,
) -> AppRuntime:
    if not skip_preflight:
        preflight = run_preflight_checks(
            runtime_config,
            execute_orders=execute_orders,
        )
        if not preflight.passed:
            raise ValueError(preflight.as_text())

    runtime = build_runtime(runtime_config)
    resolved_order_book_type = order_book_type or resolve_shift_order_book_type()
    try:
        bootstrap_once(
            runtime,
            trader_factory=trader_factory,
            order_book_type=resolved_order_book_type,
        )
        runtime.attach_default_market_maker()
        runtime.run_event_driven_until_stopped(
            stop_signal,
            execute_orders=execute_orders,
        )
        return runtime
    finally:
        runtime.stop()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    default_username, default_password = _load_default_credentials()
    parser = argparse.ArgumentParser(
        description="Run a broker-connected SHIFT deployment session through AppRuntime.",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("SHIFT_USERNAME", default_username),
        help="SHIFT username. Defaults to SHIFT_USERNAME, then credentials.my_username.",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("SHIFT_PASSWORD", default_password),
        help="SHIFT password. Defaults to SHIFT_PASSWORD, then credentials.my_password.",
    )
    parser.add_argument(
        "--initiator-cfg",
        default=os.getenv("SHIFT_INITIATOR_CFG", "initiator.cfg"),
        help="Path to SHIFT initiator config.",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=(),
        help=(
            "Symbols to subscribe and quote. Omit to discover all tradable "
            "symbols from trader.get_stock_list() after connecting."
        ),
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help=(
            "Number of control cycles to run. Omit to stay connected and run "
            "until Ctrl-C."
        ),
    )
    parser.add_argument(
        "--update-interval-ms",
        type=int,
        default=100,
        help="Fast market-data/strategy cadence in milliseconds.",
    )
    parser.add_argument(
        "--reconcile-interval-ms",
        type=int,
        default=250,
        help="Broker waiting-list/execution/portfolio poll cadence in milliseconds.",
    )
    parser.add_argument(
        "--max-position-lots-per-symbol",
        type=int,
        default=5,
        help="Hard max absolute position per symbol in lots.",
    )
    parser.add_argument(
        "--max-gross-position-lots",
        type=int,
        default=0,
        help=(
            "Hard max gross symbol exposure budget across all symbols in lots. "
            "Set <=0 to disable the static gross cap and rely on BP reserve."
        ),
    )
    parser.add_argument(
        "--buying-power-reserve-fraction",
        type=float,
        default=0.025,
        help="Fraction of reported BP to keep unused as a safety buffer.",
    )
    parser.add_argument(
        "--book-depth-levels",
        type=int,
        default=5,
        help="Number of book levels to cache per side.",
    )
    parser.add_argument(
        "--session-dir",
        default="runs/deployment",
        help="Telemetry output directory.",
    )
    parser.add_argument(
        "--enable-dashboard",
        action="store_true",
        help="Enable the live ANSI terminal dashboard.",
    )
    parser.add_argument(
        "--dashboard-redraw-interval-ms",
        type=int,
        default=250,
        help="Minimum terminal dashboard redraw interval in milliseconds.",
    )
    parser.add_argument(
        "--min-trades-per-day",
        type=int,
        default=200,
        help=(
            "Minimum daily trade-count floor used for one-sided pace catch-up. "
            "The strategy speeds up when behind and does not slow down when ahead."
        ),
    )
    parser.add_argument(
        "--target-trades-per-day",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--tick-size",
        type=float,
        default=0.01,
        help="Minimum price increment used by strategy and execution accounting.",
    )
    parser.add_argument(
        "--subscribe-all-books",
        action="store_true",
        help="Subscribe all available books instead of only requested symbols.",
    )
    parser.add_argument(
        "--execute-orders",
        action="store_true",
        help="Send live broker orders. Omit to keep routing disabled.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    runtime_config = build_live_smoke_config(
        username=args.username,
        password=args.password,
        initiator_cfg=args.initiator_cfg,
        symbols=args.symbols,
        session_dir=args.session_dir,
        tick_size=args.tick_size,
        update_interval_ms=args.update_interval_ms,
        reconcile_interval_ms=args.reconcile_interval_ms,
        max_position_lots_per_symbol=args.max_position_lots_per_symbol,
        max_gross_position_lots=args.max_gross_position_lots,
        buying_power_reserve_fraction=args.buying_power_reserve_fraction,
        book_depth_levels=args.book_depth_levels,
        enable_dashboard=args.enable_dashboard,
        dashboard_redraw_interval_ms=args.dashboard_redraw_interval_ms,
        subscribe_all_books=args.subscribe_all_books,
        min_trades_per_day=args.min_trades_per_day,
        target_trades_per_day=args.target_trades_per_day,
    )
    preflight = run_preflight_checks(
        runtime_config,
        execute_orders=args.execute_orders,
    )
    print(preflight.as_text())
    if not preflight.passed:
        return 2
    if args.cycles is None:
        stop_signal = threading.Event()
        try:
            run_live_shift_smoke_until_stopped(
                runtime_config,
                stop_signal,
                execute_orders=args.execute_orders,
            )
        except KeyboardInterrupt:
            stop_signal.set()
            return 0
    else:
        run_live_shift_smoke(
            runtime_config,
            cycles=args.cycles,
            execute_orders=args.execute_orders,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
