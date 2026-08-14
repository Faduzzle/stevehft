# Featurespace Guide

## Purpose

`src/data/featurespace/` is the home for feature discovery, feature metadata, and feature compute design.

This subfolder exists because the feature problem is large enough to deserve its own architecture.

We need to answer two different questions:

1. What transformations are worth having?
2. What is the best way to compute them in a live low-latency system?

Those questions should be handled explicitly rather than buried inside strategy code.

## Main Design Rule

Do not implement a large feature library before writing down the feature catalog.

The right order is:

1. define candidate features
2. classify them by family and latency tier
3. define dependencies and lookbacks
4. choose a production-safe subset
5. implement efficient compute paths

See also:

- [catalog.md](/home/faduzzle/projects/stevehft/src/data/featurespace/catalog.md)
- [V1_FEATURE_SELECTION.md](/home/faduzzle/projects/stevehft/src/data/featurespace/V1_FEATURE_SELECTION.md)
- [ONLINE_ALGORITHMS.md](/home/faduzzle/projects/stevehft/src/data/featurespace/ONLINE_ALGORITHMS.md)

## Horizon Principle

The short windows in the V1 docs are only the first quoting windows.

They are not meant to imply that the full feature system stops at `500ms` or even `5s`.

We should support multiple horizon bands:

- microstructure:
  - `50ms`, `100ms`, `250ms`, `500ms`
- short decision:
  - `1s`, `5s`, `15s`, `30s`
- intraday context:
  - `1m`, `5m`, `15m`, `30m`
- long intraday regime:
  - `1h`, `2h`, `3h`

Those bands have different jobs:

- microstructure windows drive quoting, queue logic, and stale-order control
- short decision windows stabilize flow, fills, and realized-vol estimates
- intraday context windows support pacing, symbol ranking, and slower inventory bias
- long intraday regime windows support covariance, Hurst, recovery, cross-symbol structure, and path-dependent overlays

Three-hour windows are very reasonable for slow-path regime and allocation context.
They are usually not the right default for direct quote placement.

## Feature Families

### Raw Features

These come directly from the market-data cache or execution state.

Examples:

- best bid and ask
- bid and ask size
- local and global top-of-book
- spread
- mid
- inventory
- open-order state

### Linear Features

These are simple combinations or residuals with strong interpretability.

Examples:

- rolling return
- moving average deviation
- weighted spread
- linear residual between two symbols
- beta-adjusted relative move

Use case:

- best baseline feature family for production
- usually easiest to update incrementally

### Nonlinear Features

These apply nonlinear transformations to otherwise simple inputs.

Examples:

- clipped imbalance transforms
- interaction terms
- threshold functions
- rank transforms
- volatility-conditioned score maps

Use case:

- useful after the linear baseline exists
- should stay simple unless proven valuable

### Model-Based Features

These come from fitted or stateful models.

Examples:

- rolling regression residuals
- latent fair-value estimates
- Kalman-like state updates
- fill-probability scores
- execution-cost estimates
- Hawkes expected-arrival estimates
- Hawkes-derived trading-cost proxies

Use case:

- often useful, but must be compute-disciplined
- usually warm-path or slow-path first

### Cross-Symbol Features

These need the broader universe, not just one symbol.

Examples:

- relative return ranking
- leader-laggard scores
- sector-relative dispersion
- symbol quality scores
- allocation priorities

Use case:

- cross-symbol overlays
- allocation logic
- pacing toward the daily trade target

Representative examples that are especially useful here:

- Ledoit-Wolf covariance or correlation estimates
- shrinkage-based dependency summaries
- relative-strength and cross-impact state derived from stable covariance structure

### Order-Flow And Toxicity Features

These model whether current flow is toxic or favorable for passive trading.

Examples:

- VPIN-style toxicity summaries
- signed-flow imbalance summaries
- toxicity regime flags
- adverse-selection risk scores

Use case:

- passive quote suppression
- quote widening
- inventory accumulation control

### Arrival And Cost Features

These model expected event arrivals and the cost of trading under current conditions.

Examples:

- Hawkes-style expected arrival intensity
- fill clustering intensity
- expected passive fill rate
- expected aggressive trading cost

Use case:

- quote-distance selection
- quote size selection
- passive-versus-aggressive choice
- execution urgency adaptation

### Information-Theoretic And Regularization Features

These help compare distributions, regimes, feature stability, and signal quality.

Examples:

- entropy of order-flow or book-state distributions
- KL-style divergence between current and reference distributions
- Jensen-Shannon style symmetric divergence
- Fisher-information-style local sensitivity measures
- surprise or self-information scores
- mutual information between state variables
- transfer entropy for directional information flow
- surprise or information content scores
- regularized similarity scores between current and historical states

