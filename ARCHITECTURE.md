# SHIFT HFT Architecture

## Purpose

This document is the repository-level architecture map for the new SHIFT HFT system.

It complements [BUILD.md](/home/faduzzle/projects/stevehft/BUILD.md):

- `BUILD.md` explains what we are building and in what order.
- `ARCHITECTURE.md` explains how the repository is organized, how components interact, and what concurrency model we should use.
- `PRE_LIVE_CHECKLIST.md` tracks the remaining work and live-smoke readiness checks.

Everything under `Old model/` remains deprecated and is not part of this architecture.

## Repository Layout

```text
stevehft/
  ARCHITECTURE.md
  BUILD.md
  credentials.py
  initiator.cfg
  wiki/
  src/
    README.md
    core/
      README.md
    data/
      README.md
    execution/
      README.md
    risk/
      README.md
    strategy/
      README.md
    telemetry/
      README.md
    app/
      README.md
  tests/
    README.md
```

## Design Goals

The architecture should support:

- low-latency decision making
- safe order and inventory handling
- passive-first execution economics
- reliable end-of-day flattening
- enough throughput to reach `200 trades/day`
- clean separation between hot-path logic and slow-path support work
- survival under asynchronous order-state races

## Integration-First Principle

We should build the repository backwards from the SHIFT interface.

That means the earliest trustworthy modules are:

- session and connection management
- market-data normalization
- order routing
- reconciliation
- portfolio-state recovery
- safe-mode and shutdown behavior

Not:

- deep alpha models first
- large feature stacks first
- online allocation sophistication first

The strategy only becomes meaningful after those boundaries are reliable.

## State Authority

The system may maintain fast local expectations, but authoritative state comes from broker-facing reconciliation.

In practice:

- market data is authoritative for current visible market state
- waiting-list and execution polls are authoritative for order lifecycle state
- portfolio and buying-power polls are authoritative for exposure and capital state

This matters because cancel requests, fills, and portfolio updates do not arrive as one atomic event.

The architecture must assume:

- fills can arrive while cancel is pending
- replace is not atomic
- portfolio updates may lag fill discovery
- order disappearance from waiting list does not guarantee execution accounting is finished

## System Shape

The recommended system is a single process with a few carefully chosen threads and strict ownership boundaries.

### Why One Process

For this competition environment, a single-process design is the best default because it:

- avoids IPC overhead
- reduces serialization overhead
- keeps market state local
- makes order ownership easier to reason about
- is simpler to profile and debug

We should only split into multiple processes if measurement shows a real bottleneck that cannot be handled inside one process.

## Strategy Layering

The trading system should support both:

- per-symbol strategy logic
- cross-symbol strategy logic

These are not competing designs. They should be stacked.

### Per-Symbol Layer

This is the hot-path decision engine.

It operates on one symbol at a time and answers:

- should we quote this symbol right now
- what prices should we quote
- what size should we expose
- should we cancel, refresh, or pull quotes

Per-symbol inputs:

- top-of-book
- spread
- microprice
- imbalance
- local inventory
- quote age
- symbol-local fill behavior

Per-symbol output:

- a local order intent for that symbol

### Cross-Symbol Layer

This is the portfolio coordination layer.

It operates on the full tradable universe and answers:

- which symbols should get more or less capital
- which names are currently highest quality for passive participation
- whether correlated names should have tighter exposure caps
- whether one symbol's move should change how we quote another
- whether we are behind pace for `200 trades/day` and need broader participation

Cross-symbol inputs:

- shared market snapshot
- relative performance across symbols
- spread and fill-quality ranking
- sector or factor group behavior if we define groups
- session trade count and fee or rebate estimates
- portfolio exposure and buying-power usage

Cross-symbol output:

- overlays, rankings, weights, and enable or disable flags

### Combination Rule

The correct relationship is:

- per-symbol strategy generates candidate intents
- cross-symbol logic modifies priority, size, skew, enablement, and risk budget

Cross-symbol logic should usually not generate raw exchange actions directly.

## Microstructure Constraints

The architecture should treat market microstructure as a constraint layer, not a post-hoc explanation.

### NBBO / Best-Market Awareness

Every executable decision should be made relative to the current best visible bid and ask.

That means:

- quote placement is relative to the touch, not absolute price alone
- joining, improving, or stepping away from the best market is an explicit decision
- crossing the spread must be recognized as an aggressive action with explicit cost

### Dual-Book Signal Contract

SHIFT gives us three distinct market views, and they should not be mixed casually:

- `Global` is effectively an L1 simulated reference process from `get_best_price()`.
- `Local` is the full-depth competitive book from `get_order_book(LOCAL_BID/ASK)`, with our own orders filtered out.
- `NBBO` / best touch is the actionable merged top-of-book used for quote placement and spread gating.

That means:

- all multi-level depth, concentration, front-shape, and queue-position features should be computed from the cleaned local book
- global L1 should be treated as a slow exogenous drift / imbalance signal over time, not as a fake depth ladder
- fair value should anchor on NBBO mid, use local microprice/local depth imbalance for competitive shape, and optionally add a small smoothed global-drift bias
- fill probability should use cleaned local top-of-book depth plus our own order size, not global L1 size

### Continuous Double Auction Reality

The system lives inside a continuous double auction with price-time priority.

That imposes several architectural truths:

- queue position and queue age matter
- cancel is not instantaneous
- replace is not atomic
- book state can move while orders are in flight
- fills, cancels, and portfolio updates can be observed in non-ideal order

This is why:

- strategy needs queue and fill-quality features
- execution needs one authoritative working-order ledger
- reconciliation must be broker-authoritative and idempotent

### Execution Benchmarks

VWAP and TWAP should exist in the system as benchmark layers, especially for:

- flattening behavior
- emergency market-order usage
- inventory cleanup quality
- comparing realized execution quality to simple baselines

They are not the primary control law for the market maker, but they are useful discipline for evaluation and tuning.

Current implementation:

- `OrderLedger` maintains session-level fill VWAP and fill-price TWAP incrementally
- `build_session_metrics(...)` publishes those benchmarks in `session_metrics` events
- order audits store decision/arrival benchmarks and implementation-shortfall totals
- strategy parameters expose expected passive/aggressive slippage and fold a
  slippage-quality score into allocation

## Hybrid Strategy Flow

Recommended flow:

1. `src/data/` updates local state for every subscribed symbol.
2. per-symbol strategy computes a candidate intent for each symbol.
3. cross-symbol strategy computes portfolio-wide overlays on a slower cadence.
4. a combiner merges local intents with cross-symbol overlays.
5. `src/risk/` validates the merged intents.
6. `src/execution/` turns approved intents into orders and cancels.

