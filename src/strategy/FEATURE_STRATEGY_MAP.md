# Feature Strategy Map

This document is a brainstorming and wiring guide for where different feature
families should sit in the strategy stack.

This map defines the decision path from an observation to a model input, strategy effect, risk interaction, and validation metric.
It prevents feature accumulation without a measurable purpose.

The core idea is:

- not every feature should directly move quotes
- some features are fast execution signals
- some are slower fair-value signals
- some are risk and confidence gates
- some belong in cross-symbol overlays, not single-symbol microstructure

## Vectorization Boundary

Current live rule:

- keep broker/order/reconciler state scalar and object-based
- keep per-symbol online estimators scalar when stateful recurrences dominate
- use `mm_feature_batch.py` as the per-cycle symbol snapshot and future
  NumPy-backed batch transform boundary
- push cross-symbol arrays (synthetic mid, lead-lag, basket dispersion,
  multi-level ladder scoring) into that batch layer before writing results back
  into per-symbol quote plans

Why:

- this avoids turning the execution state machine into array-index bookkeeping
- but still gives us one clean place to scale feature math as symbol count and
  ladder depth increase

## Strategy Families

### A. Single-Symbol Market Making

Main job:

- quote one bid and one ask around a symbol-local fair value
- earn spread/rebate while controlling inventory and adverse selection

Best feature families:

- local L2/L3 microstructure: `local_microprice`, `local_depth_imbalance`,
  `local_multi_level_voi`, queue ahead/share, depth concentration, front-shape
- execution quality: fill rate, fill arrival time, passive ratio, cancel rate,
  expected slippage, realized shortfall
- volatility/regime: realized sigma, spread z-score, quote-age quantiles,
  CUSUM/Page-Hinkley breaks
- toxicity: post-fill markout, VPIN/PIN-style flow toxicity, impact estimates
- inventory and session state: inventory lots, close urgency, BP usage,
  safe mode

What these should control:

- fair-value center: microprice, OBI/VOI, RLS drift, synthetic-mid basis
- half-width: GLFT width, volatility, toxicity, regime breaks
- side skew: CJ inventory skew, local pressure, drift bias
- size: allocation weight, queue quality, toxicity, liquidity, inventory
- taker exits: inventory pressure + adverse drift + weak queue + edge after
  fees/slippage

What not to do:

- do not let one noisy fast signal directly trigger hard flatten
- do not use multi-level shape from global book if global is effectively L1
- do not double-count the same imbalance feature in too many places without
  caps

## B. Multi-Level Market Making Ladder

Main job:

- maintain several passive levels per side around one fair value while avoiding
  stale/dead queue positions

Current live V1 status:

- the adaptive provider emits a 2-level bid ladder and 2-level ask ladder in
  normal passive mode
- level `0` is the legacy top quote slot
- deeper levels are reconciled by `level_index`, so each side/level has its own
  cancel/replace state
- flatten and inventory-taker overlays remain single-level market actions

Best feature families:

- queue position and queue decay by level
- distance-to-touch fill probability by level
- depth shape / hole structure across L2-Lk
- latency and book-staleness haircuts
- toxicity and regime confidence

What these should control:

- level spacing: wider spacing in toxic/volatile or fragmented books
- per-level size decay: more size near front when queue edge is strong, less
  size when queue edge is weak
- replace policy: only refresh a level when price drift is harmful or queue
  rank is poor after latency haircut
- ladder center shift: inherited from the single-symbol fair-value model

Recommended features:

- `queue_share_level_i`
- `queue_ahead_lots_level_i`
- `expected_fill_prob_level_i`
- `queue_decay_rate`
- `depth_entropy_L1_Lk`
- `bid_hole_count_L1_Lk` / `ask_hole_count_L1_Lk`

## C. Physics-Informed Single-Symbol Momentum Overlay

Main job:

- add a small directional center/skew overlay from velocity + mass style state,
  without turning the MM into an unstable momentum taker

Best feature families:

- fast velocity: recent mid move in ticks, local VOI/OBI delta,
  touch migration speed
- slow velocity: EWMA drift, RLS predicted mid move, tape-linked VOI trend
- liquidity mass: local depth and recent trade volume as inertia
- impact mass: inverse depth / inverse volume as "thin-book acceleration"
- regime confidence: return z-score, flow entropy, change-point signals

What these should control:

- small bounded center shift
- side-specific width/size asymmetry
- taker-exit permission when inventory is already adverse

Suggested formulas to prototype:

- `v_fast = mid_move_ticks_1 + c1 * d(local_voi)`
- `v_slow = ewma_global_drift_ticks + c2 * rls_drift_prediction_ticks`
- `m_liq = local_depth + recent_trade_volume`
- `m_imp = 1 / max(m_liq, eps)`
- `physics_shift = clip(a * v_slow * norm(m_liq) + b * v_fast * norm(m_imp))`

## D. Lead-Lag / Shadow-Quote Strategy

Main job:

- move follower quotes when a leader symbol moves first

Best feature families:

- leader L1 mid drift
- leader OFI/OBI and VOI
- leader queue depletion / touch fade
- lead-lag RLS coefficient or transfer-entropy score
- follower's own local liquidity and queue quality

What these should control:

- follower center shift
- follower near-side width/size adjustments
- confidence weight on the shadow signal

Important rule:

- lead-lag should be a capped overlay on follower fair value, not a separate
  independent quote engine fighting the base MM

## E. Pairs / StatArb Market Making Overlay

Main job:

- quote both legs while leaning from a spread residual and keeping net exposure
  closer to market neutral

Best feature families:

- online beta / hedge ratio from RLS
- spread residual and residual z-score
- half-life / mean-reversion confidence
- pair inventory and group delta
- pair-level toxicity or divergence flags

What these should control:

- opposing quote skew on A and B when residual is stretched
- pair-level allocation weight
- group inventory penalty rather than only per-symbol inventory penalty

Important implementation split:

- single-symbol MM still computes each leg's local microstructure quote
- pair overlay adds a residual-based fair-value/skew correction and a
  group-risk modifier

## F. Synthetic-Mid / Basket Fair-Value Overlay

Main job:

- replace a noisy one-symbol center anchor with a basket-consensus anchor for
  highly correlated symbols

Best feature families:

- volume-weighted basket mid
- basket basis in ticks: `own_mid - synthetic_mid`
- basket deviation z-score
- stale constituent mask / weight haircut
- cross-sectional dispersion

What these should control:

- fair-value center correction
- symbol allocation weight under large idiosyncratic dislocations
- regime confidence if one symbol diverges from basket consensus

Useful feature ideas:

- `synthetic_mid`
- `synthetic_mid_basis_ticks`
- `basket_basis_zscore`
- `basket_dispersion`
- `stale_constituent_fraction`

## G. Cross-Symbol Inventory Management

Main job:

- evaluate risk by group net delta / group gross, not only standalone symbol
  inventory

Best feature families:

- group hedge weights / beta
- group net delta
- group gross exposure
- residual unhedged inventory
- correlation confidence / topology stability

What these should control:

- reduce or soften per-symbol inventory skew when exposure is hedged by another
  leg
- increase group-level skew/flatten pressure only when net group exposure is
  truly large
- modify allocation across symbols in the same group

Important rule:

- group-aware inventory can soften quoting behavior, but hard per-symbol and
  gross caps in `src/risk/` should still remain authoritative

## H. Information-Theoretic Confidence / Regime Layer

Main job:

- measure whether book/flow state is concentrated, noisy, redundant, or
  informative, then use that as a confidence weight

Best feature families:

- depth entropy across L1-Lk
- OFI / pressure / return-sign entropy
- KL / JS divergence from a rolling baseline
- mutual information between imbalance and future returns
- mutual information between queue/depth state and fills
- transfer entropy from leader to follower symbols
- Fisher-style local sensitivity scores

Where these belong:

- mostly in slow or warm overlays, not direct tick-by-tick quote placement
- entropy and divergence can modulate width/size confidence
- MI/TE should help decide which alpha channels get trusted, not directly
  throw market orders

Example control rules:

- high flow entropy -> lower directional confidence, wider and smaller quotes
- low depth entropy with strong queue concentration -> stronger touch-joining
  confidence
- high KL/JS divergence from normal regime -> temporary stress widening
- high TE leader->follower -> increase lead-lag overlay weight

## Feature-to-Action Matrix

| Feature family | Best strategy consumer | Primary action knob | Speed |
| --- | --- | --- | --- |
| Local microprice / local OBI / local VOI | single-symbol MM, ladder MM | center shift, side width/size tilt | fast |
| Queue ahead/share, queue decay, latency haircut | ladder MM, single-symbol MM | replace/keep, level size, fill probability | fast |
| Spread / sigma / spread z-score | CJ/GLFT MM | half-width, quote-age limit | fast to warm |
| Toxicity / markout / VPIN | MM, taker exits | width up, size down, reduce touch joining | warm |
| Fill arrival time / passive ratio / shortfall | MM, ladder MM | GLFT lambda, allocation, replace aggressiveness | warm |
| RLS drift prediction | physics momentum, MM overlay | small bounded center shift | warm |
| Depth entropy / flow entropy | info-confidence layer | confidence, width/size dampening | warm |
| MI / TE / Fisher sensitivity | lead-lag, feature allocator | signal weighting, overlay enablement | slow |
| Synthetic basket mid / basis | synthetic-mid overlay | center correction, allocation | warm to slow |
| Pair residual / beta / half-life | stat-arb MM overlay | opposing A/B skew, pair allocation | warm to slow |
| Group net delta / group gross | cross-symbol inventory | inventory skew multiplier, group allocation | warm |
| Session progress / close urgency / BP | all strategies | flatten, size caps, participation enablement | slow + hard safety |

## Build Priority

For our current system, the practical implementation order should be:

1. latency-aware queue features and multi-level ladder controls
2. depth entropy + flow entropy as confidence dampeners
3. physics-informed single-symbol momentum overlay
4. synthetic-mid basket fair-value overlay
5. cross-symbol inventory groups
6. lead-lag shadow quote
7. pair residual MM overlay
8. MI / TE / Fisher weighting as slower research-driven overlays

Reason:

- items 1-3 improve the current single-symbol engine directly
- items 4-7 add cross-symbol structure after the one-symbol loop is stable
- item 8 is most research-heavy and should be validated offline before it
  drives live quote decisions
