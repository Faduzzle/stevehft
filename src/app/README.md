# App Module Guide

## Purpose

`src/app/` assembles the system into a runnable program.

This folder should contain runtime wiring, not business logic.

For the remaining pre-live startup, dry-run, and live-smoke checklist, see
[PRE_LIVE_CHECKLIST.md](/home/faduzzle/projects/stevehft/PRE_LIVE_CHECKLIST.md).

For manual recovery and thread-ownership rules, see
[OPERATIONS_RUNBOOK.md](/home/faduzzle/projects/stevehft/src/app/OPERATIONS_RUNBOOK.md).

For a repeatable post-run review template, see
[POST_RUN_REVIEW_TEMPLATE.md](/home/faduzzle/projects/stevehft/src/app/POST_RUN_REVIEW_TEMPLATE.md).

## Planned Files

### `main.py`

Responsibilities:

- own the long-lived `AppRuntime` object: lifecycle (`start`/`stop`/`attach_*`)
  and the run loop that drives it every cycle
- coordinate startup, warmup, trading enable, flatten, and shutdown
- start session telemetry before trading threads begin
- stop and flush telemetry during shutdown

Current implementation:

- `AppRuntime`
- `AppLoopStats`
- `poll_once(...)` style runtime refresh through `AppRuntime.poll_once()`
- `run_strategy_once(...)` for target generation, reconciliation, and optional routing
- `control_cycle_once(...)` for one integrated market-data plus strategy cycle
- repeated runtime refresh through `AppRuntime.run_cycles(...)`
- continuous runtime refresh through `AppRuntime.run_until_stopped(...)`
- event-driven continuous runtime through `AppRuntime.run_event_driven_until_stopped(...)`
- market-data producer thread lifecycle through `start_market_data_stream()` and `stop_market_data_stream()`
- `attach_default_market_maker(...)` for a prewired market maker that sees both market state and execution state
- runtime initialization for:
  - assembled `TradingRuntimeStack`
  - `OrderLedger`
  - `PortfolioLedger`
  - `RiskLimits`
  - `KillSwitchController`

### `bootstrap.py`

Responsibilities: the one-shot startup sequence — everything `main.py` does
NOT own, because it runs once at process start rather than every cycle.

- load config
- create the SHIFT session
- initialize state objects
- start worker threads

Current implementation:

- `build_runtime(...)`
- `create_shift_trader(...)`
- `resolve_shift_order_book_type(...)`
- `bootstrap_once(...)`
- `run_bootstrap_once(...)`
  - `OrderRouter`
  - `Reconciler`

This file is where the full system graph comes together.

### `live_smoke.py`

Responsibilities:

- build a runtime config for a broker-facing smoke session
- bootstrap `AppRuntime`
- attach the default market maker
- run a bounded number of control cycles
- optionally route live orders
- always disconnect and flush telemetry on exit

Default workflow:

1. run dry-run mode first with no live order routing
2. inspect telemetry and strategy traces
3. rerun with `--execute-orders` only after the broker state and quote logic look sane

Every smoke run now prints a preflight report first and refuses to start if config,
timing, symbols, or output paths are unsafe or invalid.

Example dry-run:

```bash
python3 run.py \
  --cycles 20 \
  --update-interval-ms 50 \
  --session-dir runs/live_smoke_dry
```

Example live-order smoke:

```bash
SHIFT_USERNAME="your_user" SHIFT_PASSWORD="your_password" \
python3 run.py \
  --cycles 20 \
  --update-interval-ms 50 \
  --session-dir runs/live_smoke_orders \
  --execute-orders
```

`--symbols` is optional. If omitted, the runtime asks `trader.get_stock_list()`
for the tradable universe after connecting and subscribes that list.

## Startup Sequence

1. Load config and credentials.
2. Connect to SHIFT.
3. Select symbols.
4. Create session logger and start telemetry.
5. Subscribe market data.
6. Warm state.
7. Start threads.
8. Enable trading only after health checks pass.

The current bootstrap target is smaller on purpose:

1. start telemetry
2. create `shift.Trader`
3. connect
4. subscribe symbols
5. run one market-data cache refresh
6. build execution and reconciliation stack
7. run one reconciliation poll
8. verify clean shutdown

After bootstrap, `AppRuntime.poll_once()` is the current single-pass integration point:

1. refresh market data
2. update cached `MarketState`
3. poll broker reconciliation
4. update safe-mode state

`AppRuntime.run_until_stopped(...)` is the current continuous integration loop:

1. call `poll_once()`
2. call `run_strategy_once()`
3. sleep to maintain the configured market-data cadence
4. continue until the app-level stop signal is set

`AppRuntime.run_event_driven_until_stopped(...)` is the first threaded handoff path:

1. start the market-data producer thread
2. producer refreshes `MarketState` and publishes `MarketDataUpdateEvent` into an
   SPSC queue after each cycle
3. strategy/execution waits on queue events instead of fixed sleeping
4. on every market-data event, poll broker reconciliation and run one strategy
   decision pass
5. if no market-data event arrives but live risk exists, keep polling
   reconciliation on timeout as a safety path
6. stop the producer thread and flush telemetry on shutdown

## Shutdown Sequence

1. Disable new trading.
2. Cancel passive orders.
3. Flatten inventory.
4. Log shutdown intent.
5. Flush telemetry.
6. Disconnect cleanly.

## Concurrency Guidance

`src/app/` should be the only layer that knows about all threads at once.

That keeps:

- thread lifecycle management centralized
- stop signals easier to reason about
- shutdown behavior consistent

## Context For Future Work

If we need to understand how the process starts, stops, and changes operating mode, look here first.
