# Market-Making Parameter Catalog

## Purpose

This document turns the hybrid market-making design into a concrete parameter inventory.

The goal is not to freeze the final math yet. The goal is to make explicit:

- which parameters exist
- what they mean
- where they come from
- how often they update
- what state they depend on
- what fallback behavior they need

This catalog covers:

- baseline Cartea-Jaimungal plus GLFT parameters
- cross-symbol overlays
- Bouchard-Loeper-Zou style extension hooks
- Almgren-Lorenz path-dependent extension hooks
- cross-impact parameters
- VPIN toxicity parameters
- Hawkes expected-arrival and trading-cost parameters

## Metadata Fields

Each parameter should eventually have:

- `name`
- `family`
- `meaning`
- `scope`
- `source`
- `cadence`
- `tier`
- `required_state`
- `consumer`
- `fallback`

## Baseline Hybrid Parameters

### Fair-Value Anchor Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `fair_value_source` | Which fair-value construction is active | global | static config | slow | config | none | strategy | default to mid |
| `mid_price` | current mid | per-symbol | online raw state | tick | hot | best bid, best ask | strategy | none |
| `microprice` | size-weighted short-horizon fair-value proxy | per-symbol | online feature | tick | hot | best prices, best sizes | strategy | mid price |
| `roll_clean_price` | microstructure-cleaned transaction-price anchor | per-symbol | online feature | warm | warm | observed price, trade sign proxy, `roll_c_t` | strategy | microprice or mid |
| `roll_anchor_weight` | blend weight on Roll-style clean-price anchor | global or per-symbol | static or offline | warm | warm | config | strategy | zero |
| `fair_value_blend_weight` | weight between mid, microprice, and other anchors | global or per-symbol | static or offline | slow | config | config | strategy | default blend |
| `alpha_bias` | short-horizon directional bias around fair value | per-symbol | online feature | warm | warm | short-term features | strategy | zero bias |
| `cross_symbol_fair_value_bias` | overlay adjustment from portfolio or related symbols | per-symbol | cross-symbol overlay | warm | warm | cross-symbol snapshot | strategy | zero bias |

### Cartea-Jaimungal Inventory Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `inventory_lots` | current signed inventory | per-symbol | online risk state | tick | hot | fills, portfolio state | strategy, risk | zero if unknown and trading paused |
| `inventory_target` | desired neutral or biased inventory target | per-symbol or global | static or overlay | warm | warm | config, overlay | strategy | zero |
| `gamma_inventory` | inventory-risk aversion strength | global or per-symbol | static or offline | slow | config | config | strategy | conservative default |
| `sigma_realized` | realized volatility estimate | per-symbol | online feature | warm | warm | rolling returns | strategy | widen or pause if stale |
| `time_to_close` | time remaining in session | global | online clock | tick | hot | session clock | strategy, risk | flatten mode if unavailable |
| `close_urgency_multiplier` | scales inventory urgency near close | global | static or schedule | warm | warm | session clock | strategy, risk | high near close |
| `inventory_skew_cap` | max allowed center shift from inventory | per-symbol or global | static risk config | slow | config | config | strategy, risk | hard cap |

### GLFT Width / Fill-Intensity Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `A_bid` | baseline bid-side arrival intensity | per-symbol-side | online estimate or offline fit | warm | warm | fills, quote distance history | strategy | conservative low value |
| `A_ask` | baseline ask-side arrival intensity | per-symbol-side | online estimate or offline fit | warm | warm | fills, quote distance history | strategy | conservative low value |
| `k_bid` | bid-side distance sensitivity of arrival intensity | per-symbol-side | offline fit or warm estimate | slow or warm | warm | fill/quote response history | strategy | conservative default |
| `k_ask` | ask-side distance sensitivity of arrival intensity | per-symbol-side | offline fit or warm estimate | slow or warm | warm | fill/quote response history | strategy | conservative default |
| `width_base_multiplier` | base quote width scaling | global or per-symbol | static config | slow | config | config | strategy | conservative default |
| `spread_floor_ticks` | minimum quoted spread or half-width floor | global or per-symbol | static config | slow | config | config | strategy, risk | floor enabled |
| `spread_ceiling_ticks` | max allowed quoted width | global or per-symbol | static risk config | slow | config | config | strategy, risk | cap enabled |
| `passive_fill_probability` | current estimate of passive fill success | per-symbol-side | online feature | warm | warm | fill history, quote distance, intensity | strategy | neutral probability |
| `liquidity_score` | current liquidity-quality score | per-symbol | online feature | warm | warm | spread, depth, fill stats | strategy | conservative low-liquidity regime |

