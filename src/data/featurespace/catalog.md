# Feature Catalog

## Purpose

This is the human-readable inventory of candidate features for the live system.

The goal is not to say all of these belong in V1.

The goal is to:

- make the candidate space explicit
- separate hot-path-safe features from heavier ones
- identify what state and windowing each feature needs
- support later selection of a production-safe subset

## Metadata Template

Each feature should eventually be tagged with:

- `name`
- `family`
- `scope`
- `window_type`
- `window_examples`
- `tier`
- `inputs`
- `consumer`
- `intuition`

## Standard Horizon Bands

The system should maintain more than just sub-second windows.

Use these horizon bands:

- microstructure:
  - `50ms`, `100ms`, `250ms`, `500ms`
- short decision:
  - `1s`, `5s`, `15s`, `30s`
- intraday context:
  - `1m`, `5m`, `15m`, `30m`
- long intraday regime:
  - `1h`, `2h`, `3h`

The shorter bands are for quote control and execution adaptation.
The longer bands are for pacing, regime, cross-symbol structure, and path-dependent overlays.

## Core Raw State Features

| Name | Family | Scope | Window Type | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `best_bid_px` | raw | per-symbol | none | hot | top of book | strategy, execution | best visible bid |
| `best_ask_px` | raw | per-symbol | none | hot | top of book | strategy, execution | best visible ask |
| `best_bid_sz` | raw | per-symbol | none | hot | top of book | strategy, features | size at best bid |
| `best_ask_sz` | raw | per-symbol | none | hot | top of book | strategy, features | size at best ask |
| `mid_price` | raw | per-symbol | none | hot | bid, ask | strategy | center of top of book |
| `spread_ticks` | raw | per-symbol | none | hot | bid, ask, tick size | strategy, risk | current spread regime |
| `local_global_divergence` | raw | per-symbol | none | hot | local and global best prices | strategy | local vs external pressure |

## Top-Of-Book And Hot Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `microprice` | microstructure | per-symbol | none | none | hot | top prices and sizes | strategy | fair value with queue pressure |
| `obi_L1` | imbalance | per-symbol | none | none | hot | best sizes | strategy | top-level order book imbalance |
| `quote_age_ms` | execution-state | per-symbol | time | `50ms`, `100ms`, `500ms` | hot | local quote timestamps | strategy, execution | stale quote detection |
| `last_fill_age_ms` | execution-state | per-symbol | time | `100ms`, `1s`, `5s` | hot or warm | fill timestamps | strategy | recent fill activity |

## Multi-Level OBI Features

Use OBI at multiple depth cutoffs, not only L1.

Representative definition:

```text
OBI(L1..Lk) = (BidDepth(L1..Lk) - AskDepth(L1..Lk)) / (BidDepth(L1..Lk) + AskDepth(L1..Lk))
```

Candidate features:

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `obi_L1` | imbalance | per-symbol | none | none | hot | best sizes | strategy | near-touch pressure |
| `obi_L1_L2` | imbalance | per-symbol | none | none | hot or warm | top 2 levels | strategy | shallow depth pressure |
| `obi_L1_L3` | imbalance | per-symbol | none | none | warm | top 3 levels | strategy | slightly deeper pressure |
| `obi_L1_L5` | imbalance | per-symbol | none | none | warm | top 5 levels | strategy | visible depth pressure |
| `obi_L1_L10` | imbalance | per-symbol | none | none | warm or slow | top 10 levels | strategy, research | broad local depth pressure |
| `obi_L1_time_avg` | imbalance | per-symbol | time | `100ms`, `500ms`, `1s`, `5s` | warm | rolling top-level imbalance | strategy | stable imbalance state |
| `obi_L1_tick_avg` | imbalance | per-symbol | tick | `8`, `16`, `32` updates | warm | rolling top-level imbalance | strategy | event-normalized imbalance |

## One-Sided Depth Features