This keeps symbol-local responsiveness fast while still letting the portfolio act coherently.

## Implementation Order

The practical implementation order should be:

1. `src/app/` session lifecycle and telemetry bootstrap
2. `src/data/` market-data ingestion and normalized symbol state
3. `src/execution/` order router, ledgers, and reconciliation
4. `src/risk/` safe modes, limits, and flatten behavior
5. `src/telemetry/` audit trail
6. `src/strategy/` simple first market maker
7. deeper features, online algorithms, and allocation overlays

This order is intentional.
It makes the SHIFT-facing contract correct before the strategy depends on it.

## Concurrency Model

Efficient concurrency matters, but more concurrency is not automatically better. The right model is a small number of long-lived threads with narrow responsibilities.

### Recommended Threads

#### 1. Main Thread

Responsibilities:

- process startup
- config loading
- `shift.Trader` creation and connection
- symbol selection and subscription startup
- lifecycle state changes
- shutdown coordination

This thread should not run the hot trading loop.

#### 2. Market Data Thread

Responsibilities:

- poll or refresh subscribed symbol state
- update the in-memory book cache
- stamp each update with `time.monotonic_ns()`
- publish the latest snapshot for strategy consumption

This thread owns writes to market-data state.

#### 3. Strategy / Execution Thread

Responsibilities:

- read the latest symbol state
- compute quote or take decisions
- run per-symbol strategy logic every cycle
- run cross-symbol portfolio logic on a slower cadence
- run pre-trade risk checks
- submit, cancel, and replace orders
- maintain local order and inventory estimates

This thread owns writes to live order state.

Keeping strategy and execution together is intentional at first. It removes cross-thread messaging on the hottest trading path.

Efficient hybrid behavior:

- run per-symbol logic every loop
- run cross-symbol logic every `N` loops or every fixed time slice like `50ms` to `250ms`
- cache the latest cross-symbol overlay and reuse it between refreshes

This avoids introducing another hot-path thread that would fight over shared market state.

#### 4. Reconciliation / Control Loop

This can be:

- a lightweight periodic task inside the strategy/execution thread, or
- a separate slow thread if needed

Responsibilities:

- poll order status
- poll executed fills
- poll portfolio state
- correct local inventory estimates
- update fee and rebate estimates
- track pace toward `200 trades/day`

This loop is the broker-authoritative correction path.
It is not optional bookkeeping.
It is what prevents local expected state from drifting into dangerous fiction.

## Safe Modes

The trading system should have explicit operating modes:

- `normal`
- `degraded_reconcile`
- `position_mismatch`
- `flatten_only`
- `kill_switch`

Meaning:

- `normal`: standard market-making and allocation behavior
- `degraded_reconcile`: broker state is stale or uncertain, reduce risk-taking
- `position_mismatch`: local and broker exposures disagree materially, stop normal quoting
- `flatten_only`: cancel passive risk and work toward neutral inventory
- `kill_switch`: no new risk, cancel what can be canceled, coordinate emergency stop

These modes should be driven by explicit conditions rather than hidden booleans scattered around the code.

### Safe-Mode Action Rules

Each mode must define:

- exact entry conditions
- exact immediate actions
- what order types remain allowed
- what gets canceled immediately
- what condition clears the mode

At a minimum:

- `degraded_reconcile` stops new opening risk and tightens or disables quoting
- `position_mismatch` cancels passive risk and freezes normal strategy output
- `flatten_only` allows only risk-reducing actions toward flat inventory
- `kill_switch` rejects all normal trading activity and preserves only emergency controls

## Failure Modes And Edge Cases

The architecture must directly account for:

- cancel in flight with late fill
- partial fill during replace
- duplicate execution polling
- out-of-order broker observations
- waiting-list removal before fill reconciliation is complete
- stale market data while orders remain live
- restart recovery with open orders and positions
- close-window races near forced liquidation deadlines
- buying-power mismatches from reserved balance or short-close burden

See also [EDGE_CASES.md](/home/faduzzle/projects/stevehft/EDGE_CASES.md).

This loop should run slower than the decision loop.

#### 5. Logger Thread

Responsibilities:

- consume structured events from a queue
- batch log writes
- flush fills, decisions, errors, and state snapshots

This thread must never block trading-critical code.

## Ownership Rules

Concurrency only stays safe if ownership is explicit.

### Market State Ownership

- `src/data/` owns the canonical in-memory market snapshot.
- The market data thread is the only writer.
- Strategy and risk code are readers.

### Order State Ownership

- `src/execution/` owns live order state.
- The strategy/execution thread is the only writer of working-order records.
- Other modules may read snapshots but must not mutate order state directly.

### Inventory and Limits Ownership

- `src/risk/` owns the rules.
- `src/execution/` owns the act of sending or canceling orders.
- Strategy may request actions, but risk and execution decide whether they are allowed and how they are encoded.

### Telemetry Ownership

- `src/telemetry/` owns persistence and metrics output.
- Other modules emit events, but telemetry decides storage format and flush timing.

## Shared-State Strategy

### Default Approach

Use single-writer, many-reader shared structures wherever possible.

Good pattern:

- market data thread writes a flat per-symbol state object
- strategy thread reads that state without rebuilding it
- execution thread mutates only order-specific state

### Preferred Data Shapes

Prefer:

- dataclasses with fixed fields
- arrays or flat symbol-indexed storage
- primitive numeric fields
- monotonic timestamps

Avoid:

- nested dicts in the hot path
- dataframe-based state
- large object graphs
- ad hoc shared mutation from multiple threads

### Locking Guidance

Start simple:

- one lock around market-state publication if needed
- one lock around shutdown state
- avoid broad global locks

If a lock is needed per symbol, that can work, but only if it stays simple and measurable.

The best outcome is usually:

- single writer
- readers consuming the latest stable snapshot
- minimal lock hold times

## Event Flow

### Market Data Flow

1. Session starts and subscribes symbols.
2. Market data thread queries `get_best_price(symbol)` and related state as needed.
3. `src/data/book_cache.py` updates per-symbol state.
4. Strategy thread reads latest state and computes per-symbol intents.
5. Cross-symbol overlay is refreshed periodically and applied before risk checks.

### Trading Flow

1. Per-symbol strategy computes desired local quote or exit action.
2. Cross-symbol logic adjusts enablement, sizing, skew, or urgency.
3. Risk checks validate the merged action.
4. Execution converts the intent into a `shift.Order`.
5. Order is submitted through the `shift.Trader` session.
6. Local order state is updated immediately.
7. Reconciliation loop later confirms fills and final status.

### Telemetry Flow

1. Components emit structured events.
2. Events go into a non-blocking queue.
3. Logger thread batches and writes them.
4. Periodic summaries update trade count, passive ratio, fee estimate, and net session status.

