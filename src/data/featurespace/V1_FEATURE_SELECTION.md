# V1 Feature Selection

## Purpose

This document defines the implementation boundary for the feature system.

The catalogs contain many good ideas. This file answers a different question:

- what do we actually build first
- what do we defer slightly
- what stays in research until the baseline engine is stable

The categories are:

- `V1 required`
- `V1.5`
- `research only`

V1 selection protects both performance and interpretability.
The live set must support quoting, inventory control, execution quality, and dashboard diagnosis without adding uncontrolled compute cost.
V1.5 and research features remain useful for replay, calibration, regime detection, and future decision models.

## Selection Principles

A feature belongs in `V1 required` if it is:

- directly useful for the first market-making strategy
- cheap or moderate to compute
- easy to validate
- unlikely to create architectural drag

A feature belongs in `V1.5` if it is:

- likely useful
- somewhat more complex
- dependent on more state or tuning
- valuable once the baseline is working

A feature belongs in `research only` if it is:

- expensive
- calibration-heavy
- uncertain in live value
- better used as a regime, confidence, or offline-analysis signal first

## V1 Required

These are the features we should explicitly commit to for the first live-capable market-maker.

### Core Raw And Hot Features

- `best_bid_px`
- `best_ask_px`
- `best_bid_sz`
- `best_ask_sz`
- `mid_price`
- `spread_ticks`
- `local_global_divergence`
- `microprice`
- `obi_L1`
- `quote_age_ms`

Why:

- these define the immediate quoting environment
- they are cheap
- they are mandatory for any inventory-aware quoting engine

### Required Shallow Book Features

- `obi_L1_L3`
- `bid_depth_L1`
- `ask_depth_L1`
- `bid_depth_L1_L3`
- `ask_depth_L1_L3`
- `depth_pressure_score`
- `depth_imbalance`
- `micro_vs_mid`

Why:

- these give us a much better read on local liquidity and pressure than L1 alone
- they are still compact enough for V1

### Required Execution-Quality Features

- `fill_prob_bid`
- `fill_prob_ask`
- `fill_rate`
- `queue_pressure_bid`
- `queue_pressure_ask`
- `queue_age_bid`
- `queue_age_ask`

Why:

- the strategy needs to know not just where the market is, but whether our quotes are likely to execute well

### Required Flow And Regime Features

- `ofi`
- `trade_pressure`
- `sigma_realized`
- `regime_code`
- `regime_score`

Why:

- these provide the first regime-sensitive and pressure-sensitive behavior without overcomplicating the stack

### Required Cross-Symbol Overlay Features

- `relative_spread_quality`
- `symbol_enable_flag`
- `allocation_weight`

Why:

- V1 already assumes a cross-symbol overlay, even if it is simple

### Required Online Statistical Baselines

- `welford_mean_price`
- `welford_var_return`

Why:

- stable online moments are cheap and improve normalization a lot

## V1.5

These should be the first features added after the baseline engine is stable and profiled.

### Expanded Book Structure

- `obi_L1_L5`
- `bid_depth_L1_L5`
- `ask_depth_L1_L5`
- `bid_shape_slope`
- `ask_shape_slope`
- `shape_asymmetry`
- `bid_hole_count_L1_L5`
- `ask_hole_count_L1_L5`
- `hole_asymmetry`
- `combined_depth_L1_L5`

Why:

- these improve structural book understanding
- they are useful, but not required to prove the first engine

### Expanded Queue And Fill Features

- `queue_position_bid`
- `queue_position_ask`
- `fill_prob_at_depth`
- `fill_rate_bid`
- `fill_rate_ask`
- `avg_time_to_fill_s`
- `filled_volume`
- `fill_count`

Why:

- good refinements once local order-state quality is stable

### Smoothed State Features

- `imbalance_ema_fast`
- `imbalance_ema_slow`
- `ofi_ema_fast`
- `ofi_ema_slow`
- `pressure_ema_fast`
- `pressure_ema_slow`

Why:

- useful for more stable short-horizon state estimation
- slightly more moving parts than V1 needs

### Toxicity And Arrival Features

