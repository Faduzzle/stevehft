# Data Module Guide

## Purpose

`src/data/` owns market-data ingestion and the local in-memory state used by the strategy.

This folder is performance-critical because every trading decision depends on it.

The data layer is the market-model boundary.
It converts broker responses into clean, time-stamped state and derives features that support quoting, risk, execution, and diagnostics.
It must preserve the distinction between observed external liquidity and the system's own orders.

## Planned Files

### `state.py`

Responsibilities:

- define per-symbol state structures
- define any shared snapshot structures
- keep the canonical field list for live market state

Expected fields include:

- top-of-book prices and sizes
- local versus global prices
- spread and mid
- NBBO mid/spread plus local microprice and local/global imbalance
- one-sided depth summaries
- ladder depth coverage features such as "how many levels cover the first 25% of visible volume"
- stale-data timestamps
- local inventory estimate hooks

Design preference:

- flat typed objects
- fixed fields
- allocation-light updates

Current implementation:

- `BestPriceSnapshot`
- `BookLevel`
- `SymbolState`
- `MarketState`

Current `SymbolState` helpers:

- `local_microprice`
- `local_depth_imbalance`
- `global_l1_imbalance`
- `global_l1_mid`
- `combined_book_imbalance(...)`
- `weighted_microprice(...)`
- `cumulative_bid_depth(...)`
- `cumulative_ask_depth(...)`
- `depth_imbalance(...)`
- `one_sided_book_volume(...)`
- `levels_to_cover_side_volume_fraction(...)`

### `book_cache.py`

Responsibilities:

- translate SHIFT best-price and book responses into local state updates
- compute derived features incrementally
- publish the latest clean symbol snapshot

This file should answer:

- what is the current best bid and ask
- how fresh is the book
- what is the current fair-value proxy

Current implementation:

- normalizes `shift.BestPrice` into `BestPriceSnapshot`
- normalizes `shift.OrderBookEntry` lists into `BookLevel`
- cleans our own orders out of the local book while leaving global untouched
- treats local depth as the multi-level competitive book and global as an L1 reference signal
- updates per-symbol cached state
- emits `book_cache_update` telemetry events when a logger is present

### `market_data.py`

Responsibilities:

- run the market-data update loop
- refresh subscribed symbols
- schedule polling cadence
- hand off updated state to readers

Concurrency role:

- single writer for market data
- strategy thread reads the latest state

Current implementation:

- `MarketDataLoopConfig`
- `MarketDataLoopStats`
- `MarketDataLoop.run_once()`
- `MarketDataLoop.run_until_stopped()`

### `featurespace/`

Responsibilities:

- define the candidate feature universe
- organize feature families
- separate feature discovery from feature productionization
- provide compute-tier guidance
- hold rolling-state utilities and feature metadata

This subfolder is where we decide both:

- which transformations are valuable
- how they should be computed safely
- which online algorithms are appropriate for compact live summaries

### Future storage helpers

As the data layer grows, it should likely also own:

- rolling window primitives
- bounded in-memory history buffers
- asynchronous persistence hooks

## Feature-Engineering Philosophy

The feature problem has two separate parts:

1. feature selection
2. feature computation design

We should not mix them.

First, define the full candidate feature catalog. Then decide how to compute each feature efficiently enough for live use.

## Feature Families

The data layer should be ready to support:

- raw state features
- linear transforms
- nonlinear transforms
- model-based transforms
- cross-symbol transforms
- execution-quality features
- inventory and capital-usage features
- time-of-day and regime features

### Raw State Features

Examples:

- bid and ask
- bid and ask sizes
- spread
- mid
- local and global price divergence
- inventory
- order age

### Linear Features

Examples:

- rolling returns
- moving averages
- weighted price spreads
- residuals from simple linear relationships

### Nonlinear Features

Examples:

- clipped imbalance transforms
- rank transforms
- interaction terms
- thresholded urgency signals

### Model-Based Features

Examples:

- rolling regression residuals
- state-space fair-value estimates
- passive fill probability scores
- execution-cost estimates

### Cross-Symbol Features

Examples:

- relative strength
- leader-laggard states
- sector-relative dislocations
- symbol quality rankings
- capital-allocation scores

