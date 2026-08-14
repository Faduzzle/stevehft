# Source Tree Guide

## Purpose

`src/` contains the production trading system for the new SHIFT HFT build.

The code in this tree should be:

- latency-conscious
- competition-aware
- testable
- isolated from deprecated `Old model/` code

## Folder Map

- `core/`: config, clocks, session lifecycle, symbol setup
- `data/`: market-data ingestion and local cache
- `execution/`: order submission, cancel flow, reconciliation
- `risk/`: inventory controls, limits, kill-switch logic
- `strategy/`: quote logic and trading decisions
- `telemetry/`: metrics, logging, persistence
- `app/`: runtime composition and process startup

## Cross-Folder Rules

- `strategy/` does not call SHIFT directly.
- `execution/` owns all `shift.Order` creation and submission.
- `risk/` can block or modify intents, but should not contain alpha logic.
- `telemetry/` stays off the hot path.
- `data/` is the only place that should define canonical market-state layout.

## Hot Path Boundaries

The primary hot path is:

`data` -> `strategy` -> `risk` -> `execution`

Everything else should support that path without slowing it down.

## Planned File Flow

1. `core/session.py` connects and subscribes.
2. `data/market_data.py` updates symbol state.
3. `strategy/market_maker.py` reads state and emits intents.
4. `risk/limits.py` validates the intents.
5. `execution/order_router.py` submits or cancels orders.
6. `telemetry/` records what happened asynchronously.
