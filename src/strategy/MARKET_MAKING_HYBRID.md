# Hybrid Market Making Strategy

## Purpose

This document defines the design of the first serious market-making strategy for the new system.

The intended direction is a hybrid of:

- Cartea-Jaimungal style inventory-aware market making
- GLFT-style queue, fill-intensity, and spread-aware market making

This document defines the **architecture and block ownership** first.

That is deliberate.

Before treating any mathematical formula as final, we need to define:

- what parameters the strategy needs
- where those parameters come from
- how often they update
- whether they are per-symbol or cross-symbol
- whether they are offline-calibrated, online-estimated, or static
- how strategy code calls them at runtime

So the current goal is:

- define the strategy architecture
- define parameter interfaces
- define runtime call flow
- define data dependencies
- keep formula blocks isolated so first-pass live estimators can be replaced
  without breaking parameter plumbing

## Why Combine Cartea-Jaimungal And GLFT

These two families solve different but complementary problems.

### Cartea-Jaimungal Contribution

Use this side of the strategy for:

- inventory control
- reservation price or fair-value skew
- inventory penalty over time
- terminal inventory urgency
- time-to-close behavior

This is the natural place to encode:

- how inventory shifts our center price
- how aggressively we reduce risk into the close
- how inventory aversion changes with volatility and time

### GLFT Contribution

Use this side of the strategy for:

- spread-aware quoting
- fill-intensity and arrival-rate modeling
- quote-distance selection
- queue and execution tradeoff logic
- liquidity-regime sensitivity

This is the natural place to encode:

- how far from fair value to quote
- how fill probability changes with quote distance
- how spread and volatility influence optimal placement

### Hybrid View

The intended combination is:

- Cartea-Jaimungal determines where the center of our quoting should move because of inventory and time
- GLFT determines how wide and how aggressively around that center we should quote

So in words:

- center comes from inventory-aware fair-value adjustment
- width comes from execution and fill-intensity logic
- final bid and ask come from combining both

## Roll-Adjusted Fair Value

One strong extension to the fair-value layer is an online Roll-style microstructure cleaner.

Conceptually:

```text
m_t = P_t - q_t c_t
```

where:

- `P_t` is the observed transaction or local price proxy
- `q_t` is trade direction or another signed pressure proxy
- `c_t` is a dynamic Roll-style bounce / friction / adverse-selection parameter
- `m_t` is the cleaned fair-value estimate

Architectural use:

- `mid_price` and `microprice` remain simple anchors
- `roll_clean_price` becomes an additional anchor candidate
- the final center can blend across these anchors depending on regime confidence

Why this is attractive:

- it explicitly separates bid-ask bounce from cleaner directional movement
- it gives the fair-value layer a microstructure-aware de-noising mechanism
- it creates a direct bridge between market-making edge and regime detection

## Dynamic `c_t` As A Toxicity Sensor

The important idea is that `c_t` should not be treated as a fixed spread constant.

If `c_t` is large:

- bounce is strong
- short-horizon reversals are stronger
- passive market making is structurally healthier

If `c_t` shrinks relative to visible spread:

- bounce is fading
- directional or informed flow may be dominating
- the environment is becoming less attractive for pure rebate capture

That makes `c_t` and the related bounce ratio useful not only for quote placement, but also for expert allocation.

## Allocation Interpretation

This creates a clean allocator signal:

- strong bounce regime:
  - allocate more to market making
- shrinking bounce / stronger informed-move regime:
  - reduce market-making weight
  - increase momentum or trend-following weight if that expert exists

In other words:

- `roll_toxicity_sensor` can become one of the allocator inputs
- it helps detect when the market transitions from "reversal / bounce" to "trend / informed flow"

This is especially useful for future online allocation such as OCO-FTRL:

- the allocator does not need to infer everything from PnL alone
- it can use `c_t`, bounce ratio, and trend-transition diagnostics as regime context