These matter because the bid and ask sides often carry different information.

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bid_depth_L1` | depth | per-symbol | none | none | hot | best bid size | strategy | immediate support at bid |
| `ask_depth_L1` | depth | per-symbol | none | none | hot | best ask size | strategy | immediate resistance at ask |
| `bid_depth_L1_L3` | depth | per-symbol | none | none | warm | top 3 bid levels | strategy | shallow buy-side support |
| `ask_depth_L1_L3` | depth | per-symbol | none | none | warm | top 3 ask levels | strategy | shallow sell-side pressure |
| `bid_depth_L1_L5` | depth | per-symbol | none | none | warm | top 5 bid levels | strategy | visible buy-side support |
| `ask_depth_L1_L5` | depth | per-symbol | none | none | warm | top 5 ask levels | strategy | visible sell-side pressure |
| `bid_depth_time_avg` | depth | per-symbol | time | `100ms`, `500ms`, `1s`, `5s` | warm | rolling bid depth | strategy | persistent support strength |
| `ask_depth_time_avg` | depth | per-symbol | time | `100ms`, `500ms`, `1s`, `5s` | warm | rolling ask depth | strategy | persistent offer pressure |

## LOB Shape Features

These summarize how depth is distributed away from the touch.

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bid_shape_slope` | shape | per-symbol | none | none | warm | bid depth curve | strategy | how quickly bid depth grows away from touch |
| `ask_shape_slope` | shape | per-symbol | none | none | warm | ask depth curve | strategy | how quickly ask depth grows away from touch |
| `bid_convexity` | shape | per-symbol | none | none | warm | bid level depths | strategy | front-loaded vs back-loaded bid book |
| `ask_convexity` | shape | per-symbol | none | none | warm | ask level depths | strategy | front-loaded vs back-loaded ask book |
| `shape_asymmetry` | shape | per-symbol | none | none | warm | bid and ask shape summaries | strategy | one-sided structure imbalance |

## LOB Hole And Ladder Integrity Features

These capture missing liquidity or irregular ladder structure.

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bid_hole_count_L1_Lk` | holes | per-symbol | none | `k=3,5,10` | warm | bid ladder occupancy | strategy | missing depth near bid touch |
| `ask_hole_count_L1_Lk` | holes | per-symbol | none | `k=3,5,10` | warm | ask ladder occupancy | strategy | missing depth near ask touch |
| `bid_first_hole_distance` | holes | per-symbol | none | none | warm | bid ladder occupancy | strategy | how close first missing bid level is |
| `ask_first_hole_distance` | holes | per-symbol | none | none | warm | ask ladder occupancy | strategy | how close first missing ask level is |
| `hole_asymmetry` | holes | per-symbol | none | none | warm | bid and ask hole summaries | strategy | one-sided fragility of the book |

## Combined Depth Features

These compress both sides together.

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `combined_depth_L1_L3` | combined-depth | per-symbol | none | none | warm | bid and ask depth | strategy | total visible near-touch liquidity |
| `combined_depth_L1_L5` | combined-depth | per-symbol | none | none | warm | bid and ask depth | strategy | visible local liquidity |
| `depth_asymmetry_L1_Lk` | combined-depth | per-symbol | none | `k=3,5,10` | warm | bid and ask cumulative depth | strategy | signed pressure from both sides |
| `depth_pressure_score` | combined-depth | per-symbol | none or time | none, `100ms`, `500ms` | warm | compressed depth features | strategy | single score for pressure and support |

## Level-By-Level Features

Sometimes we want explicit level information instead of only aggregates.

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bid_level_size_i` | level | per-symbol, per-level | none | `i=1..10` | hot or warm | bid ladder | strategy, research | exact bid depth at level i |
| `ask_level_size_i` | level | per-symbol, per-level | none | `i=1..10` | hot or warm | ask ladder | strategy, research | exact ask depth at level i |
| `bid_level_gap_i` | level | per-symbol, per-level | none | `i=1..10` | warm | bid ladder prices | strategy | price gap structure on bid side |
| `ask_level_gap_i` | level | per-symbol, per-level | none | `i=1..10` | warm | ask ladder prices | strategy | price gap structure on ask side |

## Rolling And Windowed LOB Features

Apply both tick and time windows to book features.

### Tick-Window Candidates

Use when we want event-normalized views.

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `microprice_drift_tick` | microstructure | per-symbol | tick | `8`, `16`, `32` updates | warm | rolling microprice | strategy | short event-time drift |
| `obi_L1_tick_avg` | imbalance | per-symbol | tick | `8`, `16`, `32` | warm | rolling OBI | strategy | event-normalized pressure |
| `depth_pressure_tick_avg` | combined-depth | per-symbol | tick | `8`, `16`, `32` | warm | rolling depth pressure | strategy | persistence of book pressure |
| `hole_count_tick_avg` | holes | per-symbol | tick | `8`, `16`, `32` | warm | rolling hole summaries | strategy | structural instability persistence |