### Side Bias And Quote Construction Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bid_bias_strength` | extra bid-side adjustment strength | per-symbol-side | static or online | warm | warm | inventory and feature state | strategy | zero |
| `ask_bias_strength` | extra ask-side adjustment strength | per-symbol-side | static or online | warm | warm | inventory and feature state | strategy | zero |
| `quote_size_lots` | default displayed quote size | per-symbol | static or overlay | warm | warm | capital allocation | strategy, risk | minimal size |
| `quote_size_cap_lots` | hard cap on quote size | per-symbol | risk config | slow | config | config | strategy, risk | hard cap |
| `repricing_threshold_ticks` | minimum move needed to requote | per-symbol | static config | slow | config | config | strategy, execution | one tick |
| `quote_age_limit_ms` | max quote age before refresh or pull | per-symbol | risk config | warm | warm | timer state | strategy, execution | strict default |

## Cross-Symbol Overlay Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `symbol_enable_flag` | whether this symbol is currently active | per-symbol | cross-symbol overlay | warm | warm | ranking, risk state | strategy | enabled if safe |
| `allocation_weight` | relative capital or quote-budget weight | per-symbol | cross-symbol overlay | warm | warm | ranking, portfolio state | strategy | equal weight |
| `symbol_rank_score` | attractiveness score for active quoting | per-symbol | cross-symbol overlay | warm | warm | quality, spread, flow, fills | strategy | neutral |
| `pace_multiplier` | multiplier when behind or ahead of trade-count target | global or per-symbol | overlay | warm | warm | trade count, session clock | strategy | 1.0 |
| `group_exposure_suppression` | throttles symbols when correlated exposure is high | per-group or per-symbol | overlay | warm | warm | correlated positions | strategy, risk | none |

## Cross-Impact Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cross_impact_matrix_Cij` | sensitivity of symbol i to symbol j | pairwise | offline fit or static grouping | slow | slow | relationship graph, historical co-moves | cross-symbol overlay | zero matrix |
| `group_interaction_weight` | sector/group-level interaction strength | per-group | static or offline | slow | slow | grouping metadata | overlay | zero |
| `correlated_inventory_pressure` | compact summary of inventory stress from related names | per-symbol | online overlay | warm | warm | portfolio positions, group state | strategy, risk | zero |
| `cross_impact_center_shift` | center-price adjustment from related symbols | per-symbol | overlay output | warm | warm | cross-impact inputs | strategy | zero |
| `cross_impact_width_multiplier` | width widening/tightening due to related-symbol stress | per-symbol | overlay output | warm | warm | cross-impact inputs | strategy | 1.0 |

## Almgren-Lorenz Path-Dependent Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `path_memory_window` | retained path horizon for adaptive control | global or per-symbol | static config | slow | config | config | feature layer | conservative window |
| `path_urgency_sensitivity` | how strongly realized path affects urgency | global or per-symbol | offline or static | slow | slow | path summaries | strategy | zero |
| `fill_path_state` | compact summary of realized fill trajectory | per-symbol | online feature | warm | warm | fill history | strategy | neutral |
| `vol_path_state` | compact summary of realized volatility path | per-symbol | online feature | warm | warm | rolling vol path | strategy | current vol |
| `path_adaptive_multiplier` | current adaptive control output | per-symbol | extension output | warm | warm | path states | strategy | 1.0 |

## Bouchard-Loeper-Zou Extension Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `dynamic_control_state` | compact extra state for richer control law | per-symbol | extension state | warm | warm | latent or filtered state | strategy | baseline hybrid only |
| `dynamic_control_sensitivity` | strength of extension response | global or per-symbol | offline or static | slow | slow | calibrated coefficients | strategy | zero |
| `uncertainty_proxy` | compact uncertainty measure used by extension | per-symbol | online feature | warm | warm | volatility, fill uncertainty, stale metrics | strategy | conservative high uncertainty |
| `blz_control_adjustment` | extension output into center/width logic | per-symbol | extension output | warm | warm | extension state | strategy | zero |