## Microstructure Constraints On The Hybrid

This strategy cannot be specified only as a control formula block.
It also has to obey live microstructure constraints.

### NBBO / Best-Market Constraint

The produced bid and ask must be interpreted relative to the current best visible market:

- join the touch
- improve the touch
- rest behind the touch
- cross the spread only under explicit policy

So the strategy output should be read as:

- desired center
- desired width
- desired aggressiveness
- desired participation mode

Execution then maps that into actual limit or market behavior relative to the live book.

### Continuous Double Auction Constraint

The strategy must assume:

- queue priority matters
- time at the touch matters
- fills can arrive during cancel
- replace is not atomic
- adverse selection depends on book and flow state, not just fair value

That means the hybrid needs real inputs for:

- queue pressure
- fill probability
- quote age
- spread regime
- stale-book state

### Limit Versus Market Orders

For this competition:

- limit orders are the economic default
- market orders are a safety and flattening tool

The hybrid should not use market orders as routine alpha expression.
If it wants urgency, it should first express that through:

- skew
- width changes
- size changes
- one-sided quoting
- participation suppression or activation

Only when risk or close logic overrides should it escalate to aggressive crossing.

### VWAP / TWAP Benchmarking

The hybrid should be evaluated partly against simple execution benchmarks:

- if flattening inventory, did we do better or worse than a naive TWAP-style unwind
- if urgency forced aggressive behavior, was realized execution materially worse than intraday VWAP context

These are not the quoting formulas themselves.
They are benchmark disciplines for evaluating strategy outputs under inventory cleanup and emergency conditions.

## Planned Extension Families

The initial hybrid is Cartea-Jaimungal plus GLFT, but the architecture should leave room for additional extensions.

The next model families worth planning around are:

- Bouchard-Loeper-Zou style extension
- Almgren-Lorenz style adaptive path-dependent extension
- cross-impact extension
- VPIN-style order-flow toxicity extension
- Hawkes-style expected order-arrival and trading-cost extension

These should be treated as structured overlays or extensions to the parameter architecture, not as ad hoc add-ons.

The important design choice is:

- define extension hooks now
- defer exact equations until parameter ownership and state requirements are explicit

## Extension Roles

### Bouchard-Loeper-Zou Extension

Use this extension family for:

- richer control under execution uncertainty
- dynamic adjustment under more complex state dependence
- stronger treatment of inventory and control under nontrivial dynamics
- extensions where value function structure or control law is more state-rich than the baseline hybrid

Architectural role:

- this is not the first live formula block
- it is a future extension to the inventory-control and dynamic-control layer

Likely effect on the strategy:

- more expressive inventory control
- more state-sensitive reservation-price or control adjustments
- potentially richer dependence on latent state, uncertainty, or execution conditions

What we must define before using it:

- additional state variables required
- whether the extension is purely per-symbol or needs portfolio context
- whether it runs in hot path, warm path, or slow path
- whether it produces a compact control output or a richer state object

Formula status:

- intentionally deferred

Placeholder:

```text
blz_control_adjustment = <to be defined after parameter/state design>
```

### Almgren-Lorenz Adaptive Path-Dependent Extension

Use this extension family for:

- path-dependent execution adaptation
- adaptive control based on realized path, not only current snapshot state
- dynamic urgency and execution scheduling
- behavior that depends on realized trajectory of fills, volatility, or price evolution

Architectural role:

- this is the natural extension for path-dependent quoting and execution behavior
- it should influence urgency, aggressiveness, and quote persistence

Likely effect on the strategy:

- path-aware quote adaptation
- execution urgency that depends on realized session path
- better handling of changing conditions into the close or under unstable fills

What we must define before using it:

- what path state is retained
- which path summaries are bounded and production-safe
- whether the path dependence is tick-window, time-window, or regime-state based
- how adaptive control outputs are published to the live quote logic

Formula status:

- intentionally deferred

Placeholder:

```text
adaptive_path_control = <to be defined after path-state design>
```

### Cross-Impact Extension

Use this extension family for:

- symbols affecting one another directly in the quoting and inventory logic
- inventory-aware coordination across related names
- leader-laggard or sector influence on quote placement
- portfolio-level quote suppression or widening under correlated pressure

Architectural role:

- this is the bridge between per-symbol market making and portfolio-aware execution
- it belongs in the cross-symbol overlay and allocation layer

Likely effect on the strategy:

- quote center adjustments based on related names
- quote width adjustments based on cross-symbol stress
- inventory penalties that depend on correlated positions
- coordinated throttling of symbols when portfolio pressure rises

What we must define before using it:

- cross-impact state inputs
- symbol grouping or relationship graph
- whether cross-impact is modeled statically, online, or both
- how compactly the extension can be published to the hot path

Formula status:

- intentionally deferred

Placeholder:

```text
cross_impact_adjustment = <to be defined after cross-symbol parameter design>
```

### VPIN Extension

Use this extension family for:

- order-flow toxicity estimation
- adverse-selection awareness
- deciding when passive quoting is unusually dangerous
- widening, suppressing, or skewing quotes under toxic flow

Architectural role:

- this belongs in the order-flow and execution-quality layer
- it should influence quote validity, width, passive participation, and inventory accumulation

Likely effect on the strategy:

- wider quotes under toxic flow
- lower passive participation under strong toxicity
- more conservative inventory accumulation under imbalanced informed flow

What we must define before using it:

- what trade or volume inputs are available in SHIFT
- whether toxicity is estimated from tick windows, volume buckets, or bounded approximations
- whether toxicity is per-symbol only or can feed cross-symbol overlays
- how toxicity state is summarized compactly for the hot or warm path

Formula status:

- intentionally deferred

Placeholder:

```text
vpin_toxicity_adjustment = <to be defined after order-flow parameter design>
```

### Hawkes Extension

Use this extension family for:

- expected order-arrival modeling
- self-exciting event dynamics
- fill-arrival forecasting
- short-horizon execution-opportunity and trading-cost modeling

Architectural role:

- this belongs in the fill-intensity and expected-arrival layer
- it should influence quote distance, size, urgency, and trading-cost estimates

Likely effect on the strategy:

- better estimate of expected passive fills
- better estimate of when aggressive actions are expensive or justified
- dynamic quote adaptation during clustered arrival regimes

What we must define before using it:

- which events feed the process
- whether we model market-order arrivals, fills, or broader event intensities
- what bounded state summaries are enough for live use
- whether the model output is used directly or only through compact derived scores

Formula status:

- intentionally deferred

Placeholder:

```text
hawkes_arrival_adjustment = <to be defined after arrival-model parameter design>
hawkes_cost_adjustment = <to be defined after trading-cost parameter design>
```

## Important Design Rule

Do not hardcode formulas into strategy code before parameter plumbing exists.

We first need a clean parameter layer.

That means:

- parameter names
- parameter ownership
- update cadence
- parameter source
- fallback behavior

Only then should we fill in exact equations.

## Strategy Decomposition

The hybrid strategy should be decomposed into conceptual blocks.

### 1. Mid / Fair-Value Block

Purpose:

- define the current fair-value anchor before inventory skew

Possible inputs:

- mid price
- microprice
- local/global divergence
- short-horizon alpha features
- cross-symbol overlay bias

Output:

- `base_fair_value`

Current implementation:

- `compute_fair_value(...)` blends NBBO mid, local microprice, local depth
  imbalance, and a slow global-drift shift
- front-shape shift is applied as an additional compact correction

Implementation note:

- this is a V1 fair-value estimator, not a final frozen equation
- the formula is intentionally isolated in `signals.py` and `mm_pipeline.py`

### 2. Inventory Control Block

Purpose:

- shift quoting center based on inventory and time remaining