### Time-Window Candidates

Use when we want elapsed-time stability.

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `obi_L1_time_avg` | imbalance | per-symbol | time | `100ms`, `500ms`, `1s`, `5s` | warm | rolling OBI | strategy | stable imbalance state |
| `depth_pressure_time_avg` | combined-depth | per-symbol | time | `100ms`, `500ms`, `1s`, `5s` | warm | rolling depth pressure | strategy | stable support or pressure |
| `shape_slope_time_avg` | shape | per-symbol | time | `500ms`, `1s`, `5s`, `30s` | warm | rolling shape summaries | strategy | stable depth-curve regime |
| `toxicity_time_avg` | toxicity | per-symbol | time | `1s`, `5s`, `30s`, `5m` | warm | rolling toxicity | strategy | stable adverse-selection state |
| `arrival_intensity_time_avg` | arrival | per-symbol | time | `100ms`, `500ms`, `1s`, `5s` | warm | rolling arrival intensity | strategy | stable fill-arrival regime |

## Toxicity, Arrival, And Cost Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `vpin_toxicity_score` | toxicity | per-symbol | volume bucket | bucketed volume, last `N` buckets | warm | classified signed flow | strategy, risk | informed-flow risk |
| `toxicity_regime_flag` | toxicity | per-symbol | time or bucket | `5`, `10`, `20` buckets | warm | toxicity score | strategy, risk | quoting suppression regime |
| `roll_cov_lag1` | roll-model | per-symbol | time | `1s`, `5s`, `30s`, `5m` | warm | short return stream | strategy, research | lag-1 return covariance used for bounce estimation |
| `roll_c_t` | roll-model | per-symbol | time | `1s`, `5s`, `30s`, `5m` | warm | roll covariance estimate | strategy, overlay | dynamic bounce or friction parameter |
| `roll_clean_price` | roll-model | per-symbol | time | `1s`, `5s`, `30s` | warm | observed price, trade sign or signed pressure, `c_t` | strategy | microstructure-cleaned fair-value proxy |
| `roll_bounce_ratio` | roll-model | per-symbol | time | `5s`, `30s`, `5m` | warm | `c_t`, visible spread, realized spread | strategy, overlay | how much of spread behavior looks like bounce versus directional information |
| `roll_toxicity_sensor` | roll-model | per-symbol | time | `5s`, `30s`, `5m`, `15m` | warm or slow | `c_t`, spread, flow state | overlay, allocation, strategy | shrinking bounce relative to spread signals trend or informed flow regime |
| `hawkes_intensity_bid` | arrival | per-symbol-side | time | recursive | warm | bid-side event stream | strategy | expected bid-side arrival/fill intensity |
| `hawkes_intensity_ask` | arrival | per-symbol-side | time | recursive | warm | ask-side event stream | strategy | expected ask-side arrival/fill intensity |
| `expected_passive_fill_rate` | arrival | per-symbol-side | time | `100ms`, `500ms`, `1s`, `5s` | warm | intensity plus quote state | strategy | near-term passive fill chance |
| `expected_aggressive_cost` | cost | per-symbol | time | `100ms`, `500ms`, `1s`, `5s`, `30s` | warm | spread, intensity, volatility | strategy, risk | short-horizon crossing cost |

## Information-Theoretic And Regularization Features

