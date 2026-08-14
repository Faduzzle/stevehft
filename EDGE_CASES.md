# Trading Edge Cases

## Purpose

This document lists the failure modes and race conditions the live system must survive.

The point is not to be clever after something breaks.
The point is to define expected bad situations in advance and decide what the safe behavior is.

## Core Rule

Never trust intended state over broker-confirmed state.

The system can maintain local expectations for speed, but authoritative state comes from:

- broker-reported waiting orders
- broker-reported executions
- broker-reported portfolio and buying power

## Primary Failure Class

The most dangerous class of bug is:

1. local code decides an order is gone
2. the order is still actually live
3. a fill arrives
4. local inventory or buying power is now wrong
5. the bot sends additional orders from a bad state

This is how small race conditions become position explosions.

## Order Lifecycle Edge Cases

### Cancel In Flight, Fill Still Arrives

Sequence:

1. resting limit order exists
2. bot sends cancel request
3. order is still live at the exchange or simulator
4. partial or full fill arrives before cancel is acknowledged

Required behavior:

- a cancel-requested order must still be treated as fillable
- no logic should assume `pending_cancel` means dead
- position and fill reconciliation must remain active until the order is terminal and fully accounted for

### Partial Fill During Replace

Sequence:

1. strategy wants a new price
2. router starts replace procedure
3. old quote partially fills before cancel completes
4. new quote may later be submitted

Required behavior:

- replace must be modeled as `cancel old -> old may still fill -> reconcile -> maybe submit new`
- the old order remains part of risk and inventory until confirmed inactive
- new quote sizing must not assume the old quote disappeared instantly

### Waiting List Entry Disappears Before Fill Reconciliation Completes

Sequence:

1. order leaves waiting list
2. local code marks it inactive
3. executed-order poll still has unseen fills

Required behavior:

- inactive in waiting list does not mean execution accounting is complete
- order audits remain open until fills and terminal status are reconciled

### Duplicate Execution Polling

Sequence:

1. `get_executed_orders(order_id)` returns the same fills across many polls
2. naive logic counts them repeatedly

Required behavior:

- dedupe fills per order
- prefer execution index or broker timestamp fingerprints
- never let repeated polling double-count size or PnL

### Out-Of-Order Observations

Possible observations:

- cancel request, then inactive, then fill
- fill, then partially filled order state
- terminal status, then late portfolio update

Required behavior:

- reconciliation must be idempotent
- local state transitions must tolerate messages arriving in surprising order
- no single observation should be assumed to be globally final until broker state converges

### Duplicate Submit After Uncertain Cancel

Sequence:

1. bot sends cancel
2. acknowledgement is delayed
3. strategy wants to re-enter
4. bot submits a second quote without reconciling the first

Required behavior:

- no blind resubmission on a side with unresolved cancel ambiguity
- reconcile broker state before new submit if prior state is uncertain

### Replace Storm

Sequence:

1. fast-changing fair value keeps producing tiny quote updates
2. router repeatedly sends cancel/replace style actions
3. broker state never settles

Required behavior:

- throttle replace cadence
- require meaningful drift before replace
- do not schedule another replace while a cancel is unresolved unless policy explicitly allows it

## Position And Portfolio Edge Cases

### Expected Position Does Not Match Broker Position

This is the classic "bot crash" scenario from asynchronous trading systems.

Possible causes:

- missed fill
- duplicate fill count
- restart without recovery
- stale portfolio snapshot
- local bug in inventory update logic

Required behavior:

- broker portfolio is authoritative
- if local and broker positions differ materially, stop normal quoting
- enter a restricted safe mode until the difference is explained or flattened

### Buying Power Looks Available Locally But Is Reserved Remotely

Possible causes:

- resting limit orders are holding balance
- short-close requirements consume more BP than expected
- local allocator is using stale portfolio state

Required behavior:

- use reconciled broker BP for submission gating
- treat reserved balance as first-class state
- do not size from optimistic local free-capital estimates