## VPIN Toxicity Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `trade_classification_method` | how buy/sell initiated flow is inferred | global | static design choice | slow | config | trade event stream | feature layer | default classifier |
| `volume_bucket_size` | volume per VPIN bucket | per-symbol or global | static config | slow | config | config | feature layer | conservative bucket |
| `vpin_bucket_count` | number of recent buckets used for toxicity summary | global or per-symbol | static config | slow | config | config | feature layer | conservative count |
| `vpin_smoothing_strength` | smoothing applied to raw toxicity | global or per-symbol | static or offline | warm | warm | bucket toxicity history | feature layer | simple average |
| `vpin_toxicity_score` | current toxicity estimate | per-symbol | online feature | warm | warm | signed-flow buckets | strategy, risk | conservative high-toxicity regime |
| `toxicity_width_multiplier` | width expansion under toxicity | per-symbol | mapping or LUT | warm | warm | toxicity score | strategy | widen |
| `toxicity_size_multiplier` | quote-size suppression under toxicity | per-symbol | mapping or LUT | warm | warm | toxicity score | strategy | reduce size |
| `toxicity_pause_threshold` | threshold for quote suppression | per-symbol or global | risk config | warm | warm | toxicity score | strategy, risk | strict threshold |

## Roll-Model And Bounce-Adjustment Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `roll_return_source` | return stream used for Roll covariance | per-symbol | design choice | slow | config | trade or microprice returns | feature layer | short trade return |
| `roll_cov_lag1` | lag-1 covariance used by Roll-style estimate | per-symbol | online feature | warm | warm | short return stream | feature layer, strategy | zero |
| `roll_c_t` | dynamic Roll bounce or friction parameter | per-symbol | online feature | warm | warm | lag-1 covariance estimate | strategy, overlay | zero |
| `roll_sign_proxy` | signed trade-direction or signed-pressure proxy | per-symbol | online feature | warm | warm | trade sign or imbalance proxy | feature layer, strategy | sign of short return |
| `roll_clean_price` | `m_t = P_t - q_t c_t` style clean-price anchor | per-symbol | online feature | warm | warm | observed price, sign proxy, `c_t` | strategy | microprice |
| `roll_bounce_ratio` | ratio of Roll bounce to visible or effective spread | per-symbol | online feature | warm | warm | `c_t`, spread state | strategy, overlay | neutral |
| `roll_toxicity_sensor` | compact regime signal from shrinking bounce relative to spread | per-symbol | online feature | warm or slow | warm | `c_t`, spread, flow state | overlay, allocation | neutral |
| `roll_mm_weight` | weight for market-making expert when bounce regime is strong | per-symbol or strategy | overlay or allocator | warm | warm | bounce ratio, toxicity sensor | allocation | neutral |
| `roll_momentum_weight` | weight for momentum expert when bounce collapses and trend emerges | per-symbol or strategy | overlay or allocator | warm | warm | bounce ratio, toxicity sensor | allocation | neutral |

## Hawkes Expected-Arrival And Trading-Cost Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `hawkes_event_source` | which events feed the process | per-model | static design choice | slow | config | event stream definition | feature layer | simpler fill-rate heuristic |
| `mu_bid` | bid-side baseline intensity | per-symbol-side | offline or warm estimate | slow or warm | warm | event history | feature layer, strategy | conservative default |
| `mu_ask` | ask-side baseline intensity | per-symbol-side | offline or warm estimate | slow or warm | warm | event history | feature layer, strategy | conservative default |
| `alpha_bid` | bid-side excitation strength | per-symbol-side | offline or warm estimate | slow or warm | warm | event history | feature layer | conservative default |
| `alpha_ask` | ask-side excitation strength | per-symbol-side | offline or warm estimate | slow or warm | warm | event history | feature layer | conservative default |
| `beta_bid` | bid-side decay rate | per-symbol-side | offline or warm estimate | slow or warm | warm | event history | feature layer | conservative default |
| `beta_ask` | ask-side decay rate | per-symbol-side | offline or warm estimate | slow or warm | warm | event history | feature layer | conservative default |
| `hawkes_state_summary` | compact recursive state for current intensity | per-symbol or per-symbol-side | online feature | warm | warm | recent event arrivals | feature layer, strategy | simpler intensity proxy |
| `expected_arrival_rate` | current expected arrival or fill intensity | per-symbol or per-symbol-side | online feature | warm | warm | Hawkes state | strategy | simpler fill-rate estimate |
| `trading_cost_proxy` | expected short-horizon cost of crossing or tightening | per-symbol | online feature | warm | warm | intensity, spread, volatility | strategy, risk | conservative high cost |
| `arrival_to_width_map` | mapping from expected arrival to quote distance | per-symbol or global | mapping or LUT | warm | warm | expected arrival rate | strategy | neutral map |
| `cost_to_aggression_map` | mapping from expected cost to aggressiveness | per-symbol or global | mapping or LUT | warm | warm | cost proxy | strategy | conservative |