These are useful for comparing current data to rolling or historical baselines and for deciding how much to trust a signal or model state.

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `depth_entropy_L1_Lk` | information-theory | per-symbol | none or time | `k=3,5`, `500ms`, `1s` | warm | depth across levels | strategy, research | concentration versus fragmentation of visible liquidity |
| `flow_entropy` | information-theory | per-symbol | tick or time | `16`, `32`, `1s`, `5s` | warm | signed flow buckets | strategy, research | uncertainty and dispersion in recent flow |
| `spread_surprise_score` | surprise | per-symbol | time | `500ms`, `1s`, `5s` | warm | current spread state, rolling baseline | strategy, research | how unexpected current spread state is |
| `depth_surprise_score` | surprise | per-symbol | time | `500ms`, `1s`, `5s` | warm | current depth summary, rolling baseline | strategy, research | how unusual current depth configuration is |
| `flow_surprise_score` | surprise | per-symbol | tick or time | `16`, `32`, `1s`, `5s` | warm | current signed-flow state, rolling baseline | strategy, research | how unexpected recent order flow is |
| `book_shape_divergence` | divergence | per-symbol | time | `500ms`, `1s`, `5s` | warm or slow | current depth profile, rolling baseline | strategy, research | current book shape versus recent norm |
| `regime_divergence_score` | divergence | per-symbol or global | time | `1s`, `5s`, `30s` | warm or slow | current features, historical template | strategy, overlay | current state mismatch from expected regime |
| `fisher_sensitivity_score` | fisher-information | per-symbol | time | `1s`, `5s` | slow or warm | modeled state, parameterized likelihood, local estimates | strategy, research | how informative current state is for a model |
| `mi_imbalance_to_return` | mutual-information | per-symbol | time | `5s`, `30s`, `1m`, `5m`, `15m` | slow or warm | imbalance history, return history | research, strategy | shared information between imbalance and short-term returns |
| `mi_depth_to_fill` | mutual-information | per-symbol | time | `5s`, `30s`, `1m`, `5m`, `15m` | slow or warm | depth summaries, fill outcomes | research, strategy | shared information between book state and fill behavior |
| `mi_local_to_cross_signal` | mutual-information | per-symbol or cross-symbol | time | `5s`, `30s`, `1m`, `5m`, `15m` | slow | local and overlay signals | research, overlay | redundancy or complementarity of local vs overlay signals |
| `te_leader_to_follower` | transfer-entropy | pairwise | time | `5s`, `30s`, `1m`, `5m`, `15m` | slow | leader and follower state histories | overlay, research | directional information flow from one symbol to another |
| `te_flow_to_return` | transfer-entropy | per-symbol | time | `5s`, `30s`, `1m`, `5m`, `15m` | slow | flow state history, return history | research, strategy | directional information flow from flow to price |
| `te_book_to_fill` | transfer-entropy | per-symbol | time | `5s`, `30s`, `1m`, `5m`, `15m` | slow | book state history, fill history | research, strategy | directional information flow from book state to executions |
| `model_confidence_score` | regularization | per-symbol | time | `1s`, `5s` | warm | divergence, residual stability, sensitivity | strategy | confidence modifier for advanced models |
| `shrinkage_similarity_score` | regularization | per-symbol or cross-symbol | time | `1s`, `5s`, `30s` | warm or slow | noisy comparisons plus baseline | strategy, overlay | stabilized similarity to trusted states |
| `regularized_covariance_score` | regularization | cross-symbol | time | `5s`, `30s`, `1m` | slow | multi-symbol returns or state vectors | overlay, research | stable dependency estimate under noisy data |

## Cross-Symbol Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `relative_spread_quality` | cross-symbol | per-symbol | time | `1s`, `5s`, `30s`, `5m` | warm | spread and fill metrics across names | cross-symbol overlay | rank symbols by quoting quality |
| `leader_laggard_score` | cross-symbol | pairwise or per-symbol | time | `1s`, `5s`, `30s`, `5m` | warm or slow | recent returns and book states | overlay, strategy | directional influence from related names |
| `cross_impact_pressure` | cross-symbol | per-symbol | time | `500ms`, `1s`, `5s`, `30s` | warm | related symbols and positions | overlay, strategy | correlated pressure on current symbol |
| `portfolio_pressure_score` | cross-symbol | global or per-symbol | time | `1s`, `5s`, `30s`, `5m`, `15m` | warm | inventory and group state | overlay, risk | portfolio stress throttling |
| `ledoit_wolf_covariance` | cross-symbol | global | time | `5m`, `15m`, `30m`, `1h`, `3h` | slow | multi-symbol return matrix | overlay, research | shrinkage-stabilized covariance estimate |
| `ledoit_wolf_correlation` | cross-symbol | global | time | `5m`, `15m`, `30m`, `1h`, `3h` | slow | covariance estimate | overlay, research | stable correlation structure for ranking and allocation |

