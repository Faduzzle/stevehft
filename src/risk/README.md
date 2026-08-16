# Risk Module Guide

## Purpose

`src/risk/` enforces trading safety and competition survival constraints.

This folder should be strict, boring, and dependable.

Risk is the system's hard control boundary.
Models can rank opportunities and adjust targets, but risk decides whether the resulting action fits current exposure, capital, timing, market freshness, and state certainty.
Risk decisions must remain deterministic, explainable, and visible in telemetry.

## Planned Files

### `limits.py`

Responsibilities:

- pre-trade checks
- symbol-level position caps
- session-level inventory and buying-power caps
- reserved-balance checks for resting limit orders
- fee-aware or close-aware action filters when needed

This file decides whether an action is allowed.

Current implementation:

- `RiskLimitsConfig`
- `RiskDecision`
- `RiskEvaluation`
- `RiskLimits.evaluate(...)`

### `inventory.py`

Responsibilities:

- maintain inventory estimates
- distinguish long and short exposure
- support flatten-mode urgency
- estimate close risk and short-cover requirements
- provide the local portfolio or position ledger used by reconciliation and strategy

Important because:

- shorts have extra buying-power burden
- end-of-day forced liquidation is expensive

### `kill_switch.py`

Responsibilities:

- stop new risk
- cancel passive quotes
- force flatten procedure
- coordinate emergency safe mode

Trigger examples:

- disconnect
- stale data
- repeated rejects
- limit breach
- close window reached with open inventory
- broker-versus-local position mismatch
- reconciliation uncertainty that exceeds tolerance

Safe modes that risk should help coordinate:

- `normal`
- `degraded_reconcile`
- `position_mismatch`
- `flatten_only`
- `kill_switch`

Risk should not assume all problems are market-risk problems.
Some of the most important risk events are state-consistency failures.

For each mode, risk should define:

- entry thresholds
- blocked action types
- cancel policy
- whether flattening orders are still allowed
- whether the mode can auto-clear or requires manual recovery

Current implementation:

- `SafeMode`
- `SafeModeConfig`
- `ReconciliationHealth`
- `KillSwitchController.update(...)`
- `KillSwitchController.blocks(...)`

## Concurrency Guidance

Risk decisions should execute inline with the strategy/execution thread so checks happen immediately before order submission.

Avoid:

- asynchronous risk approval hops on the hot path
- duplicated risk state in many modules

## Context For Future Work

If the question is "should we even be allowed to do this," `src/risk/` should contain the answer.
