# Online Algorithms Reference

## Purpose

This document is the algorithm toolbox for the live feature system.

It does not decide which features belong in V1.
It answers a different question:

- when we need an online statistic or compact streaming summary, which algorithm family should we reach for
- what kind of market state is each algorithm good for
- what live-trading constraints should shape the implementation

The idea is:

1. define candidate features
2. define candidate online algorithms
3. later bind a feature to a specific algorithm and parameterization

## General Design Rule

Use online algorithms to maintain compact state, not to hide feature ambiguity.

Each algorithm should eventually be documented with:

- what state it stores
- whether it is bounded, expanding, or decaying
- whether it is tick-based, time-based, or event-based
- whether it resets daily or carries intraday decay
- what numerical failure modes or warm-up issues it has

## Intraday Design Preference

For this system, many algorithms should be intraday-aware rather than permanently expanding.

Good intraday patterns:

- reset at session start
- bounded rolling state
- exponentially decayed state
- anchored-within-session state

Bad default:

- unbounded accumulation across many sessions unless the output is explicitly intended to be multi-day

## Algorithm Families

### P-Square

Use for:

- online quantile estimation without storing full history
- spread quantiles
- quote-age quantiles
- fill-latency quantiles
- queue-age quantiles
- intraday distribution cutoffs for regime flags

Good applications here:

- `spread_quantile`
- `quote_age_p90`
- `fill_latency_p50`
- `fill_latency_p90`
- `queue_age_p95`
- `trade_size_quantile`

Why it is useful:

- small memory footprint
- no need to store full rolling history
- good for thresholding and adaptive guards

Live cautions:

- warm-up matters
- can lag under sudden distribution shifts
- should usually be session-reset or decayed by session phase rather than indefinitely expanding

Intraday recommendation:

- session-reset by default
- consider separate estimators per session segment if morning and midday behavior differ materially

### Misra-Gries

Use for:

- heavy hitters in streaming categorical or discretized state
- most common spread buckets
- most common regime labels
- dominant order-flow sign states
- dominant topological or leader-laggard states

Good applications here:

- most frequent spread bucket
- dominant regime code over recent intraday window
- dominant quote outcome class
- frequent event type tracking
- common destination or venue state if relevant later

Why it is useful:

- compact approximate frequency tracking
- useful when exact histograms are too expensive or unnecessary

Live cautions:

- only tracks frequent categories well
- requires sensible discretization before use
- not a replacement for continuous-valued summaries

Intraday recommendation:

- use with regime buckets, spread buckets, toxicity states, or quote outcome labels
- usually bounded or periodically reset, not permanently expanding

### Recursive Least Squares

Use for:

- online linear relationship estimation
- adaptive hedge ratios
- local return prediction from flow or imbalance
- cross-symbol beta estimation
- adaptive fair-value residualization

Good applications here:

- `imbalance_to_return_beta`
- `microprice_to_short_return_beta`
- `leader_symbol_to_follower_symbol_beta`
- `inventory_pressure_to_fill_rate_beta`
- adaptive linear fair-value residual

Why it is useful:

- fast online update for linear models
- more adaptive than batch regression
- useful for local relationships that drift intraday

Live cautions:

- can become unstable without forgetting or regularization
- sensitive to collinearity and poorly scaled inputs
- should remain low-dimensional in live use

Intraday recommendation:

- use forgetting-factor RLS, not naive expanding RLS
- reset or strongly decay across session boundaries
- reserve for warm-path or slow-path first

### EWMA Covariance

Use for:

- adaptive covariance and correlation tracking
- cross-symbol dependence
- volatility and co-movement state
- intraday portfolio stress summaries

Good applications here:

- `ewma_cov_returns`
- `ewma_corr_returns`
- `ewma_volatility`
- cross-symbol pressure and allocation overlays
- covariance input to shrinkage or risk throttling

Why it is useful:

- cheap incremental updates
- naturally recency-weighted
- much more practical live than repeated rolling full covariance recomputation

Live cautions:

- choice of decay matters a lot
- noisy for thin or low-activity symbols
- can overweight bursty periods if decay is too short

Intraday recommendation:

