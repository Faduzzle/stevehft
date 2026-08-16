# Telemetry Module Guide

## Purpose

`src/telemetry/` records what happened without slowing down trading.

This includes:

- structured logs
- fill records
- latency metrics
- session summaries
- fee and rebate estimates
- strategy targets and model outputs
- order commands, submissions, cancels, and replacements
- portfolio and position snapshots

Telemetry is the evidence layer for the complete decision system.
It connects market state, model output, risk decisions, broker responses, and performance metrics.
The dashboard consumes current metrics for operations, while JSONL logs support replay, calibration, incident review, and model promotion.

## Planned Files

### `logger.py`

Responsibilities:

- consume event queue messages
- batch writes
- manage log rotation or file naming if needed
- log the full audit trail from strategy output to execution outcome

This is intentionally asynchronous.

Current event stream:

- `strategy_target`
- `reconciliation_action`
- `order_command`
- `order_command_ignored`
- `order_seen_live`
- `order_state_update`
- `order_submitted`
- `order_cancel_requested`
- `order_replace_requested`
- `order_fill`
- `order_inactive`
- `position_update`
- `portfolio_snapshot`
- `server_poll_complete`

### `recorder.py`

Responsibilities:

- persist fills and session events in analysis-friendly formats
- record passive versus aggressive fills
- maintain end-of-day summaries

### `metrics.py`

Responsibilities:

- track loop latency
- track trade count pacing toward `200/day`
- estimate fees, rebates, and net economics
- publish high-level health indicators

## Concurrency Guidance

Efficient choice:

- one logger thread
- non-blocking enqueue from trading threads
- periodic batch flushes

Avoid:

- synchronous file writes from the quote loop
- full text formatting for every market-data update

## Logging Scope

The logger should make it possible to reconstruct:

1. what the strategy wanted
2. what the reconciler decided
3. what was actually sent to SHIFT
4. what the server later reported as live
5. what eventually filled
6. what positions and portfolio state resulted

That means the primary audit chain is:

`strategy_target -> reconciliation_action -> order_command -> router event -> waiting-order update -> fill -> position_update -> portfolio_snapshot`

The logger is not the source of truth for live trading state. It is the replay and audit trail.

For the exact minimum event fields to verify after each dry-run or live smoke,
see [LOG_SCHEMA.md](/home/faduzzle/projects/stevehft/src/telemetry/LOG_SCHEMA.md).

For an automated post-run sanity pass, run:

```bash
python3 -m src.telemetry.dry_run_validator runs/live_smoke_dry/events.jsonl --tick-size 0.01
```

For a compact feature and quote summary, run:

```bash
python3 -m src.telemetry.dry_run_summary runs/live_smoke_dry/events.jsonl
```

For per-ladder-level fill/slippage calibration from a numbered run log, run:

```bash
python3 -m src.telemetry.ladder_calibration runs/live_smoke/events_0012.jsonl
```

## Context For Future Work

If the system works but we cannot explain why it made or lost money, the missing piece is probably in `src/telemetry/`.