## Performance-Critical Design Choices

### 1. Keep Strategy and Execution Together Initially

This is one of the most important latency choices.

Why:

- avoids queue hops between decision and order submission
- reduces synchronization complexity
- makes per-symbol state easier to reason about

Split them later only if measurement shows they are contending.

This matters even more for hybrid strategy design because:

- per-symbol logic must stay extremely cheap
- cross-symbol logic can reuse the same thread without queue hops
- we avoid building a second strategy scheduler too early

### 2. Separate Market Data Updates From Order Flow

This prevents:

- market-data stalls from blocking order submission
- slow order reconciliation from delaying state refresh

It also gives us a natural producer-consumer relationship without overcomplicating the design.

### 3. Use Slow Control Loops For Expensive Polling

Calls like:

- `get_waiting_list()`
- `get_executed_orders(order_id)`
- `get_portfolio_item(symbol)`
- `get_portfolio_summary()`

should be periodic control functions, not part of the fast per-symbol quote loop.

The same principle applies to cross-symbol analytics:

- run them from periodic snapshots
- avoid full-universe recomputation every symbol tick if the work is expensive
- publish compact overlays that the fast loop can consume cheaply

### 4. Make Logging Explicitly Asynchronous

The logger thread should absorb:

- debug output
- fill records
- quote decisions
- PnL summaries
- error traces

Trading threads should enqueue and continue.

### 5. Design For Graceful Degradation

When the system is under pressure:

- reduce symbol count
- reduce logging verbosity
- widen quoting thresholds
- increase quote refresh interval slightly
- prioritize flattening and risk safety over count chasing

## Close-To-Close Session Modes

The architecture should support mode changes through the day.

### Normal Mode

- passive quoting
- standard inventory caps
- standard spread filters

### Pace-Recovery Mode

Used when behind the `200 trades/day` target.

- increase passive participation carefully
- consider more symbols
- reduce quote selectivity modestly
- do not abandon fee-aware behavior
- use cross-symbol ranking to prefer the best symbols for extra participation

### Close-Reduction Mode

Activated before the final flatten window.

- tighter inventory caps
- shorter quote lifetime
- reduced willingness to accumulate inventory

### Flatten Mode

- stop opening fresh passive risk
- cancel resting quotes
- liquidate residual inventory
- prefer certainty of flatness over rebate capture

## File And Folder Responsibilities

The source tree is organized by responsibility, not by technical pattern.

### `src/`

Top-level package for production trading code.

Contains:

- system primitives
- market state
- execution logic
- risk controls
- strategy logic
- telemetry
- runtime entrypoints

### `src/core/`

Core runtime glue and environment management.

Expected files:

- `config.py`: typed runtime settings, symbol selection, loop timings, thresholds
- `session.py`: SHIFT connection lifecycle and subscription bootstrap
- `session_clock.py`: session-phase timing, close-window state, and pacing targets
- `symbols.py`: symbol-universe definitions and selection helpers

### `src/data/`

Market-data ingestion and in-memory state.

Expected files:

- `state.py`: fixed state structures for symbols and shared snapshots
- `book_cache.py`: top-of-book and optional depth update logic
- `market_data.py`: market-data polling loop and publication logic

Recommended subfolder:

- `featurespace/`: feature definitions, compute tiers, and transformation pipelines

### `src/execution/`

Order submission, cancellation, and live-order bookkeeping.

Expected files:

- `order_state.py`: working orders, pending cancels, fill summaries
- `order_router.py`: intent-to-order translation and order submission
- `reconciler.py`: slower polling loop for fills, order state, and portfolio corrections

### `src/risk/`

Hard limits and kill-switch logic.

Expected files:

- `limits.py`: pre-trade and session-level guardrails
- `inventory.py`: inventory estimates, netting, and close-risk handling
- `kill_switch.py`: global stop behavior and flatten procedures

### `src/strategy/`

Signal logic and quoting policy.

Expected files:

- `base.py`: shared strategy interface
- `signals.py`: fair-value, imbalance, spread, and urgency signals
- `combiner.py`: merge per-symbol intents with cross-symbol overlays
- `market_maker.py`: first production strategy

Recommended subfolders as the strategy layer grows:

- `per_symbol/`
- `cross_symbol/`
- `allocation/`

Suggested future files:

- `per_symbol/market_maker.py`
- `per_symbol/inventory_skew.py`
- `cross_symbol/ranker.py`
- `cross_symbol/allocator.py`
- `cross_symbol/regime.py`
- `allocation/oco_ftrl.py`
- `allocation/state.py`
- `allocation/combiner.py`

## Feature Architecture

The system needs a serious feature layer, but feature compute must be organized by latency tier.

The right question is not just "what features do we want" but also:

- when do we compute them
- how often do they refresh
- whether they are safe for the hot path
- whether they are per-symbol or cross-symbol
- whether they are deterministic enough for live trading

### Feature Discovery Pipeline

Feature work should happen in two phases:

#### Phase 1. Feature Inventory

List the candidate transformations we may want, grouped by:

- raw market inputs
- per-symbol microstructure features
- cross-symbol relative features
- regime and state features
- model-based features

The first deliverable is a feature catalog, not code.

For each feature, document:

- name
- intuition
- required raw inputs
- lookback window
- update frequency
- latency tier
- expected use in strategy, risk, or ranking
- whether it is per-symbol or cross-symbol

#### Phase 2. Compute Design

Only after the feature catalog exists should we decide:

- where each feature lives
- how it is updated
- whether it is incremental
- whether it is hot-path eligible
- what state must be retained in memory

This prevents us from mixing research exploration with production engineering too early.

### Feature Tiers

#### Tier 0: Raw Inputs

Examples:

- best bid and ask
- top-of-book sizes
- local versus global best prices
- spread
- mid
- last update timestamp
- inventory
- working order state

Properties:

- directly from API or local execution state
- minimal transformation
- must be cheap

#### Tier 1: Hot-Path Derived Features

Examples:

- microprice
- top-level imbalance
- quote age
- spread in ticks
- local-global divergence
- recent fill intensity
- short rolling volatility from incremental updates

Properties:

- incremental
- symbol-local
- cheap arithmetic only
- safe to use every decision loop

#### Tier 2: Warm-Path Features

Examples:

- rolling z-scores
- short-horizon realized volatility
- order-book slope from shallow depth snapshots
- short-window trend
- mean reversion residuals
- passive fill probability estimates
- intraday pace deviation from trade-count target

Properties:

- updated periodically, not every loop
- may use slightly more state and math
- still intended for live use

#### Tier 3: Heavy Features

Examples:

- cross-symbol residualization
- leader-laggard matrices
- rolling regressions
- PCA-like factor approximations
- Kalman or state-space estimates
- nonlinear scoring transforms
- model inference beyond a tiny linear score

Properties:

- must run on a slower cadence
- should usually publish compact outputs back to the live system
- should not sit directly in the symbol-by-symbol quote loop

### Feature Families

The feature space should be explicit and categorized.

#### Linear Features

Examples:

- weighted sums
- rolling differences
- linear spreads
- residuals from linear combinations
- beta-adjusted relative moves

Use case:

- cheap interpretable baseline signals
- good first production candidates

#### Model-Based Features

Examples:

- rolling linear regression residuals
- state-space or Kalman estimates
- latent fair-value estimates
- fill-probability scores
- execution-cost forecasts
- online drift/regime detectors such as CUSUM and Page-Hinkley
- decaying Welford z-score baselines for spread, imbalance, and global drift

Use case:

- useful when the model output can be updated incrementally
- best as warm-path or slow overlay features first

#### Nonlinear Features

Examples:

- clipped or piecewise transforms
- nonlinear imbalance transforms
- ranking-based transforms
- interaction terms
- volatility-conditioned score maps

Use case:

- often useful after baseline linear signals exist
- should be kept simple unless proven valuable

#### Cross-Symbol Features

Examples:

- relative return ranks
- sector-relative deviations
- leader-laggard spread states
- capital allocation scores
- correlation-aware inventory pressure

Use case:

- portfolio coordination
- symbol ranking
- pacing toward the daily trade-count target

### Compute Placement

Feature placement matters as much as feature choice.

#### In `state.py`

Keep:

- canonical raw fields
- the smallest set of always-needed derived fields

Do not put large transformation pipelines here.

#### In `book_cache.py`

Keep:

- incremental symbol-local updates
- hot-path-safe transforms

This is the best place for:

- spread
- mid
- microprice
- imbalance
- staleness

#### In `featurespace/`

Keep:

- feature definitions
- update policies
- rolling-state helpers
- transformation modules by family
- feature registry and metadata

This is where richer feature logic should live.

#### In `strategy/`

Keep:

- consumption of features
- signal weighting
- intent generation

Do not bury feature definitions inside strategy code if they are reusable.

### Feature Registry Pattern

The system should eventually have a feature registry that defines:

- feature name
- owner module
- required inputs
- compute cadence
- state dependencies
- latency tier
- output shape

This gives us a disciplined way to ask:

- what features exist
- what each feature costs
- what depends on what

### Efficient Compute Design

The feature question should be answered in this order:

1. Can this be computed incrementally?
2. Can it reuse already-maintained state?
3. Does it need full history or just rolling state?
4. Does it belong in the hot path, warm path, or slow path?
5. Can the heavy computation publish a compact scalar back to the fast loop?

Good patterns:

- rolling window ring buffers
- running sums and sums of squares
- exponentially weighted updates
- periodic snapshot recomputation for heavy cross-symbol transforms
- publishing compact feature vectors or scores instead of large objects

Bad patterns:

- recomputing history from scratch each loop
- dataframe transforms in live execution code
- full-universe heavy math on every symbol tick
- hiding expensive feature work inside strategy methods

### Recommended `featurespace/` Layout

```text
src/data/featurespace/
  README.md
  catalog.md
  registry.py
  rolling.py
  linear.py
  nonlinear.py
  model_based.py
  cross_symbol.py
  selectors.py
```

Suggested purpose:

- `catalog.md`: human-readable inventory of planned features
- `registry.py`: machine-readable metadata and registration
- `rolling.py`: rolling buffers and incremental statistics
- `linear.py`: cheap linear transforms
- `nonlinear.py`: clipped, ranked, or interaction transforms
- `model_based.py`: regression, state-space, or model-derived outputs
- `cross_symbol.py`: relative-value and ranking transforms
- `selectors.py`: logic for selecting a production-safe subset

## Time-Series Storage Architecture

The system should separate:

- live in-memory state for trading decisions
- rolling in-memory history for feature computation
- persisted historical data for replay, research, and diagnostics

These have different latency requirements and should not be handled by one generic storage layer.

The key rule is:

- snapshots do not grow
- rolling stores are bounded
- history grows through deltas or events

### 1. Live State Store

Purpose:

- hold only the latest tradable state needed for immediate decisions

Examples:

- current best bid and ask
- current top sizes
- current inventory
- current working orders
- latest cross-symbol overlay

Properties:

- tiny
- always in memory
- single-writer ownership
- no historical scans
- fixed-size or tightly bounded shape

Recommended contents:

- current best bid and ask
- current top sizes
- current derived hot features
- current inventory and order exposure
- current quote state
- current risk flags
- current cross-symbol overlay values relevant to this symbol

Do not store:

- rolling arrays
- full tick history
- large feature traces
- replay logs

### 2. Rolling Feature Store

Purpose:

- maintain short and medium lookback history for feature updates

Examples:

- last `N` top-of-book mids
- last `N` spreads
- last `N` imbalances
- last `N` fill events
- event-time and wall-clock windows

Properties:

- in memory
- bounded
- optimized for incremental updates
- not a database in the traditional sense

Recommended structure:

- symbol-local ring buffers
- per-feature rolling statistics
- separate tick-based and time-based windows

Recommended contents:

- last `N` mids, spreads, and imbalances
- recent fill events
- recent order-response timings
- bounded time-window histories
- running moments and exponentially weighted state

Do not store:

- unbounded session history
- persistence-only audit records
- large reader-facing snapshots

### 3. Persistent Session Store

Purpose:

- preserve event history for analysis, replay, model research, and debugging

Examples:

- sampled market state
- fills
- order lifecycle events
- feature snapshots
- per-interval metrics

Properties:

- asynchronous writes only
- append-oriented
- can be compressed or batched
- must never block trading threads

Recommended contents:

- market-data deltas or sampled state events
- order lifecycle events
- fill events
- periodic feature snapshot records
- periodic metrics and health summaries

## Database Strategy

For this system, "database" should mean an offline or async persistence layer, not something queried from the hot path.

### Snapshot Versus Delta Rule

Do not confuse snapshots with history accumulation.

Use:

- snapshots for "what is true now"
- delta or event encoding for "what changed over time"
- rolling stores for "what recent bounded memory do we need to compute with"

This prevents snapshot objects from growing with the session.

### What The Database Is For

Use persistent storage for:

- session replay
- post-trade analytics
- feature research
- parameter tuning
- anomaly investigation

Do not use it for:

- per-tick decision lookup in live trading
- hot-path joins
- synchronous order-loop reads

### What Should Be Snapshot-Based

Good snapshot candidates:

- current per-symbol live state
- current feature vector used by strategy
- current cross-symbol overlay
- current inventory and risk status
- current session pacing metrics