## Persistence, Recovery, And Online-Moment Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `recovery_halflife_price` | recovery | per-symbol | time | `1s`, `5s`, `30s`, `5m`, `15m` | warm or slow | post-shock price path | strategy, research | how quickly price normalizes after a disturbance |
| `recovery_halflife_spread` | recovery | per-symbol | time | `1s`, `5s`, `30s`, `5m`, `15m` | warm or slow | post-shock spread path | strategy, research | how quickly spread returns after widening |
| `recovery_halflife_depth` | recovery | per-symbol | time | `1s`, `5s`, `30s`, `5m`, `15m` | warm or slow | post-shock depth path | strategy, research | how quickly liquidity replenishes |
| `hurst_price` | hurst | per-symbol | time | `5m`, `15m`, `30m`, `1h`, `3h` | slow or warm | price path | strategy, research | persistence vs mean reversion in price |
| `hurst_microprice` | hurst | per-symbol | time | `5m`, `15m`, `30m`, `1h`, `3h` | slow or warm | microprice path | strategy, research | persistence in microprice drift |
| `hurst_imbalance` | hurst | per-symbol | time | `5m`, `15m`, `30m`, `1h`, `3h` | slow or warm | imbalance path | strategy, research | persistence in order-pressure state |
| `welford_mean_price` | welford | per-symbol | online expanding or bounded | expanding, `32`, `128` | warm | price stream | strategy | stable running mean |
| `welford_var_price` | welford | per-symbol | online expanding or bounded | expanding, `32`, `128` | warm | price stream | strategy | stable running price variance |
| `welford_zscore_price` | welford | per-symbol | online expanding or bounded | expanding, `32`, `128` | warm | price stream | strategy | low-cost normalized deviation |
| `welford_var_return` | welford | per-symbol | online expanding or bounded | expanding, `32`, `128` | warm | return stream | strategy | stable realized-volatility baseline |

## Recovered Legacy Feature Ideas

These came from the deprecated `Old model` feature stack and are worth preserving conceptually, even though the old implementation itself is deprecated.

They should be treated as candidate ideas, not automatic V1 commitments.

### Legacy Linear And Book-Compression Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `spread_pct` | linear | per-symbol | none | none | hot | spread, mid | strategy, risk | spread normalized by price level | V1 |
| `half_spread` | linear | per-symbol | none | none | hot | spread | strategy | direct quoting half-width baseline | V1 |
| `depth_imbalance` | imbalance | per-symbol | none | none | warm | bid and ask cumulative depth | strategy | deeper imbalance beyond L1 | V1 |
| `bid_vwap_L1_L5` | depth-vwap | per-symbol | none | none | warm | bid ladder | strategy | average bid quality across shallow depth | V1.5 |
| `ask_vwap_L1_L5` | depth-vwap | per-symbol | none | none | warm | ask ladder | strategy | average ask quality across shallow depth | V1.5 |
| `book_vwap_spread` | depth-vwap | per-symbol | none | none | warm | bid and ask shallow vwap | strategy | effective visible book spread | V1.5 |
| `micro_delta` | microstructure | per-symbol | tick | `8`, `16`, `32` | warm | microprice history | strategy | change in microprice over event time | V1.5 |
| `micro_vs_mid` | microstructure | per-symbol | none | none | hot or warm | microprice, mid | strategy | queue pressure around the mid | V1 |
| `roc` | linear | per-symbol | tick or time | `8`, `16`, `500ms` | warm | price changes | strategy | short-horizon return proxy | V1.5 |

### Legacy OFI, Flow, And Pressure Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ofi` | order-flow | per-symbol | none | none | hot or warm | best price and size updates | strategy | order flow imbalance from quote updates | V1 |
| `cumulative_delta` | order-flow | per-symbol | time or tick | `16`, `1s`, `5s` | warm | OFI updates | strategy | accumulated signed pressure | V1.5 |
| `trade_pressure` | order-flow | per-symbol | none or short window | none, `500ms` | warm | OFI, depth normalization | strategy | normalized signed trading pressure | V1 |
| `depth_consumed_bid` | order-flow | per-symbol | none | none | warm | bid depth changes | strategy | recent consumption of buy-side liquidity | V1.5 |
| `depth_consumed_ask` | order-flow | per-symbol | none | none | warm | ask depth changes | strategy | recent consumption of sell-side liquidity | V1.5 |

