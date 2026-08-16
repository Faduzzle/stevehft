# Operations And Thread-Ownership Runbook

## Purpose

This document captures the runtime ownership rules and manual recovery steps that
we should follow during the first live SHIFT smoke runs.

It is intentionally practical:

- who owns each mutable object
- what must be immutable after crossing a queue boundary
- how to shut down or recover safely if we enter `kill_switch`

Operations uses the dashboard and telemetry as a control surface.
The operator must compare current PnL with inventory, reconciliation health, latency, fill quality, safe mode, and model diagnostics.
A positive PnL value does not clear a control failure.

See also:

- [PRE_LIVE_CHECKLIST.md](/home/faduzzle/projects/stevehft/PRE_LIVE_CHECKLIST.md)
- [EDGE_CASES.md](/home/faduzzle/projects/stevehft/EDGE_CASES.md)

## Thread And State Ownership

### Market-Data Producer Thread

Owner:

- `MarketDataLoop`
- `BookCache`
- the producer-side mutable `MarketState` object stored inside `BookCache`

Responsibilities:

- poll SHIFT best-price and order-book snapshots
- update the producer-side `BookCache.market_state`
- publish a cloned `MarketDataUpdateEvent.market_state` snapshot into the SPSC queue

Rules:

- never enqueue the mutable producer-owned `BookCache.market_state` object directly
- never mutate a `MarketDataUpdateEvent.market_state` snapshot after `push_overwrite_oldest(...)`
- do not touch `OrderLedger`, `PortfolioLedger`, `RiskLimits`, or strategy state from
  the market-data producer thread

### Strategy / Execution Consumer Thread

Owner:

- `AppRuntime.market_state` after consuming a `MarketDataUpdateEvent`
- `StrategyEngine`
- `OrderLedger`
- `PortfolioLedger`
- `Reconciler`
- `OrderRouter`
- `KillSwitchController`
- `RiskLimits`

Responsibilities:

- consume SPSC market-data snapshots
- poll broker waiting-list / executions / portfolio state
- run strategy target generation
- build reconciliation actions
- submit/cancel/flatten through `OrderRouter`
- maintain safe-mode state

Rules:

- only the strategy/execution thread should mutate execution and risk ledgers
- strategy traces and order commands handed to telemetry should be treated as
  immutable after `event_logger.log(...)`
- if no market-data event arrives but live risk exists, keep polling reconciliation
  on timeout so stale orders and fills are still handled

### Telemetry Writer Thread

Owner:

- `JsonlEventLogger` queue consumer
- `events.jsonl` file appends

Responsibilities:

- serialize event payload snapshots
- flush telemetry to disk

Rules:

- all hot-path code must call `event_logger.log(...)` with data it will not rely on
  mutating afterward
- `JsonlEventLogger.log(...)` snapshots payloads into JSON-safe structures on the
  caller thread, so queued telemetry should not alias live mutable strategy/order state
- shutdown should call `SessionTelemetry.stop()` so the writer thread can drain and
  flush

## Queue Boundary Contract

Only immutable or effectively immutable snapshots should cross async queues.

Current queue payloads:

- `MarketDataUpdateEvent` carries a cloned `MarketState`
- `LogEvent` carries a detached JSON-safe payload snapshot

If we add more queues later, the same rule applies:

- producer mutates local state
- producer publishes a detached snapshot or command object
- consumer reads that detached payload
- neither side mutates the queued payload after enqueue

## Manual `kill_switch` Recovery Procedure

Use this when the system enters `kill_switch`, or when broker state is uncertain and
we want a controlled restart.

### 1. Stop Live Trading Loops

- trigger the app stop signal if running `run_until_stopped(...)` or
  `run_event_driven_until_stopped(...)`
- call `runtime.stop()` if the process is still inside Python control
- wait for:
  - market-data thread stop
  - telemetry flush
  - broker disconnect

### 2. Inspect Broker-Authoritative State

Use SHIFT UI/API to inspect:

- current waiting orders
- recent executions
- current long/short inventory by symbol
- buying power

Do not trust the strategy's intended state if broker and local views disagree.

### 3. Manually Neutralize If Needed

If the broker still shows live exposure:

- cancel remaining passive orders first
- if inventory remains, flatten manually with controlled market or aggressive limit
  orders
- verify the broker portfolio is flat or intentionally parked at a known small
  residual position

### 4. Review Telemetry

Inspect the latest `events.jsonl` session for:

- `safe_mode_transition`
- `execution_sync_failed`
- `waiting_order_parse_failed`
- `executed_order_parse_failed`
- `order_blocked_safe_mode`
- `position_update`
- `portfolio_snapshot`

Confirm why the system escalated before restarting.

## Session Log Separation

`JsonlEventLogger` appends to `events.jsonl` if that file already exists.

Operational rule:

- use a fresh `session_dir` for each dry run or live smoke if you want immutable
  per-run logs
- if you intentionally reuse a `session_dir`, preflight will warn because old and new
  events will share one append-only file
- never edit an existing `events.jsonl`; write analysis outputs to separate files

### 5. Restart With Recovery Gating

On restart:

- run dry mode first if there is any ambiguity
- bootstrap should apply the broker-position baseline if inventory exists
- startup reconciliation must reach `normal` before strategy attachment
- if it does not, stop and manually inspect broker state again

### 6. Resume With Reduced Risk

For the next live smoke after recovery:

- reduce symbol count
- reduce max position and gross caps
- keep short exposure especially conservative
- lengthen flatten buffer if the failure happened near close
- verify one clean order lifecycle before scaling back up

## Pre-Live Thread-Safety Audit Notes

Current status:

- market-data producer and strategy/execution no longer share one mutable
  `MarketState`; queued snapshots are cloned
- telemetry payloads are snapshotted before queueing
- SPSC queue operations are guarded by a condition lock and support bounded waits
- `run_until_stopped(...)`, `run_event_driven_until_stopped(...)`, and
  `MarketDataLoop.run_until_stopped(...)` all use interruptible waits when the stop
  signal exposes `wait(...)`

Remaining rule to preserve:

- if we introduce any new queue or background worker, write down its ownership
  contract here before wiring it into live order flow