Snapshot requirements:

- fixed-size or tightly bounded
- replaceable
- safe for many readers
- no embedded historical buffers

### What Should Be Delta-Encoded

Good delta or event candidates:

- top-of-book changes over time
- book-size changes
- order submissions
- order cancels
- fills and partial fills
- inventory transitions
- feature changes if we want replay visibility
- regime transitions

Delta requirements:

- append-only
- timestamped
- compact enough for long sessions
- async persistence friendly

### Recommended Persistence Approach

Start simple:

- append-only session files or structured local storage
- one record stream for market samples
- one record stream for orders and fills
- one record stream for periodic feature and metrics snapshots

As the system grows, this can evolve into:

- columnar research storage
- sqlite or duckdb for offline analysis
- partitioned day-by-day datasets

The key rule is:

- live trading writes asynchronously
- research reads later

## Rolling Window Design

Rolling values are essential, but they must be designed explicitly.

The system should support both:

- tick-based windows
- time-based windows

### Tick-Based Windows

Useful for:

- the last `N` book updates
- the last `N` quote changes
- the last `N` fills
- fixed-length event histories

Advantages:

- deterministic update cost
- easy ring-buffer implementation
- natural for event-driven microstructure features

Best for:

- short-horizon HFT signals
- microstructure transforms
- event-count-normalized comparisons

### Time-Based Windows

Useful for:

- last `100ms`
- last `1s`
- last `5s`
- last `1m`

Advantages:

- more stable across varying event intensity
- easier to compare across quiet and busy periods

Best for:

- volatility estimates
- pace tracking
- time-of-day normalization
- fill-rate and quote-age features

### Hybrid Window Strategy

We should support both because they answer different questions.

Good examples:

- tick window for microprice drift
- time window for realized volatility
- tick window for recent fill clustering
- time window for trade-count pacing

### Rolling Implementation Pattern

Use:

- ring buffers for fixed-length tick windows
- timestamped deques or ring buffers for time windows
- running sums, sums of squares, counts, mins, and maxes where possible
- exponentially weighted statistics when exact windows are unnecessary

Avoid:

- recomputing rolling statistics from raw history every loop
- storing unbounded history in memory
- generic dataframe rolling operations in live code

### Snapshot Contract For Rolling Systems

Readers should not receive rolling buffers as part of normal live snapshots.

Instead:

- writers update rolling buffers privately
- writers publish compact derived values
- persistence may separately record deltas or sampled rolling summaries

This keeps reader payloads stable as runtime history accumulates.

## Vectorized Compute Strategy

Vectorization is useful, but only in the right tier.

### Where Vectorization Helps

Good uses:

- cross-symbol ranking snapshots
- periodic recomputation of warm-path features
- research and offline feature evaluation
- batched transforms over many symbols at once

This is especially useful for:

- relative returns
- z-score grids
- ranking and sorting
- cross-sectional residual calculations

### Where Vectorization Hurts

Avoid relying on heavy vectorized machinery for:

- every-symbol hot-path decisions
- tiny per-event updates
- latency-sensitive execution logic

Why:

- batch setup overhead can dominate
- allocation pressure rises
- cache friendliness may worsen for small updates

### Best Practice

Use a hybrid model:

- scalar incremental updates in the hot path
- vectorized snapshots in warm or slow paths

That gives us:

- low latency where it matters
- throughput where batch math actually helps

## Lookup Table Strategy

Lookup tables can be very effective for expensive but stable transforms.

Good candidates:

- clipped nonlinear score maps
- inventory skew curves
- spread-to-quote-width mappings
- urgency schedules by time to close
- fee-aware action thresholds

Benefits:

- constant-time lookup
- predictable performance
- reduced repeated branching and recomputation

Requirements:

- stable input ranges
- well-defined discretization
- careful testing of interpolation or bucket edges

Avoid LUT overuse when:

- the input space is too large
- the transform changes frequently
- discretization error is economically meaningful

### Recommended LUT Usage

Best early uses:

- inventory skew lookup by inventory bucket
- quote aggressiveness by spread bucket
- close-mode urgency by time-remaining bucket
- imbalance-to-skew mapping after clipping and bucketing

Current implementation:

- `src/data/featurespace/lut.py` provides a reusable piecewise-linear LUT
- `src/strategy/params.py` uses LUT-backed toxicity and inventory-pressure mappings
- direct formulas remain the default shape, but the transform layer is now table-driven

## Recommended Data Contracts

The architecture should converge on a few explicit data shapes.

### `SymbolLiveSnapshot`

Purpose:

- current reader-facing per-symbol state

Should contain:

- symbol id
- best bid and ask
- best sizes
- local and global best values if needed
- spread, mid, microprice
- current inventory estimate
- current working-order identifiers or lightweight status
- latest hot-path feature outputs
- timestamps and version markers

Should not contain:

- rolling arrays
- historical event lists
- deep audit state

### `SymbolRollingStore`

Purpose:

- private bounded history for computing rolling features

Should contain:

- ring buffers for tick windows
- timestamped buffers for time windows
- running sums, counts, sums of squares
- exponentially weighted state

Should not be published as a general reader snapshot.

### `MarketEventLog`

Purpose:

- append-only description of what changed

Should contain events like:

- best-price change
- fill
- order-status transition
- inventory change
- cross-symbol overlay refresh

This is the right place for growth over time.

### `CrossSymbolSnapshot`

Purpose:

- latest portfolio-wide overlay for strategy and risk readers

Should contain:

- symbol rankings
- participation flags
- capital weights
- group exposure summaries
- pacing or urgency multipliers

Should remain compact and current-state only.

## Feature Concurrency Guidance

Feature concurrency should be defined explicitly.

The recommended approach is:

- single-writer ownership by tier
- bounded rolling stores
- compact published snapshots
- async event persistence

### Recommended Pattern

For hot and warm feature paths:

- market-data thread writes raw state and symbol-local rolling stores
- warm-path feature updaters publish compact feature outputs
- strategy reads current published feature values

For cross-symbol features:

- compute on periodic snapshots
- publish a compact `CrossSymbolSnapshot`
- do not expose growing working buffers to readers

### Seqlocks Versus Snapshot Swap

Do not start with seqlocks in this codebase.

Why:

- Python is not a great environment for low-level seqlock-style optimization as a first step
- complexity rises quickly
- single-writer publication plus compact snapshot swap is easier to reason about

Preferred default:

- single writer
- bounded private rolling storage
- compact snapshot publication

### Ring Buffers

Ring buffers are strongly recommended for rolling feature state.

They are useful for:

- tick-window history
- fixed-capacity recent event history
- bounded memory with deterministic update cost

They do not need to be "lock-free" to be valuable.