### Legacy Queue And Fill-Quality Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `queue_position_bid` | queue | per-symbol-side | none | none | warm | local order state, best bid size | strategy, execution | estimated queue position on bid | V1.5 |
| `queue_position_ask` | queue | per-symbol-side | none | none | warm | local order state, best ask size | strategy, execution | estimated queue position on ask | V1.5 |
| `queue_age_bid` | queue | per-symbol-side | time | `ms` | hot or warm | local order timestamps | strategy, execution | age of resting bid quote | V1 |
| `queue_age_ask` | queue | per-symbol-side | time | `ms` | hot or warm | local order timestamps | strategy, execution | age of resting ask quote | V1 |
| `queue_pressure_bid` | queue | per-symbol-side | none or time | none, `500ms` | warm | queue position, fill rate, depth consumed | strategy | likelihood bid quote is trapped or unattractive | V1 |
| `queue_pressure_ask` | queue | per-symbol-side | none or time | none, `500ms` | warm | queue position, fill rate, depth consumed | strategy | likelihood ask quote is trapped or unattractive | V1 |
| `fill_prob_bid` | fill-quality | per-symbol-side | none or time | none, `500ms`, `1s` | warm | queue state, fill history | strategy | expected bid-side fill chance | V1 |
| `fill_prob_ask` | fill-quality | per-symbol-side | none or time | none, `500ms`, `1s` | warm | queue state, fill history | strategy | expected ask-side fill chance | V1 |
| `fill_prob_at_depth` | fill-quality | per-symbol | none or time | none, `500ms`, `1s` | warm | side fill probabilities | strategy | best fill chance at current distance | V1.5 |
| `fill_rate` | fill-quality | per-symbol | time | `500ms`, `1s`, `5s` | warm | fill history | strategy | aggregate fill arrival rate | V1 |
| `fill_rate_bid` | fill-quality | per-symbol-side | time | `500ms`, `1s`, `5s` | warm | fill history | strategy | bid-side fill rate | V1.5 |
| `fill_rate_ask` | fill-quality | per-symbol-side | time | `500ms`, `1s`, `5s` | warm | fill history | strategy | ask-side fill rate | V1.5 |
| `avg_time_to_fill_s` | fill-quality | per-symbol | time | `5s`, `30s`, `1m` | warm | fill history | strategy | speed of execution once quoted | V1.5 |
| `filled_volume` | fill-quality | per-symbol | time | `5s`, `30s`, `1m` | warm | fill history | strategy, telemetry | executed size summary | V1.5 |
| `fill_count` | fill-quality | per-symbol | time | `5s`, `30s`, `1m` | warm | fill history | strategy, telemetry | number of fills in recent history | V1.5 |

### Legacy Toxicity, Impact, And Arrival Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `pin` | toxicity | per-symbol | time or bucket | `1s`, `5s`, `30s` | slow or warm | classified trades | strategy, research | probability of informed trading proxy | V1.5 |
| `vpin` | toxicity | per-symbol | volume bucket | last `N` buckets | warm | signed bucketed flow | strategy, risk | toxicity from signed volume imbalance | V1.5 |
| `ia_score` | toxicity | per-symbol | time or bucket | `1s`, `5s`, `30s` | warm | VPIN, PIN, autocorr | strategy | blended information-asymmetry score | research |
| `s_roll` | impact | per-symbol | time | `1s`, `5s`, `30s` | warm or slow | return autocovariance | strategy, research | Roll-style spread or friction proxy | V1.5 |
| `ac1` | impact | per-symbol | time | `1s`, `5s`, `30s` | warm or slow | return series | strategy, research | lag-1 autocorrelation proxy for microstructure effects | V1.5 |
| `lambda_market` | arrival | per-symbol | time | recursive | warm | market-order event arrivals | strategy | market-order arrival proxy | V1.5 |
| `lambda_limit` | arrival | per-symbol | time | recursive | warm | limit-order event arrivals | strategy | limit submission intensity proxy | research |
| `lambda_cancel` | arrival | per-symbol | time | recursive | warm | cancel event arrivals | strategy | cancellation intensity proxy | research |
| `lambda_hawkes` | arrival | per-symbol | time | recursive | warm | market and cancel intensities | strategy | simple self-exciting fill/arrival proxy | V1.5 |