## Compute Tiers

### Hot Path

Allowed:

- incremental arithmetic
- tiny rolling updates
- cheap symbol-local transforms

Examples:

- spread in ticks
- microprice
- imbalance
- quote age

### Warm Path

Allowed:

- periodic rolling statistics
- shallow-depth transforms
- small cross-symbol ranking updates

Examples:

- short realized volatility
- fill-rate estimates
- rolling z-scores

### Slow Path

Allowed:

- model-based transforms
- heavier cross-symbol transforms
- research-oriented candidate features

Examples:

- rolling regressions
- residualization
- latent-state estimates

## Storage Layers

The data layer should distinguish three storage roles:

### Live State

- latest tradable values only
- tiny and always in memory
- read directly by strategy and execution
- fixed-size or tightly bounded

### Rolling State

- bounded windows for feature calculation
- supports both tick-based and time-based histories
- updated incrementally
- private to writers unless explicitly summarized

### Persistent History

- async session storage
- used for replay, research, and diagnostics
- never queried in the hot path
- grows through event or delta records

## Snapshot Versus Delta Rule

Use:

- snapshots for current compact state
- rolling stores for bounded recent memory
- delta or event logs for growth over time

Do not let snapshots absorb rolling history or audit history.

## Rolling Window Design

We should support both:

- tick windows
- time windows

Tick windows are best for:

- fixed recent book updates
- event-count-normalized microstructure features

Time windows are best for:

- volatility
- pacing
- quote lifetime
- fill clustering over elapsed time

Recommended implementation style:

- ring buffers for fixed tick windows
- timestamped bounded structures for time windows
- running summary statistics whenever possible

Readers should normally receive compact derived values, not the rolling buffers themselves.

## Vectorization And Lookup Tables

Vectorization should be used selectively.

Good fits:

- offline feature evaluation
- warm-path cross-symbol snapshots
- batch ranking and residual computations

Poor fits:

- tiny hot-path scalar updates
- execution-critical decision branches

Lookup tables are useful for:

- inventory skew curves
- urgency schedules
- clipped nonlinear mappings

They should be used only when the discretization error is understood and acceptable.

## Recommended Data Contracts

The data layer should eventually expose shapes like:

- `SymbolLiveSnapshot`: current reader-facing symbol state
- `SymbolRollingStore`: private bounded rolling memory
- `MarketEventLog`: append-only session history
- `CrossSymbolSnapshot`: current portfolio-wide overlay state

These contracts keep current state, bounded memory, and historical growth separate.

## Transport Versus Storage

The data layer should also distinguish:

- storage structures
- transport structures

Storage structures:

- `SymbolLiveSnapshot`
- `SymbolRollingStore`
- `MarketEventLog`
- `CrossSymbolSnapshot`

Transport structures:

- compact delta events
- compact snapshot publications

Do not use transport messages as a substitute for real state ownership.

## Hybrid Snapshot Plus Delta Over SPSC

This pattern is useful when one producer thread needs to feed one consumer thread efficiently.

Use it for:

- market-data to cross-symbol aggregation
- market-data to warm-path feature workers
- telemetry handoff

Do not use it for:

- private rolling feature memory
- authoritative live state
- anything that requires many producers or many consumers

The recommended model is:

1. writer updates private rolling and live state
2. writer emits compact deltas on meaningful change
3. consumer applies deltas incrementally
4. periodic compact snapshots allow resync

That keeps the transport small and the storage model clean.

## Recommended `featurespace/` Layout

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

Likely future files in `src/data/` itself:

```text
src/data/
  history.py
  persistence.py
  windows.py
```

## Concurrency Guidance

Efficient choice:

- one dedicated market-data thread
- single-writer ownership for symbol state
- readers consume latest snapshots without rebuilding objects

Avoid:

- multiple threads writing the same symbol state
- deep copies on every update
- per-tick full-depth pulls unless required
- expensive feature recomputation on every symbol loop

## Context For Future Work

If we are trying to answer "what features should exist" or "why is this transform too expensive for live use," `src/data/featurespace/` should contain that context.
If quotes look wrong, timing is stale, or strategy decisions feel delayed, `src/data/` is still the first place to inspect.