The more important property is:

- one writer
- fixed capacity
- no growth with session length

### Lock-Free Structures

Do not optimize for lock-free data structures first.

In this system, the better priority order is:

1. single-writer ownership
2. fixed-size memory
3. bounded rolling stores
4. compact snapshot publication
5. measurement

Only after profiling should we consider more specialized concurrency structures.

## SPSC Hybrid Transport Design

The architecture should explicitly support a hybrid transport pattern when a single producer thread needs to feed a single consumer thread efficiently.

That pattern is:

- compact current-state snapshot
- compact delta or event stream
- SPSC ring buffer as the handoff transport

This is not the same thing as storing all state in a queue.

Current implementation:

- the market-data producer publishes cloned `MarketState` snapshots inside `MarketDataUpdateEvent`
- the strategy thread updates its own reader-side snapshot from those SPSC events
- this avoids sharing one mutable market-state object across producer and consumer threads

The correct separation is:

- private mutable writer state for computation
- compact reader-facing snapshots for current truth
- compact deltas for efficient incremental handoff

### Why Hybrid Snapshot Plus Delta Exists

Pure snapshot-only publication has a weakness:

- if we publish too often, we resend the same state repeatedly
- if snapshots accidentally include historical payloads, they become more expensive over time

Pure delta-only publication has a different weakness:

- consumers become dependent on seeing every update in order
- recovery and resynchronization get harder
- consumer startup becomes more fragile

The hybrid model solves both:

- deltas provide efficient incremental updates
- snapshots provide resynchronization and current truth

### Role Of The SPSC Ring Buffer

The SPSC ring buffer is not the state store.

Its role is:

- bounded handoff between one producer and one consumer
- transport of deltas, compact events, or refresh triggers
- backpressure boundary between concurrent components

It should not become:

- the authoritative history store
- the only source of truth
- a replacement for private rolling memory

### Best Use Cases In This System

Hybrid snapshot plus delta over SPSC is a strong fit for:

#### 1. Market Data Thread -> Cross-Symbol Aggregator

Producer:

- market-data writer

Consumer:

- cross-symbol feature or strategy aggregator

Why it fits:

- one producer
- one consumer
- consumer can update rankings and overlays incrementally
- snapshot can re-anchor the consumer periodically

#### 2. Market Data Thread -> Warm-Path Feature Worker

Producer:

- market-data writer

Consumer:

- warm-path feature updater

Why it fits:

- warm features often consume compact changes
- not every internal rolling update needs a full copy
- periodic snapshot refresh can recover state cleanly

#### 3. Strategy / Execution Thread -> Telemetry Writer

Producer:

- strategy or execution thread

Consumer:

- telemetry thread

Why it fits:

- event flow is naturally append-oriented
- bounded handoff is desirable
- consumer only needs compact event records

### Where It Is Not The Right Tool

Do not use hybrid snapshot plus delta over SPSC for:

#### Private Per-Symbol Rolling Feature Memory

Reason:

- this is not inter-thread transport
- it is writer-private compute state
- a normal bounded ring buffer is enough

#### Multi-Producer Or Multi-Consumer Workloads

Reason:

- SPSC assumes exactly one producer and one consumer
- forcing many-to-one or one-to-many through SPSC creates unnecessary complexity

#### Core Authoritative Live State

Reason:

- authoritative current state should live in writer-owned structs or published snapshots
- queue transport should not become the state model

### Recommended Hybrid Contract

For each SPSC-linked producer-consumer pair, define:

#### 1. Producer Private State

Contains:

- full mutable working state
- private rolling buffers
- incremental statistics
- writer-only bookkeeping

This state is not pushed directly through the queue.

#### 2. Delta/Event Message

Contains:

- only the minimal change needed by the consumer
- fixed or tightly bounded payload
- event timestamp
- version or sequence number if needed
- symbol id or scope id

Good delta examples:

- best bid price changed
- spread bucket changed
- imbalance bucket changed
- fill occurred
- inventory changed
- symbol-quality score invalidated
- close-mode changed

#### 3. Snapshot Message Or Snapshot Reference

Contains:

- compact current truth for recovery or periodic refresh
- no rolling history
- no unbounded arrays

Snapshot examples:

- current per-symbol live feature vector
- current cross-symbol ranking snapshot
- current strategy overlay snapshot

### Snapshot Cadence Versus Delta Cadence

The best hybrid systems do not publish snapshots and deltas at the same cadence.

Recommended pattern:

- deltas on meaningful change
- snapshots periodically or on demand

Examples:

- emit deltas for every important top-of-book or inventory state change
- emit a compact snapshot every fixed interval such as `50ms`, `100ms`, or `250ms`
- emit a snapshot when consumer resync is needed

This reduces bandwidth while preserving recoverability.

### Delta Design Rules

Good deltas are:

- small
- bounded
- semantically meaningful
- easy to apply incrementally

Bad deltas are:

- large partial copies of state
- variable-size payloads without clear bounds
- payloads that force consumer-side expensive reconstruction on every event

### Snapshot Design Rules

Good snapshots are:

- compact
- fixed-size or tightly bounded
- current-state only
- sufficient for consumer resync

Bad snapshots are:

- historical
- growing with session length
- large enough to dominate transport cost

### Aggregation Workflow For Cross-Symbol Strategy

This is the most important concrete use of the pattern.

Recommended flow:

1. Market-data thread updates writer-private `SymbolRollingStore`.
2. Market-data thread updates latest `SymbolLiveSnapshot`.
3. On meaningful changes, market-data thread emits compact per-symbol deltas through an SPSC ring.
4. Cross-symbol aggregator consumes the deltas and updates its own private aggregator state.
5. On a slower cadence, aggregator publishes a compact `CrossSymbolSnapshot`.
6. Strategy thread reads the latest `CrossSymbolSnapshot` and combines it with per-symbol local intents.

Why this works:

- hot symbol-local compute stays local
- cross-symbol compute is incremental
- queue transport stays compact
- portfolio overlay remains easy to read

### Example Cross-Symbol Delta Payloads

Useful delta fields:

- `ts_ns`
- `symbol_id`
- `event_type`
- `best_bid_px`
- `best_ask_px`
- `spread_ticks`
- `imbalance_bucket`
- `microprice_offset`
- `inventory_lots`
- `local_signal_score`
- `quality_rank_invalidated`

Not every event needs every field. The point is that the schema must stay compact and bounded.

### Example Cross-Symbol Snapshot Fields

Useful snapshot fields:

- `ts_ns`
- `version`
- `enabled_symbols_bitset`
- `symbol_rank_scores`
- `allocation_weights`
- `group_exposure_summary`
- `pace_recovery_mode`
- `close_mode_urgency`

