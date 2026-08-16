# Strategy Heuristic Constants And Calibration Plan

These constants are temporary model components with explicit operational roles.
Each value must have a guardrail, a validation metric, and a replacement path when telemetry supports a better estimator.

## Purpose

This file documents the strategy-side constants that still materially shape
quoting, sizing, flattening, and execution-quality scoring.

The goal is to make each constant visible and classify whether it should:

- stay as a bounded V1 default,
- be recalibrated from data,
- or be replaced by a better estimator / model output.

This is not a criticism of having defaults.
Some fixed constants are useful guardrails.
The problem is only when a behavior-critical number is hidden, unmotivated, or
left undocumented.

## How To Read This File

Suggested status labels:

- `KEEP_V1_GUARDRAIL`: reasonable static safety bound for first deployment.
- `CALIBRATE_SOON`: acceptable temporary heuristic, but should be fit from
  replay/sim/live data.
- `REPLACE_WITH_ESTIMATOR`: should become an online estimate, calibrated model,
  or LUT driven by a measurable quantity.
- `RESEARCH_ONLY`: not urgent for V1, but worth revisiting after live evidence.

## Adaptive History And Online-State Constants

Source: `AdaptiveHistoryConfig` in
[params.py](/home/faduzzle/projects/stevehft/src/strategy/params.py)

| Constant | Current Value | What It Does | Recommendation | Better Measure / Replacement |
| --- | --- | --- | --- | --- |
| `depth_levels` | `3` | local book depth horizon for imbalance, depth, and shape features | `KEEP_V1_GUARDRAIL` | Later compare `L3` vs `L5` via replay PnL / fill-quality sensitivity. |
| `depth_fraction` | `0.25` | how much one-sided volume defines front-book concentration | `CALIBRATE_SOON` | Fit the fraction that best predicts short-horizon fill and adverse selection. |
| `min_side_volume_lk` | `2` | minimum local-side depth used for side liquidity scaling | `CALIBRATE_SOON` | Estimate side-specific minimum meaningful depth from historical fill probability by queue-ahead bucket. |
| `max_side_25pct_levels` | `3.0` | defines when front-side liquidity is too stretched across levels | `CALIBRATE_SOON` | Fit from local-book shape vs realized fill / markout, possibly per symbol. |
| `spread_alpha` | `0.50` | EWMA decay for spread regime memory | `CALIBRATE_SOON` | Choose via replay to optimize responsiveness vs churn; possibly regime-dependent alpha. |
| `imbalance_alpha` | `0.25` | EWMA decay for local depth imbalance memory | `CALIBRATE_SOON` | Fit horizon to markout predictiveness of local OBI / depth imbalance. |
| `global_imbalance_alpha` | `0.15` | EWMA decay for global L1 imbalance trend | `CALIBRATE_SOON` | Fit as slow directional regime horizon using global drift lead/lag vs NBBO mid moves. |
| `global_drift_alpha` | `0.10` | EWMA decay for global L1 mid drift | `CALIBRATE_SOON` | Same as above; likely slower than local imbalance but should be validated. |
| `depth_alpha` | `0.20` | EWMA decay for local depth/liquidity state | `CALIBRATE_SOON` | Fit from queue/fill behavior and spread regime stability. |
| `depth_shape_alpha` | `0.20` | EWMA decay for 25%-coverage level count | `CALIBRATE_SOON` | Calibrate on shape persistence and realized queue quality. |
| `return_var_alpha` | `0.08` | decay for realized-return variance | `KEEP_V1_GUARDRAIL` + calibrate | This should likely stay slower than spread/imbalance; fit by volatility forecast error. |
| `quote_age_alpha` | `0.20` | decay for quote-age summaries | `CALIBRATE_SOON` | Tie to replacement economics and stale-order fill decay. |
| `fill_rate_alpha` | `0.12` | decay for fill-rate support | `CALIBRATE_SOON` | Consider count/intensity estimator or Hawkes-style arrival model. |
| `cancel_rate_alpha` | `0.12` | decay for non-replace cancel pressure | `CALIBRATE_SOON` | Better as explicit cancellation outcome model split by strategy cancel vs broker reject. |
| `toxicity_alpha` | `0.10` | decay for adverse-selection markout EWMA | `CALIBRATE_SOON` | Fit markout horizon/decay to realized post-fill mid moves. |
| `toxicity_markout_delay_ns` | `500_000_000` | delay before scoring post-fill markout toxicity | `CALIBRATE_SOON` | Choose from empirical post-fill adverse move horizon, e.g. `100ms/250ms/500ms/1s` benchmarks. |
| `toxicity_max_pending_fills` | `256` | cap on pending markout records | `KEEP_V1_GUARDRAIL` | Keep as memory safety; tune only if drops become frequent. |