## LOB Structure Parameters Needed By The Strategy

These are not model-family parameters in the same sense as `gamma` or `A`, but they are essential because the hybrid strategy will depend on them heavily.

### One-Sided Depth Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bid_depth_L1_to_Lk` | cumulative bid depth across top k levels | per-symbol, per-k | online feature | tick or warm | hot or warm | order book levels | strategy, features | top-of-book only |
| `ask_depth_L1_to_Lk` | cumulative ask depth across top k levels | per-symbol, per-k | online feature | tick or warm | hot or warm | order book levels | strategy, features | top-of-book only |
| `bid_depth_curve` | full or compressed bid-side depth profile | per-symbol | online feature | warm | warm | book levels | strategy | compressed summary |
| `ask_depth_curve` | full or compressed ask-side depth profile | per-symbol | online feature | warm | warm | book levels | strategy | compressed summary |

### LOB Shape And Hole Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bid_shape_slope` | slope or steepness of bid-side depth curve | per-symbol | online feature | warm | warm | multi-level bid depth | strategy | zero |
| `ask_shape_slope` | slope or steepness of ask-side depth curve | per-symbol | online feature | warm | warm | multi-level ask depth | strategy | zero |
| `bid_convexity` | convexity or concavity of bid-side shape | per-symbol | online feature | warm | warm | multi-level bid depth | strategy | zero |
| `ask_convexity` | convexity or concavity of ask-side shape | per-symbol | online feature | warm | warm | multi-level ask depth | strategy | zero |
| `bid_hole_count` | count of skipped or empty near-touch bid levels | per-symbol | online feature | warm | warm | bid ladder occupancy | strategy | zero |
| `ask_hole_count` | count of skipped or empty near-touch ask levels | per-symbol | online feature | warm | warm | ask ladder occupancy | strategy | zero |
| `bid_first_hole_distance` | distance in ticks to first empty bid level | per-symbol | online feature | warm | warm | bid ladder occupancy | strategy | large sentinel |
| `ask_first_hole_distance` | distance in ticks to first empty ask level | per-symbol | online feature | warm | warm | ask ladder occupancy | strategy | large sentinel |

### Combined LOB Parameters

| Name | Meaning | Scope | Source | Cadence | Tier | Required State | Consumer | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `combined_depth_L1_to_Lk` | total visible depth on both sides across top k | per-symbol, per-k | online feature | warm | warm | book levels | strategy | top-level total |
| `depth_asymmetry_L1_to_Lk` | signed depth imbalance between sides across top k | per-symbol, per-k | online feature | tick or warm | hot or warm | bid and ask cumulative depth | strategy | top-level imbalance |
| `depth_pressure_score` | compressed summary of one-sided plus combined depth pressure | per-symbol | online feature | warm | warm | depth features | strategy | zero |

## Parameter Decisions Still Needed

Before formulas, we still need to decide:

1. which of these are V1-critical
2. which belong in hot, warm, or slow path
3. which are scalar parameters versus derived features
4. which should be side-specific
5. which should be configurable versus calibrated
6. which ones need LUT mappings instead of direct formula use

## Recommended Next Step

Use this file together with:

- [MARKET_MAKING_HYBRID.md](/home/faduzzle/projects/stevehft/src/strategy/MARKET_MAKING_HYBRID.md)
- [catalog.md](/home/faduzzle/projects/stevehft/src/data/featurespace/catalog.md)

The next useful move is to mark each parameter as:

- `V1 required`
- `V1 optional`
- `future extension`