- `vpin_toxicity_score`
- `toxicity_regime_flag`
- `expected_passive_fill_rate`
- `expected_aggressive_cost`
- `lambda_market`
- `lambda_hawkes`

Why:

- likely valuable, but they add more modeling and validation work

### Additional Statistical / Regime Features

- `rolling_mean`
- `rolling_std`
- `zscore`
- `recovery_halflife_s`
- `recovery_halflife_price`
- `recovery_halflife_spread`

Why:

- helpful for richer regime adaptation and overextension logic

## Research Only

These are worth keeping in the architecture, but should not be in the first live feature set.

### Heavier Toxicity / Impact / Cost Features

- `pin`
- `ia_score`
- `s_roll`
- `ac1`
- `lambda_limit`
- `lambda_cancel`
- full Hawkes parameter family

Why:

- these are calibration-heavy or interpretation-sensitive

### Filtered / Latent-State Features

- `price_kf`
- `vol_kf`
- `kf_price_innovation`
- `kf_vol_innovation`
- `kappa_toxic_kf`

Why:

- potentially useful, but easy to overfit or misuse before the baseline is proven

### Temporal / Bar-Based Features

- `momentum_15m`
- `momentum_30m`
- `volatility_15m`
- `mean_imbalance_15m`
- `mean_ofi_15m`
- `tsmom_15m`
- `residual_mom_15m`

Why:

- useful for slower overlays, not first-pass live quoting

### Cross-Sectional / Topology / Allocation Research Features

- `ledoit_wolf_covariance`
- `ledoit_wolf_correlation`
- `cross_impact_pressure`
- `portfolio_pressure_score`
- `relative_strength`
- `sigma_lw`
- `hrp_weights`
- `pca_loadings`
- `fm_expected_return`
- `cross_strength_entropy`
- `topology_transition_entropy`

Why:

- useful for richer overlays and allocation, but not necessary for the first functioning market-maker

### Information-Theoretic / Confidence Features

- `depth_entropy_L1_Lk`
- `flow_entropy`
- `spread_surprise_score`
- `depth_surprise_score`
- `flow_surprise_score`
- `book_shape_divergence`
- `regime_divergence_score`
- `fisher_sensitivity_score`
- `mi_imbalance_to_return`
- `mi_depth_to_fill`
- `mi_local_to_cross_signal`
- `te_leader_to_follower`
- `te_flow_to_return`
- `te_book_to_fill`
- `model_confidence_score`
- `shrinkage_similarity_score`
- `regularized_covariance_score`

Why:

- excellent for research, diagnostics, confidence overlays, and later gating logic
- too much complexity for first live deployment

### Hurst And Advanced Persistence Features

- `hurst_price`
- `hurst_microprice`
- `hurst_imbalance`
- `hurst_regime_score`

Why:

- useful, but not first-order essential for the first live engine

## Exact V1 Windows

To keep implementation bounded, V1 should explicitly support only the first short quoting windows:

### Tick Windows

- `8`
- `16`
- `32`

### Time Windows

- `100ms`
- `500ms`
- `1s`
- `5s`

V1 does not need every V1 feature at every window.

This is a V1 quoting constraint, not the final horizon set for the full system.
Longer windows like `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, and `3h` belong in slower context and research layers.

Recommended first actual window assignments:

- `obi_L1_tick_avg_16`
- `obi_L1_time_avg_500ms`
- `depth_pressure_time_avg_500ms`
- `sigma_realized_1s`
- `fill_rate_1s`

## Exact V1 Depth Levels

V1 should explicitly support:

- `L1`
- `L3`
- `L5`

But the first live strategy should rely primarily on:

- `L1`
- `L3`

Use `L5` mainly for V1.5 shape and hole features.

## Implementation Order

Recommended order:

1. raw state and top-of-book
2. microprice and OBI L1
3. shallow depth L3
4. OFI and trade pressure
5. fill probability and fill rate
6. regime classification
7. Welford online moments
8. cross-symbol ranking and allocation flags

Only after that should we add V1.5 features.

## Guardrail

Do not add a feature to the live system unless we can answer:

1. what tier it lives in
2. what window it uses
3. what fallback exists if it is stale
4. whether it changes live decisions materially

If we cannot answer those, it should stay in `research only`.