## LUT Constants

Source: `ParameterLookupTables` in
[params.py](/home/faduzzle/projects/stevehft/src/strategy/params.py)

| Constant Family | Current Shape | What It Does | Recommendation | Better Measure / Replacement |
| --- | --- | --- | --- | --- |
| `toxicity_width_multiplier` LUT | `toxicity: 0.0->1.00, 0.2->1.08, 0.4->1.18, 0.7->1.35, 1.0->1.50` | widens quotes as post-fill toxicity rises | `CALIBRATE_SOON` | Fit monotone LUT from toxicity score to expected markout / optimal half-width. |
| `toxicity_size_multiplier` LUT | `toxicity: 0.0->1.00, 0.2->0.94, 0.4->0.84, 0.7->0.66, 1.0->0.50` | shrinks size under adverse-selection risk | `CALIBRATE_SOON` | Fit monotone LUT from toxicity score to realized fill quality and inventory-risk tradeoff. |
| `inventory_pressure_gamma_multiplier` LUT | `inventory pressure: 0.0->1.00 ... 1.0->1.35` | increases inventory risk aversion near position cap | `KEEP_V1_GUARDRAIL` + calibrate | Keep monotone shape, but fit slope/breakpoints from inventory-holding cost and flatten success. |

## CJ + GLFT Approximation Constants

Source: `CjGlftModelConfig` in
[cj_glft.py](/home/faduzzle/projects/stevehft/src/strategy/cj_glft.py)

| Constant | Current Value | What It Does | Recommendation | Better Measure / Replacement |
| --- | --- | --- | --- | --- |
| `horizon_inventory_floor_weight` | `0.35` | baseline inventory urgency even far from close | `CALIBRATE_SOON` | Fit from inventory half-life and realized close penalty. |
| `horizon_inventory_dynamic_weight` | `0.65` | adds time-to-close sensitivity to CJ skew | `CALIBRATE_SOON` | Fit against end-of-day flatten success and inventory variance. |
| `inventory_spread_weight` | `0.5` | blends spread width into inventory skew strength | `REPLACE_WITH_ESTIMATOR` | Better derive from calibrated reservation-price / `q * sigma^2 * horizon` term and spread-dependent risk budget. |
| `min_inventory_skew_ticks_per_lot` | `0.05` | lower bound on skew sensitivity | `KEEP_V1_GUARDRAIL` | Keep as numerical floor unless calibration suggests zero is safe. |
| `arrival_base_intensity` | `0.05` | minimum GLFT arrival intensity | `REPLACE_WITH_ESTIMATOR` | Replace with estimated `A_bid/A_ask` from fill-vs-distance history or Hawkes arrival state. |
| `fill_probability_intensity_weight` | `1.25` | maps passive fill probability into arrival intensity | `REPLACE_WITH_ESTIMATOR` | Fit arrival-intensity mapping from realized fills by queue state and quote distance. |
| `queue_support_intensity_weight` | `0.75` | maps queue share into arrival intensity | `REPLACE_WITH_ESTIMATOR` | Replace with explicit queue-depletion model or fitted queue-share response curve. |
| `liquidity_intensity_weight` | `0.15` | maps local depth/liquidity into arrival intensity | `CALIBRATE_SOON` | Fit from local depth and realized passive-fill arrivals. |
| `min_arrival_intensity` / `max_arrival_intensity` | `0.05` / `5.0` | clamps inferred GLFT intensity | `KEEP_V1_GUARDRAIL` | Keep bounds, but derive realistic caps from replay/sim fill distributions. |
| `queue_depth_weight` | `2.0` | controls queue support influence in depth sensitivity | `REPLACE_WITH_ESTIMATOR` | Better use direct `k_bid/k_ask` estimates from quote-distance/fill calibration. |
| `min_depth_sensitivity` / `max_depth_sensitivity` | `0.05` / `5.0` | clamps inferred GLFT `k` proxy | `KEEP_V1_GUARDRAIL` | Keep numerical bounds, calibrate range from empirical response curve. |
| `risk_term_sigma_scale` | `0.5` | scales volatility risk term in GLFT width proxy | `REPLACE_WITH_ESTIMATOR` | Better derive from final model fit once CJ/GLFT equations are calibrated. |
| `risk_horizon_floor` | `0.25` | keeps some horizon risk penalty near close | `CALIBRATE_SOON` | Fit from near-close spread behavior and flatten reliability. |
| `intensity_discount_weight` | `0.35` | narrows width when arrival intensity is high | `REPLACE_WITH_ESTIMATOR` | Replace with calibrated GLFT closed-form or fitted monotone map. |
| `spread_anchor_weight` | `0.5` | base half-width anchored to half observed spread | `KEEP_V1_GUARDRAIL` + calibrate | This is economically intuitive, but should be revisited for touch-joining vs stepping-back policy. |