Possible inputs:

- current inventory
- inventory target
- volatility estimate
- risk-aversion parameter
- time to close
- close-mode urgency
- optional BLZ-style dynamic-control extension state
- optional path-dependent adaptive state

Output:

- `inventory_adjusted_fair_value`
- or `inventory_skew`

Current implementation:

- `estimate_cj_glft_parameters(...)` produces
  `cj_inventory_skew_ticks_per_lot`
- `compute_inventory_skew(...)` applies a capped convex inventory skew
- `mm_pipeline.py` shifts the quote center by subtracting that inventory skew
  from fair value

Implementation note:

- this is the current Cartea-Jaimungal role in V1: inventory-aware reservation
  price skew plus stronger close-time risk aversion
- exact coefficients remain calibratable through `params.py` and `cj_glft.py`

### 3. Quote Width Block

Purpose:

- determine half-spread or quote distance around the adjusted center

Possible inputs:

- volatility
- spread regime
- fill-intensity parameters
- liquidity score
- trade-count pacing pressure
- passive fill quality estimates
- optional VPIN toxicity estimate
- optional Hawkes expected-arrival estimate
- optional Hawkes-based trading-cost adjustment
- optional adaptive path-dependent urgency controls
- optional cross-impact widening or tightening signal

Output:

- `half_spread`
- `bid_distance`
- `ask_distance`

Current implementation:

- `estimate_cj_glft_parameters(...)` computes a bounded
  `glft_half_width_ticks`
- that width uses spread anchor, realized volatility in tick units, queue/fill
  support, liquidity, and an arrival-intensity discount
- `compute_half_width(...)` enforces the final floor/ceiling bounds
- side-specific bid/ask width multipliers are then applied in `mm_pipeline.py`

Implementation note:

- this is the current GLFT role in V1: fill-intensity and queue-aware width
  around the CJ-shifted center
- it is a first-pass approximation, not a final calibrated closed-form model

### 4. Asymmetry / Side Bias Block

Purpose:

- allow bid and ask widths to differ when inventory, fills, or cross-symbol state suggest asymmetry

Possible inputs:

- inventory sign and magnitude
- fill imbalance
- local short-term drift
- cross-symbol overlay bias
- close urgency
- optional cross-impact side pressure
- optional path-dependent execution pressure

Output:

- `bid_bias`
- `ask_bias`

Current implementation:

- side asymmetry comes from `side_size_tilt`, `side_width_tilt`, queue support,
  soft one-sided OBI pressure, toxicity multipliers, and regime overlays
- strong one-sided inventory or taker overlay can temporarily collapse to a
  single active side

### 5. Final Quote Construction Block

Purpose:

- convert center and width outputs into actual quote targets

Output:

- `target_bid_px`
- `target_ask_px`
- `target_bid_size`
- `target_ask_size`

Current implementation:

- `mm_pipeline.build_quote_plan(...)` converts center, width, and side
  multipliers into rounded bid/ask prices and sizes
- `MarketMakingQuotePlan.to_quote_target(...)` maps that internal plan into the
  execution-facing `QuoteTarget`

### 6. Quote Validity Block

Purpose:

- determine whether each side should stay live, be widened, or be pulled

Possible triggers:

- stale data
- spread too tight
- inventory breach
- close mode
- insufficient buying power
- poor fill-quality regime
- high toxicity regime
- unfavorable expected-arrival or trading-cost regime

Output:

- `enable_bid`
- `enable_ask`
- `flatten_only_mode`

## Parameter Architecture

This is the most important section for the current phase.

We should define the parameter model before defining the equations.

## Parameter Classes

The strategy should distinguish parameter classes by role.

### Structural Parameters

These define the shape of the strategy.

Examples:

- inventory aversion
- close urgency strength
- quote-width base multiplier
- asymmetry strength
- pacing sensitivity

Properties:

- relatively stable
- often configured or slowly recalibrated