### Legacy Entropy And Multi-Scale State Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `imbalance_ema_fast` | smoothed-state | per-symbol | tick | `~10 ticks` | warm | imbalance history | strategy | fast smoothed imbalance state | V1.5 |
| `imbalance_ema_slow` | smoothed-state | per-symbol | tick | `~100 ticks` | warm | imbalance history | strategy | slow smoothed imbalance state | V1.5 |
| `ofi_ema_fast` | smoothed-state | per-symbol | tick | `~10 ticks` | warm | OFI history | strategy | fast smoothed OFI state | V1.5 |
| `ofi_ema_slow` | smoothed-state | per-symbol | tick | `~100 ticks` | warm | OFI history | strategy | slow smoothed OFI state | V1.5 |
| `pressure_ema_fast` | smoothed-state | per-symbol | tick | `~10 ticks` | warm | trade pressure history | strategy | fast smoothed pressure state | V1.5 |
| `pressure_ema_slow` | smoothed-state | per-symbol | tick | `~100 ticks` | warm | trade pressure history | strategy | slow smoothed pressure state | V1.5 |
| `ofi_entropy` | information-theory | per-symbol | tick or time | `32`, `64`, `1s`, `5s` | warm | ternary OFI state history | strategy, research | uncertainty in directional flow state | V1.5 |
| `pressure_entropy` | information-theory | per-symbol | tick or time | `32`, `64`, `1s`, `5s` | warm | ternary pressure state history | strategy, research | uncertainty in pressure regime | V1.5 |
| `return_sign_entropy` | information-theory | per-symbol | time | `1s`, `5s`, `30s` | warm | signed return history | strategy, research | uncertainty in short-horizon return sign | V1.5 |

### Legacy Statistical And Filtered Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `rolling_mean` | statistical | per-symbol | tick or time | `32`, `1s`, `5s` | warm | price history | strategy | local average price baseline | V1.5 |
| `rolling_std` | statistical | per-symbol | tick or time | `32`, `1s`, `5s` | warm | price history | strategy | local dispersion baseline | V1.5 |
| `zscore` | statistical | per-symbol | tick or time | `32`, `1s`, `5s` | warm | rolling mean and std | strategy | local overextension score | V1.5 |
| `welford_mean` | welford | per-symbol | online | expanding | warm | online price updates | strategy | stable online mean estimate | V1.5 |
| `welford_M2` | welford | per-symbol | online | expanding | warm | online price updates | strategy, research | online second central moment | research |
| `welford_n` | welford | per-symbol | online | expanding | warm | online price updates | strategy, research | sample count for online moments | research |
| `price_kf` | filtered-state | per-symbol | recursive | recursive | warm | last price | strategy, research | Kalman-smoothed price estimate | research |
| `vol_kf` | filtered-state | per-symbol | recursive | recursive | warm | realized vol | strategy, research | Kalman-smoothed volatility | research |
| `kf_price_innovation` | filtered-state | per-symbol | recursive | recursive | warm | price and Kalman state | strategy, research | surprise versus filtered price | research |
| `kf_vol_innovation` | filtered-state | per-symbol | recursive | recursive | warm | vol and Kalman state | strategy, research | surprise versus filtered volatility | research |
| `kappa_toxic_kf` | filtered-state | per-symbol | recursive | recursive | slow or warm | toxicity or impact proxy | strategy, research | filtered toxicity or distance-sensitivity state | research |

### Legacy Regime Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `regime_code` | regime | per-symbol | none or short time | none, `500ms`, `1s` | warm | spread, depth, vol, zscore, price move | strategy, risk | discrete market state classification | V1 |
| `regime_score` | regime | per-symbol | none or short time | none, `500ms`, `1s` | warm | regime inputs | strategy, risk | confidence in current regime classification | V1 |
| `recovery_halflife_s` | regime | per-symbol | time | `1s`, `5s`, `30s` | warm or slow | post-shock recovery estimates | strategy, research | mean-reversion versus trend persistence proxy | V1.5 |
| `hurst_regime_score` | regime | per-symbol | time | `5s`, `30s`, `1m` | slow or warm | Hurst estimates plus vol state | strategy, research | persistence-conditioned regime indicator | research |