## Expected Slippage Proxy Constants

Source: `ExpectedSlippageModelConfig` in
[slippage.py](/home/faduzzle/projects/stevehft/src/execution/slippage.py)

| Constant | Current Value | What It Does | Recommendation | Better Measure / Replacement |
| --- | --- | --- | --- | --- |
| `base_adverse_selection` | `0.15` | baseline passive adverse-selection fraction of spread | `CALIBRATE_SOON` | Fit from realized passive-fill markouts when toxicity score is near zero. |
| `toxicity_adverse_selection_weight` | `0.85` | extra adverse-selection penalty from toxicity score | `CALIBRATE_SOON` | Fit from toxicity score vs realized post-fill shortfall. |
| `queue_penalty_weight` | `0.25` | penalizes poor queue share in passive slippage proxy | `CALIBRATE_SOON` | Fit from queue-share buckets vs realized slippage and missed-fill opportunity cost. |
| `liquidity_relief_weight` | `0.10` | reduces expected passive slippage in deep local books | `CALIBRATE_SOON` | Fit from local depth regime and markout. |
| `liquidity_relief_threshold` / `liquidity_relief_cap` | `1.0` / `2.0` | controls when liquidity relief starts and saturates | `KEEP_V1_GUARDRAIL` + calibrate | Reasonable bounded guardrail; calibrate from liquidity-score distribution. |
| `half_spread_crossing_fraction` | `0.5` | approximates one-side market-order cost as half spread | `KEEP_V1_GUARDRAIL` | Keep for simple aggressive-cost baseline; optionally add impact-on-crossing term later. |

## Direct Formula Constants Still Inside `params.py`

These are currently not wrapped in their own config objects yet.
They are worth documenting because they directly influence economics.

| Formula / Constant | Current Behavior | Recommendation | Better Measure / Replacement |
| --- | --- | --- | --- |
| `volatility_penalty = clamp(realized_sigma * 100.0, 0, 1)` | converts short-horizon return volatility into inventory-risk pressure | `CALIBRATE_SOON` | Calibrate the return-vol scaling from realized quote-loss / inventory-risk sensitivity. |
| `buying_power_scale = clamp(1.0 - 2.0 * max(bp_usage - 0.75, 0), 0.2, 1.0)` | only soft-throttles size once reserved/short-close BP exceeds 75% | `KEEP_V1_GUARDRAIL` | Good safety shape for V1; later fit threshold/slope to fill economics and BP exhaustion frequency. |
| `session_pace_multiplier = clamp(1.0 + 0.75 * max(1.0 - trade_count_ratio, 0), 1.0, 1.75)` | speeds up only when behind the trade-count target | `KEEP_V1_GUARDRAIL` + calibrate | Good one-sided policy; later fit max boost and response curve from fill-rate shortfall recovery. |
| `close_urgency_multiplier = 1.0 + 0.5 * close_progress` | gently increases inventory urgency near close | `CALIBRATE_SOON` | Fit against flatten success and pre-close adverse-selection cost. |
| `microprice_weight = clamp(0.30 + 0.25 * smoothed_abs_imbalance, 0.30, 0.75)` | leans center more toward local microprice when imbalance is stronger, but leaves more NBBO-mid anchor | `REPLACE_WITH_ESTIMATOR` | Better estimate blend weights from markout predictiveness of local microprice vs NBBO mid. |
| `imbalance_shift_ticks = clamp((0.25 + abs_imbalance) * (1 + 0.25 * abs(front_shape)) * agreement, 0.25, 1.0)` | converts local OBI/shape/global agreement into center shift | `REPLACE_WITH_ESTIMATOR` | Fit a monotone mapping from local imbalance + shape + global agreement to short-horizon fair-value move. |
| `gamma_inventory = clamp((0.85 + 0.2*abs_imb + vol_penalty) * close_urgency, 0.75, 6.0)` | heuristic inventory risk aversion before LUT pressure multiplier | `REPLACE_WITH_ESTIMATOR` | Replace with calibrated CJ-style risk-aversion schedule and inventory penalty model. |
| `allocation_weight` base blend | product of spread quality, liquidity, depth concentration, and fill support | `CALIBRATE_SOON` | This is effectively a hand-built score; later fit with OCO-FTRL or another online allocator. |
| `execution_cost_score = clamp(0.70*passive_fill_ratio + 0.30*(0.5 - avg_net_fee/0.30), 0, 1)` | rewards passive fee mix and lower net fees | `CALIBRATE_SOON` | Normalize by capital/time and fit weights from realized PnL contribution. |
| `slippage_quality_score = clamp(1 + 0.20 * (aggressive_slip - passive_slip), 0.7, 1.3)` | boosts allocation when passive slippage looks better than crossing | `CALIBRATE_SOON` | Fit boost slope/caps from realized opportunity cost and adverse-selection outcomes. |
| `passive_fill_probability = clamp(0.15 + 0.10*liq + 0.10*spread_q + 0.35*queue + 0.10*fill + 0.05*passive_ratio, 0.1, 0.9)` | handcrafted passive-fill probability proxy, with stronger queue-position weight and lower optimistic base | `REPLACE_WITH_ESTIMATOR` | Replace with logistic/RLS/Hawkes-style fill model using queue share, local depth, spread, and distance. |
| `quote_age_limit_ms = max(base_age * clamp(1 + 0.1*liq, 0.35, 1.5), 1.25 * quote_age_p90)` | adaptive quote lifetime lower-bounded by observed 90th percentile age | `KEEP_V1_GUARDRAIL` + calibrate | This is a reasonable anti-churn rule; later fit stale threshold from fill decay and adverse-selection after quote age. |
| `queue_position_good = queue_share >= 0.65` | stale same-price orders are preserved when queue priority is strong | `CALIBRATE_SOON` | Estimate queue-share threshold from expected fill advantage vs stale quote markout risk. |