### Market-State Parameters

These reflect current environment conditions.

Examples:

- volatility estimate
- fill-intensity estimate
- liquidity score
- spread regime
- passive fill probability
- order-flow toxicity estimate
- expected order-arrival estimate
- trading-cost estimate

Properties:

- dynamic
- updated from data/features

### Risk Parameters

These enforce safety and business rules.

Examples:

- max inventory
- max quote size
- max spread width
- stale-data thresholds
- close-mode triggers

Properties:

- owned jointly by strategy and risk
- must be explicit and testable

### Overlay Parameters

These come from cross-symbol or session-level coordination.

Examples:

- symbol enable flag
- capital allocation weight
- relative attractiveness score
- trade-count pacing multiplier
- group exposure suppression

Properties:

- not purely symbol-local
- may update on slower cadence

### Extension Parameters

These support richer model families beyond the baseline hybrid.

Examples:

- dynamic-control extension coefficients
- adaptive path-memory coefficients
- cross-impact sensitivity parameters
- group-interaction weights
- VPIN configuration parameters
- Hawkes kernel or excitation parameters
- Hawkes trading-cost parameters

Properties:

- may be partially offline-calibrated
- often need stricter metadata and cadence control
- should usually publish compact outputs into live strategy logic

## Parameter Source Types

Every parameter should declare its source type.

### Static Config

Examples:

- baseline risk aversion
- default quote size
- spread floor

### Online Feature Estimate

Examples:

- realized volatility
- fill-rate estimate
- intensity proxy
- imbalance regime
- VPIN toxicity proxy
- expected arrival-rate proxy
- trading-cost proxy

### Offline Calibration

Examples:

- fitted fill-intensity curve coefficients
- quote-distance response coefficients
- inventory penalty scaling constants
- cross-impact sensitivity coefficients
- adaptive-control calibration coefficients
- Hawkes kernel coefficients
- Hawkes baseline-intensity coefficients
- toxicity-model calibration constants

### Cross-Symbol Overlay

Examples:

- allocation weights
- symbol ranking score
- urgency multiplier

### Extension State Or Extension Model

Examples:

- path-dependent state summaries
- dynamic-control latent state
- cross-impact state summaries

## Parameter Scope

Every parameter should also declare scope.

### Global Parameters

Shared across all symbols.

Examples:

- session close urgency schedule
- baseline pacing target
- regime-mode switches
- cross-impact regime switches

### Per-Symbol Parameters

Specific to one symbol.

Examples:

- symbol volatility estimate
- symbol fill-intensity estimate
- symbol quote width multiplier
- symbol-specific adaptive-control coefficients
- symbol-specific toxicity estimate
- symbol-specific arrival-intensity estimate

### Per-Symbol-Side Parameters

Specific to one symbol and one side.

Examples:

- bid-side fill adjustment
- ask-side fill adjustment
- side-specific queue preference
- bid-side arrival-intensity adjustment
- ask-side arrival-intensity adjustment

### Pairwise Or Group Parameters

These are important once cross-impact enters the design.

Examples:

- symbol-pair cross-impact sensitivity
- group-level stress multiplier
- sector interaction weights

## Parameter Update Cadence

We should not update every parameter at the same speed.

### Tick / Event Driven

Examples:

- best-price derived state
- inventory
- quote-age and staleness

### Warm Path Periodic

Examples:

- realized volatility
- fill-rate estimates
- symbol quality score
- pacing score
- VPIN or toxicity summaries
- Hawkes state summaries or compact arrival proxies
- bounded path-state summaries
- compact cross-impact summaries

### Slow Path Periodic

Examples:

- offline-calibrated coefficient refresh
- heavier intensity fits
- cross-symbol ranking model refresh
- pairwise cross-impact recalibration
- adaptive-control coefficient refresh
- Hawkes parameter refresh
- toxicity-model recalibration

## Parameter Interface Contract

The strategy should not reach into random modules for values.