Again, this should remain current-state only.

### Failure And Recovery Semantics

Hybrid transport should be designed with recovery in mind.

Consumer-side expectations:

- deltas are the normal fast path
- snapshots are the recovery anchor
- if a consumer detects inconsistency, it can wait for or request the next snapshot boundary

This is another reason not to rely on delta-only transport for important aggregation state.

### Overwrite And Backpressure Philosophy

Because the ring buffer is bounded, we must define the policy.

Recommended default:

- consumer should be fast enough for normal load
- if it falls behind, prefer resync via fresh snapshot over trying to preserve an unbounded queue

This is especially acceptable for:

- cross-symbol ranking updates
- warm-path feature overlays
- telemetry summaries

It is less acceptable for:

- irreversible audit or compliance logs

Those should be handled by persistent append logic, not only by an in-memory SPSC queue.

### Where To Put The SPSC Primitive

If it is a reusable concurrency primitive, place it in:

- `src/core/concurrency/spsc.py`

or, if keeping the tree flatter:

- `src/core/spsc.py`

Why:

- it is a transport primitive, not a feature definition
- multiple subsystems may reuse it
- it belongs with concurrency infrastructure

Do not place the generic SPSC primitive inside `featurespace/`.

### Where To Put Rolling Ring Buffers

Private rolling buffers for feature computation should live with data and windowing:

- `src/data/windows.py`
- `src/data/featurespace/rolling.py`

These are not the same as SPSC transport buffers.

### Decision Rule

Use hybrid snapshot plus delta over SPSC when all of the following are true:

1. exactly one producer exists
2. exactly one consumer exists
3. consumer benefits from incremental updates
4. consumer can re-anchor from a compact snapshot
5. transport payloads remain compact
6. bounded buffering is acceptable

If those conditions do not hold, use a different pattern.

### Initial Recommendation For This Repository

Recommended:

- yes for market-data to cross-symbol aggregation
- yes for market-data to warm-path feature aggregation
- yes for execution to telemetry event handoff

Not recommended:

- no for writer-private rolling feature memory
- no for replacing authoritative live state
- no for every small internal feature update

## Strategy / Execution Consumer Loop Design

If strategy or execution consumes updates through an SPSC transport, the consumer loop should be explicitly designed as event-driven with timer-based safety fallbacks.

The correct model is not:

- poll queue forever
- recompute everything continuously

The correct model is:

- wake on new transport activity or scheduled timer boundary
- ingest updates
- refresh local decision state
- recompute only impacted symbols
- run periodic safety checks even if no market data changed

### Sequence Counters And Versions

The transport and local decision state should expose versioning.

Recommended counters:

#### 1. `stream_seq`

Purpose:

- tells the consumer whether any new transport messages have arrived

Use:

- producer increments when publishing messages
- consumer tracks `last_seen_stream_seq`
- if unchanged, there is no new transport activity

#### 2. `symbol_version[symbol]`

Purpose:

- tells decision code whether a symbol's local view changed meaningfully

Use:

- increment when the consumer applies a meaningful local update to that symbol
- recompute only when the current version differs from the last processed version

#### 3. `overlay_version`

Purpose:

- tells decision code whether the cross-symbol overlay changed

Use:

- if overlay version changes, any affected symbols should be reconsidered

### Why Versioning Helps

Versioning gives us:

- event-driven wakeup
- cheap "anything new?" detection
- cheap per-symbol "do I need to recompute?" checks
- clean separation between transport activity and decision scheduling

### Consumer Loop Stages

The strategy/execution consumer loop should have two main stages and one safety stage.

#### Stage 1. Ingest

Responsibilities:

- drain available SPSC messages up to a bounded amount
- update local state
- apply snapshot refreshes if present
- mark affected symbols dirty
- bump local symbol and overlay versions as needed

Rules:

- do not submit orders during low-level message parsing
- do not run full portfolio logic per message
- keep ingest cheap and deterministic

#### Stage 2. Decision

Responsibilities:

- process dirty symbols
- compute per-symbol intents from local state
- apply latest cross-symbol overlay
- run risk checks
- compare desired quote state with working orders
- send execution actions

Rules:

- recompute only symbols whose state or overlay relevance changed
- compute both bid and ask decisions from one coherent local state view
- do not treat queue messages themselves as the decision state

#### Stage 3. Safety Timer

Responsibilities:

- stale quote checks
- quote-age refreshes
- close-window logic
- flatten triggers
- disconnect and kill-switch checks
- periodic reconciliation triggers

This stage must continue even if no new transport message arrives.

### Event-Driven Plus Timer-Driven Scheduling

The strategy/execution loop should be:

- event-driven first
- timer-driven second

Why:

- market and fill changes should trigger prompt updates
- stale-risk and close handling cannot depend only on fresh market events

Good triggers:

- new SPSC data
- overlay refresh
- fill event
- inventory event
- timer boundary for stale or close checks

### Wait Policy While No New Data Arrives

Yes, the consumer should wait when nothing new is happening.

But the wait policy should be deliberate.

#### Good Default

- block until new message arrives or timer deadline is hit

Why:

- simple
- avoids burning CPU uselessly
- aligns well with Python runtime realities

#### More Aggressive Option

- short spin for very bursty workloads
- then fall back to blocking wait

This is only worth it if profiling shows real benefit.

#### Avoid

- infinite busy-spin on empty transport
- long coarse sleeps that harm responsiveness
- recompute loops with no new information

### Dirty Symbol Scheduling

The consumer should not process every symbol for every message.

Instead:

1. ingest message
2. update local symbol state
3. mark affected symbols dirty
4. process only dirty symbols
5. optionally run occasional slower whole-universe passes if needed

This keeps strategy compute proportional to actual change.

### Bid / Ask Decision Pipeline

For each dirty symbol, compute both sides from the same local decision state.

Recommended order:

1. read local live state
2. read local inventory state
3. read latest cross-symbol overlay
4. compute fair value
5. compute inventory bias
6. compute cross-symbol bias
7. compute risk constraints
8. derive target bid price and size
9. derive target ask price and size
10. compare targets against current working orders
11. decide keep, cancel, reprice, pull one side, pull both, or flatten

This is better than treating bid and ask as unrelated branches.

### Recommended Message Effects

Some events should almost always create dirty work:

- best-price move
- spread regime change
- fill event
- inventory change
- cross-symbol overlay refresh
- risk-mode change

Some events may update state without immediate action if nothing economically changed.

That logic should live in the local state-application layer, not in ad hoc queue handling.

### Recovery Semantics

If the consumer detects inconsistency:

- stop trusting only incremental deltas
- wait for or request the next compact snapshot boundary
- re-anchor local state
- resume incremental consumption