### Short Close Burden Changes Faster Than Local Estimates

Possible causes:

- price rises while we are short
- required close capital grows
- local planner still thinks prior BP is enough

Required behavior:

- short-risk checks must use conservative close-cost estimates
- near BP pressure, reduce quoting before hard rejection

### Portfolio Snapshot Lag

Sequence:

1. fills arrive
2. portfolio summary updates slightly later

Required behavior:

- tolerate small temporary lag
- but do not allow prolonged mismatch between fill ledger and portfolio state
- if lag exceeds threshold, degrade or pause risk-taking

## Market Data And Timing Edge Cases

### Stale Market Data While Orders Remain Live

Sequence:

1. feed pauses or falls behind
2. old quotes remain working
3. market has moved but bot does not know it

Required behavior:

- detect feed staleness
- cancel passive orders if staleness exceeds threshold
- stop new quoting until data freshness returns

### Close Window Race

Sequence:

1. session approaches end
2. cancels, passive fills, flatten orders, and forced-liquidation risk all overlap

Required behavior:

- close handling overrides normal market making
- flatten logic gets stricter as deadline approaches
- do not leave inventory to forced liquidation penalties if avoidable

### Session Restart / Bot Crash Recovery

Sequence:

1. process restarts mid-session
2. live orders and positions already exist at broker

Required behavior:

- recover waiting orders
- recover executed fills
- recover portfolio state
- rebuild local ledgers before sending new orders
- do not submit fresh quotes until recovery and reconciliation complete

## Required Safe Modes

The system should have explicit operating modes:

- `normal`
- `degraded_reconcile`
- `position_mismatch`
- `flatten_only`
- `kill_switch`

Expected meaning:

- `normal`: normal quoting and allocation allowed
- `degraded_reconcile`: broker state lagging or uncertain, reduce aggressiveness and new exposure
- `position_mismatch`: local and broker state disagree materially, stop normal quoting
- `flatten_only`: cancel passive risk and work only to neutralize inventory
- `kill_switch`: no new risk, cancel what can be canceled, escalate shutdown or operator action

## Required Actions When Reconciliation Fails

Detecting a mismatch is not enough.
The system needs deterministic actions.

### Action Matrix

#### `normal`

Entry condition:

- broker state is fresh
- local and broker positions are within tolerance
- waiting orders, fills, and portfolio snapshots are reconciling normally

Immediate actions:

- continue normal quoting
- continue cross-symbol allocation
- continue reconciliation polling

Allowed trading behavior:

- new passive quotes allowed
- normal cancel and replace behavior allowed
- normal flatten behavior only when strategy or session logic requires it

Clear condition:

- stays active while all health checks remain green

#### `degraded_reconcile`

Entry condition examples:

- repeated reconciliation poll failures
- waiting-list updates are stale beyond threshold
- portfolio snapshot age exceeds threshold
- execution polling is incomplete or intermittent
- broker state is available but not converging confidently

Immediate actions:

- stop sending new opening-risk orders
- suppress cross-symbol allocation increases
- cancel stale working quotes first
- widen or reduce any quotes still allowed by policy
- increase reconciliation polling priority
- emit high-priority telemetry event

Allowed trading behavior:

- cancel allowed
- risk-reducing replace allowed only if policy explicitly permits
- new passive quoting only in very conservative mode, or disabled entirely
- no new aggressive risk-taking

Escalation rule:

- if degraded reconcile persists beyond timeout, escalate to `position_mismatch` or `flatten_only`

Clear condition:

- broker state freshness and consistency recover for a sustained interval

#### `position_mismatch`

Entry condition examples:

- local net position differs from broker net position beyond tolerance
- cumulative fills imply exposure not reflected in local inventory
- broker portfolio and local expected position diverge materially

Immediate actions:

- stop all normal market making immediately
- freeze new strategy-generated quotes
- cancel all passive working orders
- stop allocator from increasing exposure anywhere
- recompute exposure from broker state only
- mark all symbols touching the mismatch as risk-blocked
- emit critical telemetry event

Allowed trading behavior:

- cancel allowed
- broker-state refresh allowed
- risk-reducing flatten orders allowed
- no new passive quotes
- no new directional or inventory-increasing orders

Escalation rule:

- if mismatch cannot be resolved quickly, escalate to `flatten_only`
- if broker state is unavailable while mismatch exists, escalate to `kill_switch`

Clear condition:

- broker and local positions match again within tolerance
- all mismatch symbols have reconciled order and fill history

#### `flatten_only`

Entry condition examples:

- unresolved position mismatch
- close-window urgency
- severe reconciliation uncertainty with non-flat inventory
- repeated state inconsistency under live risk

Immediate actions:

- cancel all passive quotes
- disable all strategy quoting
- disable allocation overlays except flatten prioritization
- compute target inventory as zero
- submit only risk-reducing actions
- prioritize smallest-latency path to neutral inventory that policy allows

Allowed trading behavior:

- cancel allowed
- flattening orders allowed
- no inventory-increasing orders
- no passive re-entry after flatten attempts unless explicitly cleared

Escalation rule:

- if flattening cannot proceed safely or broker state is unavailable, escalate to `kill_switch`

Clear condition:

- broker inventory is flat
- no unresolved live orders remain
- reconciliation is healthy again

#### `kill_switch`

Entry condition examples:

- broker unavailable while live risk exists
- repeated state divergence after attempted recovery
- stale market data plus unresolved live orders
- repeated rejected cancels or submits indicating loss of control
- explicit operator stop

Immediate actions:

- reject all new strategy actions
- cancel all cancelable live orders
- stop market-making loop from generating fresh targets
- preserve telemetry and audit logging
- raise operator-visible alert
- prepare controlled shutdown or supervised manual intervention

Allowed trading behavior:

- only emergency risk-reducing actions explicitly permitted by the safety policy
- otherwise no trading

Clear condition:

- manual or supervised recovery only
- should not auto-clear from `kill_switch` without explicit recovery logic

## Reconciliation Failure Thresholds To Define In Code

These must become explicit config values, not hand-wavy ideas:

- max waiting-list staleness before degrade
- max portfolio-snapshot staleness before degrade
- max consecutive reconciliation poll failures
- max tolerated local-versus-broker position difference per symbol
- max tolerated total net exposure mismatch
- max time allowed in `degraded_reconcile`
- max time allowed in `position_mismatch` before forced `flatten_only`
- max quote age while reconciliation is uncertain

## Priority Order Of Emergency Actions

When reconciliation fails, actions should happen in this order:

1. stop creating new risk
2. stop allocator from increasing exposure
3. cancel passive live quotes
4. reconcile broker state again
5. if exposure is non-zero or uncertain, move to flatten behavior
6. if control is still not regained, enter `kill_switch`

The bot should fail closed, not fail optimistic.

## Required Invariants

These should remain true even under race conditions:

- broker-reconciled position is authoritative
- every live side has at most one active intended quote unless strategy explicitly supports layered quoting
- pending cancel does not mean impossible to fill
- replace is never assumed atomic
- reconciliation is idempotent
- event logs record enough state to reconstruct what happened

## Logging Requirements

When these edge cases happen, telemetry should make them visible.

Important event types include:

- strategy target emitted
- reconciliation action chosen
- cancel requested
- replace requested
- live order seen or removed from waiting list
- fill recorded
- position update
- portfolio snapshot
- safe-mode transition
- duplicate fill suppressed
- restart recovery completed

## Build Implications

Before the strategy becomes sophisticated, the system must already support:

- restart recovery
- pending-cancel awareness
- fill dedupe
- broker-authoritative reconciliation
- stale-feed protection
- close-window flatten behavior

Good alpha with weak state management is still a bad trading system.
