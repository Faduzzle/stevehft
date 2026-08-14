# Market-Making V1 Definition

## Purpose

This document defines the first production-safe version of the market-making strategy.

It answers:

- what we are building first
- which features are required
- which windows and depth levels we will support
- which parameters must exist
- which components are deferred

The goal is to keep V1 disciplined and implementable.

## V1 Strategy Shape

V1 is:

- a per-symbol inventory-aware market maker
- with a cross-symbol ranking and allocation overlay
- using a Cartea-Jaimungal plus GLFT style architecture
- without advanced extensions turned on by default

Enabled in V1:

- inventory-aware quote center shift
- fill-intensity-aware quote width logic
- shallow-depth and imbalance features
- cross-symbol symbol ranking and allocation weight overlay
- stale quote handling
- close urgency and flatten behavior

Deferred from V1:

- full Bouchard-Loeper-Zou extension
- full Almgren-Lorenz path-dependent extension
- full cross-impact matrix logic
- full Hawkes calibration and full VPIN pipeline
- large pairwise models

## V1 Feature Set

### Required Hot Features

- `mid_price`
- `spread_ticks`
- `microprice`
- `obi_L1`
- `inventory_lots`
- `quote_age_ms`
- `time_to_close`

### Required Warm Features

- `obi_L1_L3`
- `bid_depth_L1_L3`
- `ask_depth_L1_L3`
- `depth_pressure_score`
- `sigma_realized`
- `passive_fill_probability`
- `relative_spread_quality`
- `symbol_enable_flag`
- `allocation_weight`

### Optional V1.5 Features

- `bid_shape_slope`
- `ask_shape_slope`
- `bid_hole_count_L1_L5`
- `ask_hole_count_L1_L5`
- `vpin_toxicity_score`
- `expected_passive_fill_rate`

## V1 Window Choices

To keep rolling-state complexity bounded, V1 should support only a small standardized set of short quoting windows.

### Tick Windows

- `8`
- `16`
- `32`

### Time Windows

- `100ms`
- `500ms`
- `1s`
- `5s`

V1 does not need every feature at every window.

This does not mean the full architecture should stop at `5s`.
Slower context layers should later add `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, and `3h` windows for regime, pacing, covariance, and path-dependent overlays.

Recommended first live usage:

- `obi_L1_tick_avg_16`
- `obi_L1_time_avg_500ms`
- `depth_pressure_time_avg_500ms`
- `sigma_realized_1s`

## V1 LOB Depth Levels

V1 should support:

- `L1`
- `L3`
- `L5`

Interpretation:

- `L1` is mandatory for the hot path
- `L3` is the primary shallow-depth summary for V1
- `L5` is available for warm-path shape or hole features

V1 does not require `L10` in live strategy logic.

## V1 Parameter Set

### Required Strategy Parameters

- `gamma_inventory`
- `sigma_realized`
- `A_bid`
- `A_ask`
- `k_bid`
- `k_ask`
- `quote_size_lots`
- `spread_floor_ticks`
- `spread_ceiling_ticks`
- `close_urgency_multiplier`
- `symbol_enable_flag`
- `allocation_weight`

### Required Risk / Guard Parameters

- `inventory_skew_cap`
- `quote_size_cap_lots`
- `quote_age_limit_ms`
- stale-data thresholds
- flatten-only trigger
- buying-power availability

### Optional V1.5 Parameters

- `toxicity_width_multiplier`
- `toxicity_size_multiplier`
- `expected_arrival_rate`
- `trading_cost_proxy`

## V1 Parameter Ownership

### `src/core/config`

Owns:

- default quote size
- spread floor and ceiling
- close urgency schedule
- static risk-aversion defaults

### `src/data/featurespace`

Owns:

- volatility estimate
- passive fill probability
- shallow-depth and OBI features
- depth pressure score
- optional toxicity and arrival summaries

### `src/strategy/allocation`

Owns:

- symbol enablement overlay
- allocation weights
- future online allocation such as OCO-FTRL

### `src/risk`

Owns:

- hard inventory caps
- buying-power limits
- stale-data gating
- flatten-only mode

## V1 Runtime Tiering

### Hot Path

- `mid_price`
- `spread_ticks`
- `microprice`
- `obi_L1`
- `inventory_lots`
- `quote_age_ms`
- `time_to_close`

### Warm Path

- `obi_L1_L3`
- `bid_depth_L1_L3`
- `ask_depth_L1_L3`
- `depth_pressure_score`
- `sigma_realized`
- `passive_fill_probability`
- `relative_spread_quality`
- allocation overlays

### Slow Path

- initial `A` and `k` calibration
- optional recalibration
- optional richer model parameters

## V1 Fallback Rules

These must exist before we trust the live system.

- if volatility is stale, widen or pause
- if passive fill estimate is stale, revert to conservative width
- if cross-symbol overlay is stale, revert to symbol-local quoting
- if depth features are unavailable, fall back to L1-only quoting
- if advanced toxicity or arrival models are unavailable, ignore them and run baseline hybrid logic
- if close urgency is high, flattening logic overrides normal quoting
- if buying power is insufficient, pull or reduce affected sides immediately

## V1 Allocation Design

V1 should support a simple allocation overlay, even before OCO-FTRL is enabled.

V1 allocation outputs:

- `symbol_enable_flag`
- `allocation_weight`

These are used to:

- reduce participation in low-quality names
- prefer better spread and fill environments
- prepare the architecture for later online allocators

OCO-FTRL is a future extension to this allocation layer, not a requirement for first live deployment.

## V1 Execution Behavior

V1 execution should:

- quote passively by default
- use one bid and one ask per symbol
- reprice only when meaningful state changes
- use market orders only for controlled flattening or emergency risk reduction
- suppress or widen quotes under stale, toxic, or close-risk conditions

## V1 Definition Of Success

V1 is successful when:

- quotes are generated consistently from the hybrid architecture
- inventory remains controlled
- the system finishes flat
- the system can support the trade-count objective safely
- the feature and parameter set remains small enough to understand and profile

## Next Step

After V1 is accepted, the next layer to add should be:

1. optional VPIN toxicity overlay
2. optional Hawkes arrival or cost overlay
3. richer L5 shape and hole logic
4. more advanced allocation such as OCO-FTRL