This is why snapshot support is important even in a delta-driven design.

### Recommended Local Strategy State

The strategy/execution consumer should maintain local reader-private state such as:

- `local_symbol_state[symbol_id]`
- `local_order_state[symbol_id]`
- `local_inventory_state[symbol_id]`
- `cross_symbol_overlay`
- `dirty_symbols`
- `last_processed_symbol_version[symbol_id]`
- `last_seen_stream_seq`
- `overlay_version`

The SPSC queue is the ingress path, not the working decision model.

## Recommended Files For Storage And Rolling Infrastructure

As the repository grows, `src/data/` should likely add:

```text
src/data/
  history.py
  persistence.py
  windows.py
  featurespace/
    rolling.py
```

Suggested roles:

- `history.py`: in-memory rolling series and snapshot access
- `persistence.py`: asynchronous session writers and replay-friendly records
- `windows.py`: tick-window and time-window primitives
- `featurespace/rolling.py`: feature-specific rolling statistics helpers

## Practical Rule Set

For any new feature or storage need, decide:

1. Is it needed in the hot path, warm path, or offline only?
2. Does it need latest value only, rolling memory, or persistent history?
3. Should it be scalar incremental, vectorized batch, or LUT-backed?
4. Can it be bounded cleanly in memory?
5. Can it be written asynchronously for later research?

This keeps the system fast while still making rich feature engineering possible.

### What We Need Before Implementation

Before coding many feature transforms, we should produce:

1. a feature catalog
2. a latency-tier map
3. an input dependency map
4. a compute cadence plan
5. a production-safe subset for V1

That sequence will keep the feature system powerful without making the trading engine unmanageably slow.

### `src/telemetry/`

Non-blocking metrics and persistent logs.

Expected files:

- `logger.py`: queue consumer and structured logging
- `recorder.py`: fill and session record persistence
- `metrics.py`: latency, pacing, PnL, fee, and rebate tracking

### `src/app/`

Runtime assembly and launch entrypoints.

Expected files:

- `main.py`: compose config, session, threads, strategy, risk, and telemetry

### `tests/`

Fast tests for logic that must be trusted before live sessions.

Expected test groups:

- state update correctness
- quote generation
- fee and rebate accounting
- risk limits
- flatten behavior
- reconciliation correctness

## Allowed Dependencies By Layer

### Hot Path

Allowed:

- standard library
- lightweight dataclasses
- simple numeric operations

Discouraged:

- pandas
- large scientific stacks in the decision loop
- serialization-heavy frameworks

### Slow Path

Allowed:

- richer diagnostics
- formatted reports
- more detailed analytics

The rule is simple: expensive work belongs outside the quote loop.

## Interface Philosophy

Each folder should expose narrow, explicit interfaces.

Good examples:

- strategy returns an order intent, not a raw API call
- per-symbol logic returns candidate symbol-local intents
- cross-symbol logic returns overlays and allocations, not raw orders
- online allocators such as OCO-FTRL return weights, budgets, or enablement, not raw orders
- risk returns allow, deny, or modify
- execution owns all `shift.Order` construction
- telemetry accepts events, not internal mutable objects

## Strategy Allocation Layer

If we want to manage multiple strategies or multiple symbol-level experts with an online allocator such as OCO-FTRL, that logic belongs in the strategy allocation layer.

It should not live in:

- `src/execution/`
- `src/risk/`

### Why It Does Not Belong In `execution/`

Execution should own:

- order translation
- order submission
- cancel and replace mechanics
- reconciliation

Execution should not own:

- portfolio intelligence
- online strategy weighting
- performance-driven allocation learning

### Why It Does Not Belong In `risk/`

Risk should own:

- hard limits
- exposure caps
- drawdown stops
- kill-switch behavior

Risk can clip or reject allocator outputs, but it should not be the place where online alpha allocation learning lives.

### Correct Role Of OCO-FTRL

OCO-FTRL should be treated as:

- a strategy-level or symbol-level online allocation method
- a portfolio overlay
- an upstream decision layer that controls budget, weights, or participation

Its job is to answer questions like:

- which strategy sleeve gets more weight right now
- which symbols deserve more active quoting budget
- which expert should dominate under current market conditions

Its job is not to:

- place orders directly
- bypass risk controls
- own final execution behavior

### Recommended Placement

Preferred home:

- `src/strategy/allocation/oco_ftrl.py`

Supporting files:

- `src/strategy/allocation/state.py`
- `src/strategy/allocation/combiner.py`

If we want to keep the tree flatter at first, a temporary home could be:

- `src/strategy/cross_symbol/oco_ftrl.py`

But long term, a dedicated `allocation/` subfolder is cleaner.

### Recommended Flow

1. Per-symbol or expert strategies emit candidate intents or target exposures.
2. OCO-FTRL produces allocation weights, symbol budgets, or strategy weights.
3. Cross-symbol combiner applies those weights to candidate outputs.
4. Risk validates, clips, or vetoes unsafe results.
5. Execution translates the approved targets into live orders.

### Recommended OCO-FTRL Inputs

Examples:

- recent strategy-level pnl
- regret or loss estimates
- fill quality
- turnover
- capital usage
- inventory usage
- symbol quality scores
- toxicity or cost regime indicators

### Recommended OCO-FTRL Outputs

Examples:

- strategy weights
- symbol-level budget multipliers
- enable or disable flags
- confidence scores
- allocation caps

### Design Rule

OCO-FTRL should allocate desired exposure or budget.

It should not construct exchange orders directly.

This keeps modules composable and makes future optimization easier.

## Documentation Strategy

Each major folder includes its own local markdown guide so context can be loaded selectively:

- [src/README.md](/home/faduzzle/projects/stevehft/src/README.md)
- [src/core/README.md](/home/faduzzle/projects/stevehft/src/core/README.md)
- [src/data/README.md](/home/faduzzle/projects/stevehft/src/data/README.md)
- [src/execution/README.md](/home/faduzzle/projects/stevehft/src/execution/README.md)
- [src/risk/README.md](/home/faduzzle/projects/stevehft/src/risk/README.md)
- [src/strategy/README.md](/home/faduzzle/projects/stevehft/src/strategy/README.md)
- [src/telemetry/README.md](/home/faduzzle/projects/stevehft/src/telemetry/README.md)
- [src/app/README.md](/home/faduzzle/projects/stevehft/src/app/README.md)
- [tests/README.md](/home/faduzzle/projects/stevehft/tests/README.md)

These docs should stay close to the code as the implementation evolves.

## Near-Term Next Step

Use this architecture and the build sequence in [BUILD.md](/home/faduzzle/projects/stevehft/BUILD.md) to keep the implementation under `src/` aligned as the strategy, execution, and feature layers evolve.
