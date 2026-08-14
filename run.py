from __future__ import annotations

import os
import threading
from datetime import time as LocalTime

from src.app.live_smoke import (
    _load_default_credentials,
    build_live_smoke_config as build_deployment_config,
    run_live_shift_smoke_until_stopped as run_live_deployment_until_stopped,
)
from src.app.preflight import run_preflight_checks


# Live deployment launcher.
# This entrypoint is intentionally live-order only and fails fast on missing
# credentials/config instead of falling back to smoke-test behavior.
EXECUTE_ORDERS = True

# Redraws a live in-terminal account + per-symbol dashboard.
ENABLE_DASHBOARD = True
DASHBOARD_REDRAW_INTERVAL_MS = 250

# Leave SYMBOLS empty to pull the tradable universe from trader.get_stock_list().
SYMBOLS: tuple[str, ...] = ()
SUBSCRIBE_ALL_BOOKS = True

INITIATOR_CFG = "initiator.cfg"
SESSION_DIR = "runs/deployment"
BOOK_DEPTH_LEVELS = 5

# Price grid size used for quote rounding, price-drift checks, and slippage-in-ticks
# accounting. Keep 0.01 for standard US equity penny ticks unless SHIFT symbols use
# a different increment.
TICK_SIZE = 0.01

# Fast market-data + strategy cadence in milliseconds.
UPDATE_INTERVAL_MS = 150

# Slower broker waiting-list/execution/portfolio poll cadence in milliseconds.
# Keep this slower than UPDATE_INTERVAL_MS so quote decisions can react to books
# quickly without hammering broker reconciliation every pass.
RECONCILE_INTERVAL_MS = 300

# Hard risk rails.
MAX_POSITION_LOTS_PER_SYMBOL = 5
# Set MAX_GROSS_POSITION_LOTS <= 0 to disable the static gross-lot cap and let
# buying-power reserve + per-symbol caps determine deployment.
MAX_GROSS_POSITION_LOTS = 0

# Keep this fraction of broker-reported BP unused as a safety buffer so noisy BP
# marks do not push us into negative buying power.
BUYING_POWER_RESERVE_FRACTION = 0.025

# Minimum daily trade-count floor. The strategy speeds up only when behind this
# pace and does not slow down when already ahead.
MIN_TRADES_PER_DAY = 200

# SHIFT sim clock is currently reporting pre-regular-hours timestamps, so keep
# the session gate wide-open and let explicit flatten/risk logic handle exits.
SESSION_TIMEZONE = "America/New_York"
SESSION_OPEN_LOCAL = LocalTime(0, 0)
SESSION_CLOSE_LOCAL = LocalTime(23, 59)


def _load_deployment_credentials() -> tuple[str, str]:
    username = os.getenv("SHIFT_USERNAME", "").strip()
    password = os.getenv("SHIFT_PASSWORD", "")
    if not username or not password:
        fallback_username, fallback_password = _load_default_credentials()
        username = username or fallback_username.strip()
        password = password or fallback_password
    if not username:
        raise RuntimeError(
            "missing SHIFT username; set SHIFT_USERNAME or credentials.my_username"
        )
    if not password:
        raise RuntimeError(
            "missing SHIFT password; set SHIFT_PASSWORD or credentials.my_password"
        )
    return username, password


def main() -> int:
    username, password = _load_deployment_credentials()
    runtime_config = build_deployment_config(
        username=username,
        password=password,
        initiator_cfg=INITIATOR_CFG,
        symbols=SYMBOLS,
        tick_size=TICK_SIZE,
        session_dir=SESSION_DIR,
        update_interval_ms=UPDATE_INTERVAL_MS,
        reconcile_interval_ms=RECONCILE_INTERVAL_MS,
        max_position_lots_per_symbol=MAX_POSITION_LOTS_PER_SYMBOL,
        max_gross_position_lots=MAX_GROSS_POSITION_LOTS,
        buying_power_reserve_fraction=BUYING_POWER_RESERVE_FRACTION,
        book_depth_levels=BOOK_DEPTH_LEVELS,
        enable_dashboard=ENABLE_DASHBOARD,
        dashboard_redraw_interval_ms=DASHBOARD_REDRAW_INTERVAL_MS,
        subscribe_all_books=SUBSCRIBE_ALL_BOOKS,
        min_trades_per_day=MIN_TRADES_PER_DAY,
        session_timezone=SESSION_TIMEZONE,
        session_open_local=SESSION_OPEN_LOCAL,
        session_close_local=SESSION_CLOSE_LOCAL,
    )

    preflight = run_preflight_checks(
        runtime_config,
        execute_orders=EXECUTE_ORDERS,
    )
    print(preflight.as_text())
    if not preflight.passed:
        return 2

    stop_signal = threading.Event()
    try:
        run_live_deployment_until_stopped(
            runtime_config,
            stop_signal,
            execute_orders=EXECUTE_ORDERS,
            skip_preflight=True,
        )
    except KeyboardInterrupt:
        stop_signal.set()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