## Priority List

## V1 Live-Smoke Freeze Policy

For the first read-only dry-run and first tiny live-order smoke, use this rule:

- **Frozen for V1 smoke unless a safety bug appears**:
  - hard clamps and min/max bounds,
  - `buying_power_scale`,
  - one-sided `session_pace_multiplier`,
  - `quote_age_limit_ms` anti-churn floor,
  - `queue_position_good` keep-queue threshold,
  - `toxicity_max_pending_fills`,
  - `half_spread_crossing_fraction`,
  - `max_arrival_intensity` / `min_arrival_intensity`,
  - `max_depth_sensitivity` / `min_depth_sensitivity`.
- **Recalibrate immediately after the first telemetry-bearing dry-run/smoke**:
  - EWMA / online-stat decay rates,
  - toxicity markout delay and toxicity LUT breakpoints,
  - inventory-pressure LUT breakpoints,
  - `liquidity_score` normalization,
  - `passive_fill_probability` weights,
  - queue-support weighting,
  - GLFT arrival/depth coefficients,
  - CJ inventory-risk coefficients,
  - `microprice_weight` / `imbalance_shift_ticks` blend strength,
  - slippage proxy weights,
  - close-urgency slope.
- **Do not tune mid-run during the first smoke** unless we hit a safety issue
  such as repeated stale-order churn, unexpected flatten failure, crossed/tick-
  invalid targets, or clearly wrong fee/BP units. For normal economic tuning,
  stop the run, inspect telemetry, then change one constant family at a time.

Practical interpretation:

- Phase 1 and the first Phase 2 dry-run should validate wiring and signs, not chase
  PnL by hand-tweaking.
- Phase 4 calibration should use recorded telemetry and replay summaries before
  loosening these V1 guardrails.

### Keep For V1 As Guardrails

- hard clamps and caps
- `buying_power_scale` thresholded soft throttle
- one-sided `session_pace_multiplier`
- `quote_age_limit_ms` anti-churn floor from `quote_age_p90`
- `half_spread_crossing_fraction = 0.5`

### Calibrate Soon From Replay / Sim / Live Logs

- EWMA and Welford decay constants
- toxicity LUT breakpoints
- inventory-pressure LUT breakpoints
- close urgency slope
- queue-share threshold for stale-order preservation
- volatility penalty scale
- slippage proxy weights

### Replace With Better Measures

- handcrafted `passive_fill_probability`
- handcrafted `imbalance_shift_ticks`
- handcrafted `gamma_inventory` base formula
- heuristic GLFT intensity / depth-sensitivity proxy
- handcrafted allocation score blend once OCO-FTRL or another allocator is ready

## Practical Next Step

After enough sim/live telemetry is available, build a calibration notebook or script
that estimates these constants from:

- fill probability by queue share / distance / spread regime,
- markout and toxicity by post-fill horizon,
- inventory holding cost and flatten success near close,
- symbol-level return on reserved capital,
- and realized shortfall vs passive/aggressive execution choice.