- one of the best defaults for intraday covariance
- use multiple horizons such as `1m`, `5m`, `15m`, `1h`
- combine with shrinkage for cross-symbol overlays

### Online Roll Model

Use for:

- microstructure-noise estimation
- bid-ask bounce estimation
- online friction or effective spread proxy
- clean-price extraction from observed transaction prices

Core idea:

- estimate short-horizon negative return autocovariance
- map that into a Roll-style friction or bounce parameter
- use the resulting `c_t` as a dynamic microstructure-cost state

Useful stylized form:

```text
c_t ~= f(-Cov(Delta P_t, Delta P_{t-1}))
```

and a clean-price style adjustment:

```text
m_t = P_t - q_t * c_t
```

where:

- `P_t` is observed trade or transaction price
- `q_t` is trade direction or signed pressure proxy
- `c_t` is the dynamic bounce / friction / adverse-selection parameter
- `m_t` is the cleaned or microstructure-adjusted price estimate

Use it for:

- cleaning fair-value anchors
- separating bid-ask bounce from true directional movement
- detecting when mean-reverting bounce is strong versus when informed trending flow is dominating

Good applications here:

- `roll_cov_1`
- `roll_c_t`
- `roll_clean_price`
- `roll_bounce_ratio`
- `roll_toxicity_sensor`

Why it is useful:

- directly tied to market microstructure
- practical way to distinguish bounce-driven noise from cleaner directional price movement
- naturally useful for both market-making and allocation logic

Live cautions:

- classical Roll assumptions are stylized and incomplete
- the raw covariance estimate is noisy intraday
- should usually be smoothed or estimated with online covariance plus guardrails
- `c_t` should be treated as an adaptive proxy, not a perfect spread decomposition

Intraday recommendation:

- use online or decayed covariance of short returns
- smooth `c_t` with EWMA or decayed moments
- treat `c_t` as dynamic state, not a single constant
- compare `c_t` against total spread or realized spread to infer bounce-versus-trend regime

### Ziv-Lempel Style Complexity / Compression Score

Use for:

- online-ish complexity or novelty summaries of discretized state sequences
- sequence unpredictability
- change in structural repetitiveness of order-flow or regime states

Good applications here:

- complexity of discretized spread states
- complexity of imbalance sign sequence
- complexity of regime-code sequence
- novelty of leader-laggard state transitions

Why it is useful:

- gives a different view than entropy alone
- can highlight when state sequences become unusually structured or unusually irregular

Live cautions:

- works best on discretized sequences, not raw floats
- direct hot-path use is unlikely to be worthwhile
- better as warm-path or research overlay than as a per-tick feature

Intraday recommendation:

- use on bounded windows or sampled state sequences
- good for regime diagnostics and structural-change overlays

### CUSUM / Page-Hinkley

Use for:

- online change detection
- drift detection
- detecting shifts in spread, toxicity, fill behavior, residuals, or latency

Good applications here:

- spread regime breaks
- fill-rate deterioration
- quote-rejection surge
- strategy residual drift
- latency degradation
- microprice residual shift

Why it is useful:

- directly actionable for regime change and safe-mode triggering
- much more operational than many heavier statistical diagnostics

Live cautions:

- thresholds need careful tuning
- too sensitive creates alert spam and mode thrash
- should distinguish transient spikes from persistent drift

Intraday recommendation:

- very useful for live health and regime monitoring
- especially good for:
  - stale or broken fill-quality state
  - spread/volatility regime shifts
  - execution-quality deterioration

### Reservoir Sampling

Use for:

- maintaining a representative bounded sample from a long stream
- later diagnostics or approximate distribution estimation
- offline replay subsets without logging everything

Good applications here:

- representative sample of fills
- representative sample of book states
- representative sample of quote outcomes
- representative sample of spread or imbalance states for later diagnostics

Why it is useful:

- bounded memory
- useful for telemetry, research, and post-trade analysis
- avoids full-history retention for every stream

Live cautions:

- not the right primitive for hot-path decision variables
- better for diagnostics and research support
- uniform reservoir may miss recent-state emphasis unless modified

Intraday recommendation:

- use for telemetry, model-validation samples, and offline inspection
- if recency matters, pair with time-segmented reservoirs instead of one all-day reservoir

### O'Neill Online Skewness

Use for:

- online third-moment estimation
- asymmetry of returns, spread changes, fill latencies, or toxicity scores

Good applications here:

- return skewness
- microprice change skewness
- fill-latency skewness
- spread-change skewness
- imbalance-change skewness

Why it is useful:

- adds asymmetry information beyond variance
- useful for regime context and abnormal-tail behavior

Live cautions:

- noisy on short samples
- should not be overtrusted early in the session
- better as warm or slow context than direct quote-control input

Intraday recommendation:

- use with bounded or decayed updates
- likely more useful for regime scoring and diagnostics than for direct hot-path quoting

### Decaying Welford

Use for:

- online mean and variance with recency weighting
- intraday adaptive baselines
- volatility and z-score style normalization with forgetting

Good applications here:

- decayed mid-price baseline
- decayed return variance
- decayed spread mean and variance
- decayed fill-rate baseline
- decayed quote-age baseline

Why it is useful:

- Welford-style stability is attractive
- decay makes it much more intraday-appropriate than pure expanding moments

Live cautions:

- exact formulation must be chosen carefully
- interpretation differs from classical unbiased moments
- should be treated as adaptive baseline, not textbook sample-statistic output

Intraday recommendation:

- strong candidate for many live normalization tasks
- often better than permanently expanding Welford for intraday strategies

## What To Apply Them To

The best mapping for this system is:

### Distribution And Threshold Features

Apply:

- P-square
- decaying Welford
- O'Neill skewness

To:

- spread
- quote age
- fill latency
- fill size
- return magnitude
- depth pressure

### Streaming Category / State Dominance

Apply:

- Misra-Gries
- Ziv-Lempel style sequence complexity

To:

- spread buckets
- regime labels
- toxicity states
- sign-of-flow states
- quote outcome labels

### Adaptive Linear Relationship Features

Apply:

- recursive least squares

To:

- imbalance to return
- microprice drift to return
- leader-laggard symbol relations
- fair-value residualization
- fill-rate response to quote distance or queue state

### Regime And Health Change Detection

Apply:

- CUSUM
- Page-Hinkley

To:

- spread regime shifts
- realized-vol regime shifts
- fill-rate deterioration
- latency deterioration
- model residual drift
- quote rejection or cancellation anomalies

### Cross-Symbol Dependency Tracking

Apply:

- EWMA covariance
- shrinkage around EWMA covariance later if needed

To:

- multi-symbol returns
- symbol quality scores
- portfolio pressure summaries
- cross-impact overlays
- allocation and exposure throttling

### Telemetry And Research Sampling

Apply:

- reservoir sampling

To:

- fills
- quote outcomes
- book snapshots
- exceptional events
- candidate feature observations for later review

## Hot / Warm / Slow Guidance

Good hot or near-hot candidates:

- decaying Welford
- simple EWMA covariance inputs at small dimension
- pre-thresholded change detectors if already maintained incrementally

Good warm-path candidates:

- P-square
- recursive least squares
- O'Neill skewness
- CUSUM / Page-Hinkley

Good slow-path or research-first candidates:

- Ziv-Lempel style complexity
- reservoir-sampled diagnostic layers
- larger cross-symbol covariance structures

## Strong Recommendations

The strongest immediate candidates for this project are:

1. decaying Welford for intraday normalization and variance baselines
2. EWMA covariance for cross-symbol dependence
3. CUSUM / Page-Hinkley for regime and health breaks
4. P-square for spread, latency, and queue-age quantiles
5. recursive least squares for a small number of adaptive linear relationships

These are practical, interpretable, and naturally useful in a live intraday system.

## Implementation Note

Do not bind these algorithms to exact feature formulas prematurely.

The right workflow is:

1. create or refine a feature candidate
2. choose whether it needs:
   - exact rolling history
   - a decayed online estimate
   - a compact streaming summary
   - a change detector
   - a sampled diagnostic layer
3. then select the algorithm that fits that need

That keeps the feature space and the compute space cleanly separated.