### Legacy Temporal Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `momentum_15m` | temporal | per-symbol | minute bars | `15m` | slow | 1-minute closes | strategy, overlay | medium-horizon trend | V1.5 |
| `momentum_30m` | temporal | per-symbol | minute bars | `30m` | slow | 1-minute closes | strategy, overlay | longer-horizon trend | research |
| `volatility_15m` | temporal | per-symbol | minute bars | `15m` | slow | 1-minute returns | strategy, overlay | medium-horizon realized vol | V1.5 |
| `mean_imbalance_15m` | temporal | per-symbol | minute bars | `15m` | slow | 1-minute imbalance bars | strategy, research | medium-horizon order-pressure mean | research |
| `mean_ofi_15m` | temporal | per-symbol | minute bars | `15m` | slow | 1-minute OFI bars | strategy, research | medium-horizon OFI mean | research |
| `tsmom_15m` | temporal | per-symbol | minute bars | `15m` | slow | momentum and volatility | strategy, research | volatility-scaled trend score | research |
| `residual_mom_15m` | temporal | per-symbol | minute bars | `15m` | slow | momentum and market factor | strategy, overlay | idiosyncratic momentum | research |

### Legacy Cross-Sectional And Topology Features

| Name | Family | Scope | Window Type | Window Examples | Tier | Inputs | Consumer | Intuition | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `relative_strength` | cross-symbol | per-symbol | time | `1s`, `5s`, `30s` | warm | standardized relative returns | overlay, strategy | which symbols are outperforming peers | V1.5 |
| `sigma_lw` | cross-symbol | global | time | `5s`, `30s`, `1m` | slow | multi-symbol returns | overlay, research | shrinkage covariance estimate | research |
| `hrp_weights` | cross-symbol | per-symbol | time | `5s`, `30s`, `1m` | slow | covariance estimate | overlay, allocation | risk-balanced symbol weighting | research |
| `pca_loadings` | cross-symbol | per-symbol | time | `5s`, `30s`, `1m` | slow | multi-symbol returns | overlay, research | latent factor exposure | research |
| `fm_expected_return` | cross-symbol | per-symbol | minute-bar cadence | `15m+` | slow | factors and returns | overlay, research | cross-sectional expected return estimate | research |
| `cross_strength_entropy` | information-theory | cross-symbol | time | `5s`, `30s`, `1m` | slow | relative-strength distribution | overlay, research | concentration of cross-sectional strength | research |
| `topology_transition_entropy` | information-theory | cross-symbol | time | `5s`, `30s`, `1m` | slow | topology state transitions | overlay, research | instability of leader-laggard regime | research |

## Suggested V1 Candidate Set

If we want a disciplined first live subset, a strong candidate set is:

- `mid_price`
- `spread_ticks`
- `microprice`
- `obi_L1`
- `obi_L1_L3`
- `bid_depth_L1_L3`
- `ask_depth_L1_L3`
- `depth_pressure_score`
- `quote_age_ms`
- `sigma_realized`
- `passive_fill_probability`
- `relative_spread_quality`
- `symbol_enable_flag`
- `allocation_weight`

Optional V1.5 additions:

- `bid_shape_slope`
- `ask_shape_slope`
- `bid_hole_count_L1_L5`
- `ask_hole_count_L1_L5`
- `ofi`
- `trade_pressure`
- `fill_rate`
- `fill_prob_bid`
- `fill_prob_ask`
- `queue_pressure_bid`
- `queue_pressure_ask`
- `regime_code`
- `regime_score`
- `welford_mean_price`
- `welford_var_return`
- `vpin_toxicity_score`
- `expected_passive_fill_rate`

Later additions:

- full Hawkes intensity family
- richer cross-impact summaries
- adaptive path-state summaries
- BLZ-style dynamic-control state
- information-theoretic regime and confidence overlays
- Fisher-information-style model-quality summaries
- PIN / IA-style toxicity blends
- Kalman-smoothed latent state family
- cross-sectional covariance / PCA / HRP / Fama-MacBeth / topology family
- Ledoit-Wolf covariance and correlation overlays
- recovery half-life family
- Hurst-style persistence family

## Notes On Window Choices

Recommended starting windows:

- tick windows: `8`, `16`, `32`
- microstructure time windows: `50ms`, `100ms`, `250ms`, `500ms`
- short decision windows: `1s`, `5s`, `15s`, `30s`
- intraday context windows: `1m`, `5m`, `15m`, `30m`
- long intraday regime windows: `1h`, `2h`, `3h`

We do not need every feature at every horizon.

The right next step is to decide:

- which exact windows matter for V1
- which are hot-path-safe
- which should only be warm-path or research features
