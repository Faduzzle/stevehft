# Strategy Module Guide

## Purpose

`src/strategy/` contains alpha and quoting logic.

This folder decides what we want to do, but not how orders are physically submitted.

## Planned Files

### `base.py`

Responsibilities:

- define the strategy interface
- standardize how state becomes an intent
- keep strategy implementations swappable
- define structured strategy diagnostics and trace events
- log raw model internals before they collapse into final quote targets
- define the live strategy engine interface

### `params.py`

Responsibilities:

- implement adaptive parameter estimation and static fallback providers
- separate static defaults from online estimates
- give symbol-level overrides and overlays a clean home

### `param_types.py`

Responsibilities:

- define the runtime parameter bundle consumed by live strategies
- define adaptive-state and history-config dataclasses
- keep strategy parameter/state schemas separate from estimator logic

Current implementation:

- `AdaptiveHistoryConfig`
- `ParameterLookupTables`
- `SymbolAdaptiveState`
- `SymbolStrategyParameters`
- `StrategyParameterBundle`
- `StrategyParameterProvider`
- `StaticParameterProvider`
- `AdaptiveParameterProvider`

Important rule:

- static values are fallback bounds and test fixtures
- live strategy behavior should default to adaptive parameter resolution from market state
- the adaptive provider should use short rolling memory, not only instantaneous state
- the adaptive provider should also consume execution-ledger state such as live quote age, recent fills, and cancel pressure

### `signals.py`

Responsibilities:

- compute fair-value helpers
- compute spread and imbalance features
- estimate urgency from time-of-day and inventory pressure
- support passive-versus-aggressive decision scoring

Keep this file lightweight. Complex research logic does not belong in the live loop unless measured and justified.

Current implementation:

- quote gating
- fair-value helper blocks
- inventory skew helper blocks
- width-selection helper blocks
- tick rounding

### `cj_glft.py`

Responsibilities:

- estimate a first-pass Cartea-Jaimungal inventory-skew coefficient
- estimate a first-pass GLFT half-width from spread, volatility, queue, and
  fill-probability state
- keep the model formulas isolated from the broader parameter-provider plumbing

Current implementation:

- `CjGlftParameters`
- `estimate_cj_glft_parameters(...)`

### `market_maker.py`

Responsibilities:

- implement the first live strategy
- quote around local fair value
- skew away from inventory
- widen or pull quotes under stress
- pace participation toward `200 trades/day`

See also:

- [MARKET_MAKING_HYBRID.md](/home/faduzzle/projects/stevehft/src/strategy/MARKET_MAKING_HYBRID.md)
- [PARAMETER_CATALOG.md](/home/faduzzle/projects/stevehft/src/strategy/PARAMETER_CATALOG.md)
- [V1_DEFINITION.md](/home/faduzzle/projects/stevehft/src/strategy/V1_DEFINITION.md)
- [HEURISTIC_CONSTANTS.md](/home/faduzzle/projects/stevehft/src/strategy/HEURISTIC_CONSTANTS.md)
- [FEATURE_STRATEGY_MAP.md](/home/faduzzle/projects/stevehft/src/strategy/FEATURE_STRATEGY_MAP.md)
- [STRATEGY_TODO.md](/home/faduzzle/projects/stevehft/src/strategy/STRATEGY_TODO.md)
- [PRE_LIVE_CHECKLIST.md](/home/faduzzle/projects/stevehft/PRE_LIVE_CHECKLIST.md)

Important strategy constraints:

- prioritize passive fills
- do not rely on market orders for normal activity
- reduce inventory risk into the close

Current implementation:

- `TopOfBookMarketMaker`
- `TopOfBookMarketMakerConfig`

Despite the class name, the adaptive live path now emits a small 2-level
passive ladder per side. Static/manual parameter providers remain 1-level by
default unless `passive_ladder_depth_levels` is explicitly raised.

Current pipeline split:

- `market_maker.py` owns the per-symbol runtime orchestration loop
- `mm_feature_batch.py` owns the per-cycle symbol feature snapshot and is the
  future NumPy-backed batch transform boundary for cross-symbol signals
- `mm_pipeline.py` owns base quote construction, flatten/taker overlays, and
  conversion into a level-indexed `QuoteTarget` ladder
- `mm_trace.py` owns trace/diagnostics payload assembly

This implementation is now a compact but fully wired CJ + GLFT market-maker
runtime path, so the live loop uses model-derived skew and width instead of
standalone heuristic constants:

- market state in
- quote targets out
- structured traces emitted

It now has explicit decision blocks for:

- quote suppression on stale or too-wide books
- fair-value construction from mid, microprice, and imbalance
- CJ-style capped inventory skew
- GLFT-style bounded width selection

### `allocation/`

Responsibilities:

- manage portfolio-level or strategy-level allocation overlays
- host online allocators such as OCO-FTRL
- convert candidate strategy outputs into budgeted or weighted targets

Important rule:

- allocation decides weights, budgets, and enablement
- risk constrains them
- execution does not own them

## Concurrency Guidance

Efficient choice:

- strategy runs on the same thread as execution initially
- reads market state
- emits intents immediately

Why:

- minimizes handoff latency
- avoids stale intent queues
- keeps per-symbol decision flow simple

## Context For Future Work

If fill quality is poor, trade count is too low, or quoting behavior looks wrong, the first strategy investigation should start here.