Use case:

- compare current market state to historical regimes
- detect when feature distributions are unstable or regime-shifted
- penalize overly noisy or unstable model inputs
- score how informative a state appears before trusting a model output
- detect shared information between features
- detect directional predictive flow across symbols or state channels

### Persistence, Recovery, And Online-Moment Features

These help characterize whether the market is reverting, trending, stabilizing, or structurally changing.

Examples:

- recovery half-life after shocks
- Hurst-style persistence or anti-persistence summaries
- Welford online mean, variance, and z-score state
- rolling or filtered recovery-speed estimates

Use case:

- detect trend versus mean-reversion
- stabilize volatility and z-score estimation
- measure how quickly price or book state recovers after disturbance
- feed regime logic and quote-width adaptation

## Feature Metadata Standard

Every serious feature should eventually have metadata with:

- feature name
- feature family
- intuition
- required inputs
- lookback
- update cadence
- latency tier
- consumer module
- output type
- production status

For toxicity and arrival features, metadata should also clarify:

- event source
- bucket or window type
- whether the output is safe for hot path, warm path, or slow path
- whether the output is directly consumed or converted into a compact score

For information-theoretic or regularization features, metadata should also clarify:

- what reference distribution or baseline is being compared against
- whether the comparison is rolling, anchored, or regime-conditional
- whether the feature compares price, depth, flow, fills, or model residuals
- whether the output is diagnostic-only or allowed into live decision logic

This metadata matters because feature systems become chaotic quickly without it.

## Online Algorithm Toolbox

Feature choice and online-algorithm choice should be related, but not fused too early.

Examples of useful online algorithm families for this repo:

- P-square for quantiles
- Misra-Gries for dominant discrete states
- recursive least squares for adaptive linear relationships
- EWMA covariance for intraday dependence
- Ziv-Lempel style complexity for discretized sequence novelty
- CUSUM and Page-Hinkley for drift and regime breaks
- reservoir sampling for bounded diagnostics
- O'Neill online skewness for asymmetry
- decaying Welford for intraday adaptive baselines

See [ONLINE_ALGORITHMS.md](/home/faduzzle/projects/stevehft/src/data/featurespace/ONLINE_ALGORITHMS.md) for where each one is likely to fit.

## Compute Tiers

### Tier 0: Raw State

- direct inputs
- must always be available
- no expensive transformation

### Tier 1: Hot Path

- safe every loop
- incremental only
- small bounded math

Examples:

- spread in ticks
- microprice
- imbalance
- quote age

### Tier 2: Warm Path

- refreshed periodically
- rolling-state friendly
- still live-trading safe

Examples:

- short realized vol
- fill-rate estimate
- short-window z-score
- shallow-depth slope
- VPIN toxicity summary
- compact Hawkes state summary
- compact entropy or divergence summary
- rolling information-content score

### Tier 3: Slow Path

- heavier model or cross-symbol work
- refreshed on snapshots
- publishes compact outputs back to live code

Examples:

- residualization
- regressions
- state-space outputs
- cross-symbol ranking maps
- fitted Hawkes parameter refresh
- richer toxicity-model recalibration
- Fisher-information-style sensitivity summaries
- richer divergence-map recalibration versus historical templates

## Rolling Memory Design

Feature computation will need rolling memory at multiple horizons.

The right design is to support:

- tick-based windows
- time-based windows
- exponentially weighted state where exact windows are unnecessary

Information-theoretic features often need one more concept:

- reference windows or reference baselines

These may be:

- recent rolling baseline
- session-start baseline
- historical profile by symbol and time-of-day
- regime-specific template

### Tick Windows

Use for:

- last `N` mid changes
- last `N` imbalance values
- last `N` fills
- last `N` spread observations

Preferred implementation:

- ring buffers
- running aggregates when possible

### Time Windows

Use for:

- last `100ms`
- last `1s`
- last `5s`
- last `1m`

Preferred implementation:

- timestamp-aware bounded buffers
- expiry on insert
- running summaries for common moments

### Exponentially Weighted State

Use for:

- smooth volatility estimates
- persistent microstructure drift
- fill-rate or participation intensity

Why:

- compact memory
- constant-time updates
- often good enough for live trading

## Database And Persistence Philosophy

Persistent storage is important, but it is not part of the hot path.

Use persistent storage for:

- session replay
- research datasets
- feature diagnostics
- model validation
- post-trade review

Do not use persistent storage for:

- live per-tick feature retrieval
- order-loop decisions
- synchronous joins during trading

The live engine should read from memory and write asynchronously to history.

Information-theoretic comparisons are especially useful offline and warm-path because they often compare current state to a stored reference distribution or template.

## Snapshot Versus Delta Design

For feature systems, use:

- snapshots for current compact feature outputs
- rolling stores for bounded feature memory
- delta or event records for historical growth

Feature snapshots should contain only the latest values needed by readers now.

They should not contain:

- rolling arrays
- growing event traces
- full feature history

## Vectorization Strategy

Vectorization is valuable in the feature layer, but only at the right cadence.

Good uses:

- cross-symbol snapshot transforms
- batch feature recomputation
- offline analysis
- ranking and residualization
- periodic Hawkes-state refreshes
- batch toxicity or arrival recalibration
- entropy and divergence calculations against rolling or historical baselines
- Fisher-information-style sensitivity comparisons across feature states

Bad uses:

- tiny scalar updates that happen every event
- code paths that need extremely tight latency and low allocation

Best practice:

- incremental scalar math in the hot path
- vectorized recomputation in warm and slow paths

## Concurrency Guidance

Feature concurrency should follow a simple pattern:

- one writer owns raw and rolling symbol-local state
- readers consume compact published feature outputs
- cross-symbol features publish compact overlays on a slower cadence
- persistent history is append-only and async

### Ring Buffers

Ring buffers are the preferred primitive for bounded rolling feature memory.

Why:

- fixed-size
- deterministic update cost
- natural fit for tick-based history

### Seqlocks And Lock-Free Structures

Do not start by designing around seqlocks or exotic lock-free structures here.

The better default is:

- single-writer ownership
- bounded private mutable state
- compact snapshot publication
- append-only event logs

If lower-level optimization is needed later, we can revisit specialized structures with profiling in hand.

## Hybrid Snapshot Plus Delta Transport

For feature aggregation and cross-symbol coordination, a hybrid snapshot plus delta transport can be very effective.

The pattern is:

- private mutable writer state
- compact deltas for incremental consumer updates
- periodic compact snapshots for recovery and current truth

### Best Uses

Use this pattern for:

- cross-symbol feature aggregation
- warm-path feature workers
- asynchronous consumer pipelines that only need compact updates

### Not A Replacement For Rolling Memory

Do not push rolling feature buffers through transport queues.

Rolling buffers should remain:

- bounded
- private
- writer-owned

Transport should carry:

- compact changes
- compact current-state snapshots

### Message Philosophy

A good delta message should contain only the minimum required change.

A good snapshot message should contain only the current compact state needed for resync.

Neither should contain:

- full rolling buffers
- growing histories
- large internal working arrays

### Why This Helps

This pattern gives us:

- cheap incremental aggregation
- bounded transport cost
- recovery without replaying an entire session
- clear separation between compute state and reader state

## Lookup Table Strategy

Lookup tables are useful for stable nonlinear transforms that are called often.

Good candidates:

- imbalance bucket to quote bias
- inventory bucket to skew amount
- spread bucket to quote width
- time-to-close bucket to urgency
- toxicity bucket to passive-participation multiplier
- arrival-rate bucket to quote-distance adjustment

Questions to ask before using a LUT:

1. Is the input range bounded and easy to bucket?
2. Is the transform stable enough to precompute?
3. Is approximation error acceptable?
4. Is a direct formula actually slower in practice?

If the answer to those questions is yes, a LUT can be a clean latency optimization.

## Information-Theoretic And Regularization Design Notes

These tools are useful, but they should be placed carefully.

### Fisher Information Style Features

Use these for:

- measuring local sensitivity of a modeled distribution or likelihood to parameter changes
- comparing how informative one market state is versus another
- detecting when the current state provides weak versus strong identification for a model

Likely live use:

- warm-path compact score
- model-confidence modifier
- feature-quality or regime-quality filter

Best placement:

- warm or slow path
- often downstream of fitted or estimated models

### Recovery Features

Use these for:

- measuring how quickly price, spread, or book state returns after a shock
- distinguishing persistent dislocations from transient ones
- deciding whether current dynamics favor mean reversion or follow-through

Examples:

- price recovery half-life
- spread normalization half-life
- depth recovery speed

Likely live use:

- warm-path regime modifier
- trending versus mean-reversion classifier input

### Hurst-Style Features

Use these for:

- estimating persistence versus anti-persistence in price or feature paths
- distinguishing trending behavior from mean-reverting behavior
- adding path-structure context to volatility and momentum signals

Examples:

- Hurst estimate on price changes
- Hurst estimate on microprice drift
- Hurst estimate on imbalance or OFI paths

Likely live use:

- warm-path persistence score
- regime filter

### Welford Online-Moment Features

Use these for:

- online mean, variance, and standard deviation estimation
- stable z-score construction
- low-cost rolling or expanding moment updates

Examples:

- Welford mean of price
- Welford variance of returns
- Welford-based realized volatility

Likely live use:

- hot or warm-path statistical baseline
- low-cost normalization for other features

### Entropy Features

Use these for:

- measuring concentration versus dispersion in depth, flow, or state occupancy
- detecting whether liquidity is concentrated or fragmented
- summarizing uncertainty in observed state distributions

Examples:

- depth entropy across levels
- signed-flow entropy across buckets
- event-type entropy across recent arrivals

Likely live use:

- compact regime score
- optional width or participation modifier

### Surprise Features

Use these for:

- measuring how unexpected the current observation is under a rolling or historical reference distribution
- detecting unusual market states quickly
- flagging states where model assumptions may be breaking

Examples:

- surprise of current spread regime
- surprise of current depth shape
- surprise of current signed-flow pattern

Likely live use:

- warm-path anomaly score
- quote suppression or widening modifier under extreme surprise

### Mutual Information Features

Use these for:

- measuring shared information between two variables or feature streams
- identifying whether two candidate signals contain overlapping information
- ranking which state variables are most informative about fills, price moves, or regime labels

Examples:

- mutual information between imbalance and short-term returns
- mutual information between depth pressure and fill outcomes
- mutual information between symbol-local and cross-symbol signals

Likely live use:

- mostly slow-path or warm-path model-selection support
- compact redundancy or usefulness score

### Transfer Entropy Features

Use these for:

- measuring directional information flow from one process to another
- identifying whether one symbol or state channel leads another
- supporting leader-laggard and cross-impact style overlays

Examples:

- transfer entropy from leader symbol returns to follower symbol returns
- transfer entropy from order-flow state to fill state
- transfer entropy from one side of the book to subsequent price response

Likely live use:

- slow-path or warm-path directional influence score
- cross-symbol overlay input

### Divergence Features

Use these for:

- comparing current distribution to recent or historical reference states
- detecting regime change
- deciding whether model assumptions still resemble the data we calibrated on

Examples:

- KL-style divergence from rolling baseline
- Jensen-Shannon divergence from time-of-day template
- divergence between current and recent depth profile

Likely live use:

- warm-path regime-change detector
- trust or distrust modifier for model outputs

### Regularization-Oriented Comparison Features

Use these for:

- shrinking noisy comparisons toward stable baselines
- penalizing unstable model outputs
- comparing multiple candidate signals while discouraging overreaction to noise

Examples:

- shrinkage-adjusted similarity scores
- regularized covariance or correlation estimates
- penalty-weighted model-confidence score
- stabilized residual or divergence score

Likely live use:

- model-confidence overlays
- selection among competing signals or strategies
- gating advanced extensions when state comparisons are too noisy

### Best Practical Rule

Most information-theoretic and regularization-heavy features should begin as:

- research features
- slow-path or warm-path summaries
- compact overlays into live strategy logic

They should not begin life as hot-path per-event computations unless they prove both valuable and cheap.

## Compute Design Questions

For every feature, ask:

1. Can it be updated incrementally?
2. Does it need full history?
3. Can a ring buffer or running-stat update replace full recomputation?
4. Is the feature per-symbol or cross-symbol?
5. Does the strategy need the raw transform or only a compact score?
6. What cadence is actually necessary?

## Recommended Files

### `catalog.md`

Human-readable inventory of all planned features.

### `registry.py`

Machine-readable feature metadata and registration.

### `rolling.py`

Reusable rolling-window utilities, ring buffers, and incremental statistics.

### `linear.py`

Cheap linear transforms intended to be the first production-ready family.

### `nonlinear.py`

Simple nonlinear transforms and interaction logic.

### `model_based.py`

Stateful or fitted transforms that need tighter compute discipline.

### `cross_symbol.py`

Transforms that depend on multiple symbols or portfolio-wide state.

### `selectors.py`

Logic for selecting the V1 live subset from the broader catalog.

## Productionization Strategy

The correct way to productionize features is:

1. start with a broad candidate catalog
2. identify the smallest useful subset
3. implement only the subset needed for the first live strategy
4. measure compute cost
5. add heavier families only when their value is clear

## Context For Future Work

If the question is "what should we compute" or "how can we compute this without breaking latency," this folder should contain the answer.