It should receive a compact parameter bundle.

Recommended conceptual interfaces:

### `SymbolMarketInputs`

Current raw and derived market state needed by the strategy.

Suggested fields:

- best bid and ask
- sizes
- mid
- microprice
- spread in ticks
- local/global divergence
- staleness metrics

### `SymbolFeatureInputs`

Current feature outputs needed by the strategy.

Suggested fields:

- volatility estimate
- fill-rate estimate
- intensity proxy
- short-term alpha bias
- liquidity score
- VPIN toxicity summary
- expected arrival-rate summary
- trading-cost summary
- bounded path-state summary
- optional compact dynamic-control extension output

### `SymbolRiskInputs`

Current risk and operational constraints.

Suggested fields:

- current inventory
- max allowed inventory
- available quote budget
- stale-data flag
- flatten-only flag
- close urgency level

### `CrossSymbolInputs`

Current portfolio-wide overlay state.

Suggested fields:

- symbol enable flag
- allocation weight
- ranking score
- pacing multiplier
- group exposure suppression
- compact cross-impact summary
- correlated inventory pressure

### `StrategyParameterBundle`

This is the object the market-making logic should actually consume.

It should contain:

- structural parameters
- current symbol inputs
- current feature inputs
- current risk inputs
- current cross-symbol overlay inputs
- optional extension parameters and extension-state summaries

## Runtime Call Graph

Before equations, we need call discipline.

Recommended runtime sequence for one symbol:

1. load `SymbolMarketInputs`
2. load `SymbolFeatureInputs`
3. load `SymbolRiskInputs`
4. load `CrossSymbolInputs`
5. build `StrategyParameterBundle`
6. compute `base_fair_value`
7. compute inventory adjustment
8. compute quote width and asymmetry
9. compute target bid and ask prices and sizes
10. apply quote validity rules
11. emit final intent

Possible later extension insertion points:

- BLZ-style control adjustment after baseline fair-value and inventory state are assembled
- adaptive path-dependent adjustment before final urgency and width selection
- cross-impact adjustment before final bid and ask construction
- VPIN toxicity adjustment before quote validity and width selection
- Hawkes arrival and trading-cost adjustment before final size and distance selection

This gives us a clean place to insert formulas later without changing the architecture.

## Suggested Function Boundaries

The first production implementation should likely split logic into functions like:

### `compute_base_fair_value(bundle)`

Returns:

- `base_fair_value`

Formula:

- placeholder for now

### `compute_inventory_adjustment(bundle, base_fair_value)`

Returns:

- `inventory_skew`
- `inventory_adjusted_fair_value`

Formula:

- placeholder for now

### `compute_extension_adjustments(bundle, intermediate_state)`

Returns:

- optional BLZ-style control adjustment
- optional adaptive path adjustment
- optional cross-impact adjustment
- optional VPIN toxicity adjustment
- optional Hawkes arrival adjustment
- optional Hawkes trading-cost adjustment

Formula:

- placeholder for now

### `compute_quote_width(bundle, adjusted_fair_value)`

Returns:

- `half_spread`
- side-specific distances if asymmetry is allowed

Formula:

- placeholder for now

### `compute_side_bias(bundle)`

Returns:

- `bid_bias`
- `ask_bias`

Formula:

- placeholder for now

### `build_target_quotes(bundle, adjusted_fair_value, width_output, bias_output)`

Returns:

- target bid and ask price
- target bid and ask size

Formula:

- placeholder for now

### `apply_quote_guards(bundle, target_quotes)`

Returns:

- guarded quote decision
- side enable flags
- flatten-only override if necessary

## State Needed Before Formula Work

Before writing formulas, we need to know what state is guaranteed to exist.

Minimum required inputs:

- best bid and ask
- spread
- mid
- inventory
- time to close
- volatility estimate
- fill-rate or intensity estimate
- symbol enable flag
- pacing and close-mode state

