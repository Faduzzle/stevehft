# Execution Module Guide

## Purpose

`src/execution/` owns all order lifecycle behavior.

This includes:

- new orders
- cancels
- replace-like behavior through cancel and resubmit
- working-order tracking
- fill reconciliation

This folder is the only place that should directly translate strategy intent into `shift.Order` objects.

Execution is both a broker adapter and a control system.
It converts strategy targets into bounded actions, maintains the working-order ledger, and measures the difference between intended and realized execution.
Its telemetry supplies the dashboard and the model-calibration loop with evidence about queue position, fills, cancels, slippage, and shortfall.

It should also emit structured telemetry for every meaningful order lifecycle transition so the system can later explain:

- what the strategy wanted
- what execution decided to send
- what the broker reported back
- how positions changed afterward

Execution must also enforce live microstructure realities:

- passive orders are evaluated relative to the current visible touch
- aggressive orders are explicit spread-crossing actions
- queue age, queue position, and replace latency matter
- cancel and replace are not atomic from the bot's point of view

## Planned Files

### Portfolio / Position Ledger

The execution and reconciliation stack should maintain:

- a working-order ledger
- a portfolio or position ledger
- a fill and audit ledger keyed by `order_id`
- a reconciliation loop that polls the server and repairs local state

The live flow should be:

1. strategy emits target quotes
2. reconciler polls SHIFT and refreshes local order and portfolio state
3. reconciler compares live orders with target quotes
4. stale or off-target orders are canceled or replaced
5. router submits approved actions

This gives us a concrete place to answer:

- what positions do we currently have
- what live orders are still working
- what fills happened for a given `order_id`
- what cancellations or replacements were requested
- which orders are stale
- which orders no longer align with strategy
- which replacements should be sent next

### `order_state.py`

Responsibilities:

- store live bid and ask order identifiers
- track pending cancels
- track submitted size versus executed size
- keep lightweight local fill summaries
- hold per-symbol quote targets and order-ledger state
- maintain audit records and fill history keyed by `order_id`
- store decision and arrival benchmark prices for slippage attribution
- track realized implementation shortfall and per-order slippage summaries

### `slippage.py`

Responsibilities:

- compute signed shortfall versus decision or arrival benchmarks
- express fill slippage in ticks
- estimate simple expected passive/aggressive slippage from spread, toxicity,
  queue support, and liquidity

This file is the canonical live-order view for the strategy/execution thread.
It feeds telemetry, but telemetry should not become the source of truth for live state.

### `order_router.py`

Responsibilities:

- accept validated order intents
- construct `shift.Order` objects
- submit new orders
- submit cancellations
- enforce duplicate-action suppression

Key logic:

- passive-first order selection
- market-order usage only when policy permits
- cancel/replace throttling
- quote placement relative to the current visible best market
- emergency behavior for flattening and reconciliation-safe shutdown
- pre-trade risk and safe-mode gating before submit

### `shift_orders.py`

Responsibilities:

- construct concrete `shift.Order` objects from internal order intents
- keep all broker-type mapping in one place
- make cancellation semantics explicit rather than scattering SHIFT-specific enum logic

Current implementation:

- `ShiftPyOrderFactory`
- limit-order construction
- market-order construction for future flattening work
- explicit `CANCEL_BID` and `CANCEL_ASK` construction using broker order ids

### `runtime.py`

Responsibilities:

- assemble the execution-facing runtime graph
- create one coherent stack from:
  - order ledger
  - portfolio ledger
  - risk limits
  - safe-mode controller
  - order factory
  - router
  - reconciler

Current implementation:

- `TradingRuntimeStack`
- `build_trading_runtime_stack(...)`

### `reconciler.py`

Responsibilities:

- poll `get_executed_orders(order_id)`
- inspect `get_waiting_list()`
- inspect portfolio items and summary
- compare local state with broker state
- repair local state when fills or cancels complete
- generate cancel, keep, submit, or replace actions based on strategy targets
- append fill records and audit updates so local order history is explainable

This loop is important, but it is not the fastest loop in the system.
It is also where most broker-facing audit events are discovered and emitted.

It is also the layer that prevents the bot from believing an idealized execution model instead of the actual continuous-auction state.

## Benchmarks

Execution telemetry should support benchmark comparisons such as:

- realized fill price versus contemporaneous mid
- realized fill price versus expected spread capture
- flatten quality versus simple TWAP-style and VWAP-style baselines
- order-level decision shortfall and arrival shortfall

Current implementation:

- `OrderAuditRecord` stores `decision_price`, arrival bid/ask, realized
  slippage in ticks, and implementation shortfall
- `Reconciler` emits slippage summaries with `order_fill` events
- `OrderLedger` maintains session fill VWAP, fill TWAP, and session shortfall
  totals incrementally

These are especially useful for evaluating inventory cleanup and emergency actions.

## Concurrency Guidance

Efficient choice:

- strategy and execution share one thread initially
- order state has one writer
- reconciliation runs as a slower periodic task

Why:

- avoids decision-to-submit queue latency
- keeps live-order ownership clear
- reduces lock contention

## Context For Future Work

If we see duplicate orders, cancel storms, or inventory mismatches, the root cause is likely in `src/execution/`.