Likely future extension inputs:

- bounded path-state summaries
- compact cross-impact summaries
- extension-state validity flags
- toxicity summaries
- arrival-intensity summaries
- cost-of-trading summaries

If any of these do not have a clear owner yet, we should fix ownership first.

## Calibration Questions We Must Answer First

Before filling in equations, answer:

1. Which parameters are configured statically?
2. Which are estimated online?
3. Which are calibrated offline?
4. Which are per-symbol versus global?
5. How often do they update?
6. What happens when a parameter is stale or unavailable?
7. Which parameters are allowed to affect bid and ask asymmetrically?
8. Which parameters are strategy-only and which must be visible to risk?
9. Which extension states are compact enough for live use?
10. Which pairwise or group interactions are stable enough to calibrate?
11. Which adaptive path variables are actually worth carrying in bounded memory?
12. How should toxicity be estimated with bounded live memory?
13. Which Hawkes state summaries are compact enough for the hot or warm path?
14. How should expected arrival and trading-cost outputs feed quote width, size, and suppression logic?

## Fallback Design

The strategy must still behave safely when some model-driven parameter is unavailable.

Examples:

- if fill-intensity estimate is unavailable, fall back to a conservative width regime
- if volatility estimate is stale, widen or pause
- if cross-symbol overlay is unavailable, revert to symbol-local quoting
- if close urgency is high, prioritize flattening over normal model outputs
- if extension-state inputs are unavailable, revert to baseline Cartea-Jaimungal plus GLFT behavior
- if toxicity state is unavailable, use conservative passive participation defaults
- if Hawkes arrival or cost estimates are unavailable, revert to simpler fill-intensity heuristics

This fallback policy should be defined before equations become complicated.

## Relationship To The Rest Of The System

### `src/data/`

Provides:

- market inputs
- feature inputs
- rolling estimates

### `src/risk/`

Provides:

- inventory limits
- quote enablement constraints
- flatten triggers

### `src/strategy/`

Owns:

- parameter bundling
- formula evaluation
- quote target generation

### `src/execution/`

Owns:

- translating targets into live orders
- cancel/reprice mechanics

## Current Implementation Alignment

The current live implementation now follows this decomposition as:

1. `AdaptiveParameterProvider` builds `StrategyParameterBundle`
2. `compute_quote_gate(...)` validates whether the symbol can quote
3. `mm_pipeline.build_quote_plan(...)` computes:
   - fair value
   - CJ inventory skew
   - GLFT half-width
   - side-specific sizes/widths
   - flatten/taker overlays
4. `TopOfBookMarketMaker` emits `QuoteTarget` plus diagnostics traces
5. `Reconciler` maps targets into submit/cancel/replace/flatten commands

The CJ and GLFT formulas are currently implemented in
`src/strategy/cj_glft.py`, with additional guardrails and state transforms in
`src/strategy/params.py`.

## What We Should Define Next

Before formulas, the next useful artifacts are:

1. parameter catalog
2. parameter ownership map
3. input bundle schema
4. update cadence table
5. fallback policy table

After those are defined, we can safely refine:

- Cartea-Jaimungal inventory adjustment formula and coefficients
- GLFT quote width / arrival-intensity approximation
- Bouchard-Loeper-Zou style dynamic-control extension
- Almgren-Lorenz adaptive path-dependent extension
- cross-impact adjustment rules
- VPIN toxicity adjustment rules
- Hawkes expected-arrival and trading-cost adjustments
- side-asymmetry rules
- final quote construction equations

## Current Status

Status:

- hybrid strategy direction chosen
- architecture and decomposition defined
- first-pass CJ + GLFT estimators implemented in `cj_glft.py`
- quote construction refactored into `mm_pipeline.py`
- parameter layer explicit in `params.py`

Current stance:

- V1 formulas are live and test-covered
- they are still considered **calibratable first-pass estimators**, not final
  locked math
