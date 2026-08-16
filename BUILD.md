# SHIFT HFT Build Guide

## Goal

Build a low-latency, competition-ready trading system for the SHIFT HFT environment using the API documented in `wiki/`.

The build target is a measurable decision system, not only an order sender.
It connects market observations to strategy decisions, risk controls, execution outcomes, and performance evidence.
Every subsystem must expose enough state and telemetry to explain its behavior and support model improvement.

The design has four linked loops:

1. The market loop maintains clean, time-stamped views of the visible auction.
2. The decision loop converts market and inventory state into bounded strategy targets.
3. The control loop blocks unsafe actions and corrects local state with broker reconciliation.
4. The learning loop uses dashboards, traces, replay, and calibration to improve models and parameters.

The first three loops protect live operation.
The fourth loop improves performance without placing unvalidated research logic in the hot path.

This document is a fresh design. Everything under `Old model/` is considered deprecated and is intentionally excluded from this build.

Competition requirements that materially affect the design:

- The system needs to execute at least `200 trades per day`.
- Each executed limit-order share earns a `$0.002` rebate.
- Each executed market-order share pays a `$0.003` fee.
- Fees and rebates are applied at end of day.
- Partial fills earn or pay fees only on executed shares.
- Resting limit orders withhold balance until execution.
- Any long inventory left at market close is forcibly closed at the closing price with a `1%` deduction.
- Any short inventory left at market close is forcibly closed at the closing price with a `1%` addition.
- Short positions tie up the original short-sale buying power and also require enough additional buying power to close the position.

## Design Principles

1. Keep the hot path tiny.
2. Cache market state locally and avoid expensive API calls inside the decision loop.
3. Separate trading-critical code from logging, analytics, and research code.
4. Prefer deterministic behavior over cleverness.
5. Design for safe failure: disconnects, stale books, runaway inventory, and stuck orders must be handled automatically.
6. Prefer passive fills over aggressive fills unless inventory or time constraints force us to cross.
7. End the session flat. Overnight or end-of-day forced liquidation is too punitive for an HFT-style strategy.

## What The SHIFT API Gives Us

From the docs in `wiki/`, the useful primitives for the trading engine are:

- `shift.Trader(username)` to create the session.
- `connect(cfg_file, password)` and `disconnect()` for session lifecycle.
- `sub_order_book(symbol)` / `sub_all_order_book()` to receive book updates.
- `get_best_price(symbol)` for top-of-book state.
- `get_order_book(symbol, type, max_level=99)` and `get_order_book_with_destination(...)` for depth snapshots.
- `submit_order(order)` and `submit_cancellation(order)` for execution.
- `get_waiting_list()` / `get_executed_orders(order_id)` for order-state reconciliation.
- `get_portfolio_item(symbol)`, `get_portfolio_items()`, and `get_portfolio_summary()` for inventory and PnL.
- `cancel_all_pending_orders(timeout=10)` as a safety valve.

Important API constraints from the wiki:

- Order size is in lots, where `1 size = 100 shares`.
- `get_last_price(symbol)` and `get_last_size(symbol)` are not guaranteed to be synchronized.
- Some API methods are snapshot/poll oriented, so we should treat local state as the primary source of truth in the hot path.

Important competition constraints:

- We should assume profitability is strongly influenced by limit-order rebates.
- Market orders are useful for emergency risk reduction, but they are economically disadvantaged in normal flow.
- End-of-day inventory is dangerous because forced close pricing is intentionally punitive for both longs and shorts.
- Buying-power usage must account for resting orders, existing inventory, and short-close requirements.

## Trading Economics

Translate everything into per-share and per-lot terms so the strategy can reason about true edge.

### Fee / Rebate Model

- Limit fill rebate: `$0.002/share` = `$0.20` per 100-share lot.
- Market fill fee: `$0.003/share` = `$0.30` per 100-share lot.

Implication:

- Passive trading has a structural edge if we can avoid adverse selection.
- Aggressive trading needs a stronger signal because it starts with a cost handicap.

### Forced Close Penalty

At market close:

- Long inventory is closed at closing price with a `1%` deduction.
- Short inventory is closed at closing price with a `1%` addition.

Implication:

- Inventory must be aggressively flattened before the session ends.
- Strategy behavior should become more conservative near close.
- The final minutes need a dedicated flattening mode, not normal quoting.

### Buying Power Implications

- Resting limit orders reserve buying power.
- Shorts consume buying power twice in practice: capital tied to the short plus capital needed to buy back shares.

Implication:

- Open-order budgeting matters.
- We should cap resting quotes per symbol and monitor reserved buying power continuously.
- Short inventory caps should generally be tighter than raw gross-notional caps would suggest.

## Market Microstructure Constraints

These are hard execution constraints, not optional theory.

### NBBO / Best-Market Awareness

The system should evaluate quotes relative to the current best visible bid and ask.

Implications:

- a limit order only makes sense relative to the live touch and queue
- joining, improving, or backing off the touch should be explicit policy
- crossing the spread should be recognized as an aggressive action with explicit spread and fee cost

For this competition:

- limit orders are the default expression of alpha
- market orders are mainly for flattening, emergency risk reduction, or tightly bounded exceptional cases

### Continuous Double Auction Mechanics

The engine operates inside a continuous double auction.

That means:

- price-time priority matters
- queue position matters
- the spread can move before our cancel or replace completes
- cancel does not remove fill risk immediately
- fills, cancels, and state updates interleave

Design implication:

- queue and fill-quality features are mandatory
- replace must be modeled as cancel-plus-reconcile, not atomic replace
- reconciliation must tolerate asynchronous state ordering

### Dual-Book Signal Contract

SHIFT exposes two economically different book views, plus a merged touch:

- `Global` is effectively an L1 simulated reference process from `get_best_price()`
- `Local` is the full-depth competitive book from `get_order_book(LOCAL_BID/ASK)`
- `NBBO` / best touch is the merged actionable quote anchor

Design implication:

- compute multi-level depth, concentration, front-shape, and queue-position features from
  the cleaned local book, not from global L1
- use global L1 only as a slow drift / reference-imbalance signal
- anchor executable bid/ask prices to NBBO and tick-align them
- estimate queue position from cleaned local top-of-book depth plus our own live size,
  not from global L1 size

### VWAP / TWAP As Execution Benchmarks

VWAP and TWAP are useful benchmark layers even for a market maker.

Use them for:

- evaluating flatten quality
- evaluating emergency aggressive execution
- comparing realized inventory-cleanup quality to simple baselines

Implication:

- telemetry should record enough state to compare realized execution against simple VWAP and TWAP references
- flatten logic should be judged on execution quality, not only on whether it eventually reached flat

### Microstructure As Hard Constraints

No strategy should be allowed to ignore:

- spread floors
- stale-book conditions
- queue and fill asymmetry
- market-order fees
- reserved buying power
- end-of-day forced-liquidation risk

The right posture is:

- alpha proposes
- microstructure and risk constrain
- execution implements only what survives those constraints

## High-Level Architecture

The system should be one process with a small number of tightly scoped threads. Avoid a many-thread design unless measurement proves it helps.

The strategy stack should have two layers:

- a fast per-symbol layer for local quote decisions
- a slower cross-symbol layer for portfolio coordination and symbol prioritization

The per-symbol layer owns the immediate trading decision. The cross-symbol layer acts as an overlay that adjusts participation, sizing, skew, and capital allocation.

## Integration-First Build Order

We should build this system backwards from the SHIFT-facing boundaries.

That means the earliest milestones should be:

1. session lifecycle and connectivity
2. market-data subscriptions and normalized local state
3. order submission, cancellation, and order-id tracking
4. waiting-list, execution, and portfolio reconciliation
5. safe-mode and flatten behavior
6. telemetry and audit trail
7. only then deeper strategy and feature sophistication

This order gives us confidence in the parts that can actually lose money first.

For the remaining execution checklist, use
[PRE_LIVE_CHECKLIST.md](/home/faduzzle/projects/stevehft/PRE_LIVE_CHECKLIST.md).

## Per-Symbol And Cross-Symbol Design

### Per-Symbol Strategy

Use per-symbol logic for:

- quote placement
- quote refresh and cancel decisions
- spread and microprice reactions
- inventory skew at the symbol level
- stale-book handling

This should be the fastest strategy path in the system.

### Cross-Symbol Strategy

Use cross-symbol logic for:

- ranking symbols by spread quality or fill opportunity
- coordinating capital across names
- controlling exposure across correlated symbols
- leader-laggard or relative-strength overlays
- deciding which names should be active when behind the `200 trades/day` target

This should run on a slower cadence than the per-symbol loop.

### How They Combine

Recommended rule:

- per-symbol logic emits a candidate intent
- cross-symbol logic emits overlays such as weights, symbol enable flags, urgency multipliers, or inventory biases
- a combiner merges both into a final executable intent

This prevents cross-symbol logic from bloating the hot path while still giving us portfolio-wide coordination.

### Core Components

#### 1. Session Manager

Responsibilities:

- Load credentials and `initiator.cfg`.
- Establish and maintain `shift.Trader` connectivity.
- Subscribe to required symbols at startup.
- Detect disconnects and trigger a clean trading halt.

Rules:

- No strategy logic here.
- No blocking disk I/O after startup.

#### 2. Market Data Cache

Responsibilities:

- Maintain an in-memory state object per symbol.
- Update local best bid/ask, spread, depth, imbalance, last update time, and optional rolling features.
- Normalize API objects into lightweight internal structs.

Suggested per-symbol state:

```text
symbol
best_bid_px
best_bid_sz
best_ask_px
best_ask_sz
local_bid_px
local_bid_sz
local_ask_px
local_ask_sz
spread
mid
microprice
book_imbalance
last_book_update_ns
position_lots
open_bid_order_id
open_ask_order_id
pending_cancel_bid
pending_cancel_ask
```

Rules:

- Keep this structure flat and numeric where possible.
- Prefer overwriting fields to constructing fresh dictionaries on every update.
- Use integer ticks internally if the instrument price grid is known and stable.

#### 3. Strategy Engine

Responsibilities:

- Read the latest cached state.
- Produce a quoting or taking decision.
- Emit desired order intents, not raw API calls.

The strategy engine should be split internally into:

- per-symbol decision logic
- cross-symbol overlay logic
- an intent combiner

First strategy target:

- Start with a simple inventory-aware market maker.
- Quote only when spread is wide enough.
- Skew quotes away from inventory.
- Pull quotes when market data is stale or spread collapses.
- Bias the system toward passive executions so rebates help fund the required trade count.
- Use a lightweight cross-symbol ranking overlay to decide which symbols deserve active quoting.

Do not start with:

- Cross-sectional models.
- Heavy ML inference.
- Per-tick regressions.
- Large feature pipelines in pandas.

#### 4. Execution Engine

Responsibilities:

- Convert intents into `shift.Order` objects.
- Submit new orders.
- Cancel or replace stale quotes.
- Track outstanding orders and reconcile fills.

Execution rules:

- One owner for order state.
- Never let strategy code call `submit_order()` directly.
- Throttle cancel/replace churn.
- Reject duplicate actions for the same symbol-side while an earlier action is unresolved.

#### 5. Risk Engine

Responsibilities:

- Enforce hard limits before any order is sent.
- Monitor inventory, gross exposure, order count, cancel rate, and stale market data.
- Trigger kill-switch behavior.

Minimum hard limits:

- Max long lots per symbol.
- Max short lots per symbol.
- Max net lots overall.
- Max reserved buying power from resting orders.
- Max open orders per symbol.
- Max age for a quote before mandatory refresh or cancel.
- Max stale-data age before trading halt.
- Daily or session drawdown stop.

#### 6. Telemetry / Recorder

Responsibilities:

- Persist fills, decisions, rejects, and periodic snapshots.
- Stay fully off the hot path.

Rules:

- Use a queue between trading logic and disk logging.
- Batch writes.
- Avoid printing every event to stdout during live trading.

## Threading Model

Recommended initial model:

- Main thread: session lifecycle and shutdown control.
- Market data thread: refresh subscribed symbol state.
- Strategy/execution thread: run the decision loop.
- Logger thread: write events asynchronously.

Hybrid strategy scheduling:

- run per-symbol logic every strategy loop
- refresh cross-symbol overlays every `N` loops or on a fixed cadence such as `50ms` to `250ms`
- reuse the latest cross-symbol overlay between refreshes

Why this is a good starting point:

- Simple enough to debug.
- Keeps ownership boundaries clear.
- Avoids lock contention from over-engineering too early.

Guidelines:

- Use one lock per symbol state group or a lock-free snapshot handoff if simple enough.
- Avoid shared mutable structures spanning every module.
- Measure before adding more concurrency.
- Do not create a dedicated hot-path cross-symbol thread unless measurement proves it is necessary.

## Hot Path Rules

Inside the strategy/execution loop, avoid:

- pandas
- numpy allocations per tick
- repeated string formatting
- repeated dictionary creation
- file writes
- network calls other than the final order action
- repeated full-book pulls if top-of-book is enough
- portfolio-wide scans for every symbol on every loop

Inside the hot path, prefer:

- preallocated state objects
- simple arithmetic
- integer comparisons
- monotonic clock timestamps
- symbol-local decisions
- small bounded loops
- cached cross-symbol overlays instead of recomputing full-universe analytics every symbol cycle

## Market Data Handling

### Subscription Policy

At startup:

- Prefer an explicit configured symbol universe for deterministic competition runs.
- Optionally call `get_stock_list()` as a discovery tool or sanity check.
- Subscribe only to the symbols we will actively trade.

Universe selection should prefer names with:

- consistent top-of-book activity
- enough spread persistence to support passive fills
- enough turnover to realistically reach the `200 trades/day` target
- manageable inventory swings

Do not:

- Blindly use `sub_all_order_book()` unless profiling shows the environment can handle it comfortably.

### Book Update Policy

Use `get_best_price(symbol)` as the default fast view.

Use `get_order_book(...)` only for:

- startup warmup
- periodic validation
- deeper liquidity estimation before larger aggressive orders
- special strategies that truly need depth

Derived features to maintain incrementally:

- spread = ask - bid
- mid = (bid + ask) / 2
- microprice
- local/global divergence
- top-level imbalance
- staleness age
- expected passive/aggressive slippage

Cross-symbol snapshots should maintain:

- symbol quality scores
- spread opportunity rankings
- passive fill participation ranking
- group or sector exposure buckets if used
- current capital allocation weights

Suggested microprice:

```text
microprice = (bid_px * ask_sz + ask_px * bid_sz) / (bid_sz + ask_sz)
```

Current implementation nuance:

- the actionable spread and mid come from NBBO / merged best touch
- multi-level microprice, depth imbalance, and front-shape features come from the cleaned local book
- global L1 is tracked separately as a slow exogenous imbalance/drift signal

## Execution Policy

### Order Types

Use:

- `LIMIT_BUY`
- `LIMIT_SELL`
- `MARKET_BUY`
- `MARKET_SELL`
- `CANCEL_BID`
- `CANCEL_ASK`

Recommended behavior:

- Passive quoting for normal market making.
- Market orders only for emergency flattening, controlled taking, or hard stop exits.
- Treat market orders as a risk tool, not a primary alpha source.

### Quote Lifecycle

For each symbol, track:

- current desired bid price/size
- current desired ask price/size
- live bid order id
- live ask order id
- last action timestamp

Requote only when one of these is true:

- fair value moved by at least one tick
- spread regime changed
- inventory skew changed enough to alter the quote
- quote age exceeds the refresh threshold
- local/global best price moved away from our resting quote

Trade-count policy:

- The engine should track fills and projected end-of-day trade count.
- If we are behind pace for `200 trades/day`, we can widen symbol coverage or tighten participation thresholds.
- We should not blindly force market orders just to hit count, because fees and forced-close risk can erase the objective.
- Cross-symbol logic should decide where extra participation is most likely to produce passive fills rather than spreading risk evenly across all names.

### Fill Reconciliation

Polling targets:

- `get_executed_orders(order_id)`
- `get_waiting_list()`
- `get_portfolio_item(symbol)`

Policy:

- Reconcile fills in a slower control loop, not the micro-decision loop.
- Treat portfolio and executed orders as confirmation, but maintain a fast local estimate of inventory between polls.
- Track executed shares separately by passive and aggressive flow so end-of-day fees/rebates can be estimated in real time.

## Session-Level Objectives

The strategy is not just trying to maximize raw PnL. It must satisfy participation requirements without bleeding on fees or forced liquidation.

Primary objectives:

1. Finish the day flat.
2. Reach or exceed `200` executed trades.
3. Maximize passive fill ratio.
4. Keep fee-adjusted and rebate-adjusted PnL positive.

Daily health metrics:

- executed trades
- executed shares
- passive fill count
- aggressive fill count
- estimated rebate earned
- estimated fees paid
- fill VWAP
- fill TWAP
- arrival and decision implementation shortfall
- gross realized PnL
- net realized PnL after estimated fees/rebates
- current inventory by symbol
- reserved buying power

## Risk and Safety

Kill-switch conditions should include:

- trader disconnected
- data stale beyond threshold
- inventory limit breach
- repeated rejects
- too many live orders
- inability to cancel

Time-based safety modes:

- Normal mode: regular passive quoting.
- Close-reduction mode: tighter inventory caps and reduced quote persistence.
- Flatten mode: stop passive quoting and exit residual inventory before the close.

Kill-switch action sequence:

1. Stop new order generation.
2. Cancel all passive orders.
3. If inventory remains, flatten with controlled aggressiveness.
4. Continue logging until flat and safe.

Startup safety sequence:

1. Connect.
2. Verify symbol list.
3. Subscribe symbols.
4. Warm local state.
5. Confirm book timestamps are moving.
6. Enable strategy only after warmup succeeds.

Intraday pacing sequence:

1. Compute current executed-trade count.
2. Compare to expected pace for time of day.
3. If behind pace, increase passive participation carefully.
4. If ahead of pace, prioritize quote quality over raw count.

Shutdown safety sequence:

1. Disable strategy.
2. Cancel working orders.
3. Flatten all inventory before the close.
4. Flush logs.
5. Disconnect.

## Recommended Repository Layout

```text
stevehft/
  BUILD.md
  ARCHITECTURE.md
  README.md
  initiator.cfg
  credentials.py
  wiki/
  src/
    core/
      session.py
      session_clock.py
      config.py
      symbols.py
    data/
      market_data.py
      book_cache.py
      state.py
    execution/
      order_router.py
      order_state.py
      reconciler.py
    risk/
      limits.py
      kill_switch.py
      inventory.py
    strategy/
      base.py
      combiner.py
      market_maker.py
      signals.py
      allocation/
        oco_ftrl.py
        state.py
        combiner.py
      per_symbol/
        market_maker.py
      cross_symbol/
        ranker.py
        allocator.py
    telemetry/
      logger.py
      recorder.py
      metrics.py
    app/
      main.py
  tests/
    test_book_cache.py
    test_inventory.py
    test_limits.py
    test_market_maker.py
    test_order_router.py
```

## Build Plan

### Phase 1: Bootstrap

Deliverables:

- Clean environment setup for the SHIFT Python package.
- Minimal app that connects, subscribes to a few symbols, prints cached top-of-book, and exits cleanly.
- Config object for usernames, password source, symbols, limits, and loop timings.
- Session telemetry bootstrap so startup and shutdown are logged.

Exit criteria:

- Stable connect/disconnect.
- Book subscriptions confirmed.
- Top-of-book cache updates without crashes.

### Phase 2: Local State Engine

Deliverables:

- Per-symbol state struct.
- Market-data updater loop.
- Derived feature calculations.
- Staleness tracking.
- Shared cross-symbol snapshot inputs.

Exit criteria:

- We can maintain live best bid/ask and derived fields for the selected universe.
- State updates are allocation-light and easy to inspect.

### Phase 3: Execution And Reconciliation

Deliverables:

- Order router.
- Working-order registry.
- Cancel and replace policy.
- Fill and inventory reconciliation loop.
- Broker-authoritative portfolio recovery path.
- Safe handling of pending cancel, partial fill, and inactive-order edge cases.

Exit criteria:

- We can submit, cancel, and reconcile a single-symbol quote safely.
- No duplicate live-order confusion.
- Local and broker state either converge or trigger explicit safe modes.

### Phase 4: Telemetry And Auditability

Deliverables:

- Session logger lifecycle.
- Structured event stream from strategy trace through fills and portfolio updates.
- Replayable audit trail for order lifecycle and reconciliation failures.

Exit criteria:

- We can explain what happened for a live order from target through fill.
- Startup and shutdown telemetry are flushed cleanly.

### Phase 5: First Strategy

Deliverables:

- Inventory-aware per-symbol market-making strategy.
- Quote skewing logic.
- Quote refresh rules.
- Basic spread and volatility filters.
- Cross-symbol ranking and allocation overlay.
- Strategy-allocation layer ready for future online allocators such as OCO-FTRL.
- Intent combiner for local plus portfolio context.
- Trade-count pacing logic toward `200 trades/day`.
- Passive-versus-aggressive fee model in decision scoring.
- Strategy consumption of a selected V1 feature subset.
- Event-driven strategy or execution loop with dirty-symbol scheduling and timer-based safety checks.

Exit criteria:

- Strategy can quote passively, coordinate across symbols, manage inventory, pace toward `200 trades/day`, and stop safely under stale-data conditions.

### Phase 6: Feature Catalog And Compute Plan

Before building a large live feature pipeline, define the full candidate feature space.

Deliverables:

- feature catalog grouped by family and latency tier
- separation between per-symbol and cross-symbol features
- separation between raw, linear, model-based, and nonlinear transforms
- dependency map from raw inputs to derived outputs
- compute placement plan for each feature

Feature families to inventory:

- raw market-state features
- linear transforms
- model-based transforms
- nonlinear transforms
- cross-symbol transforms
- execution and fill-quality features
- inventory and capital-usage features
- time-of-day and regime features

For each feature, document:

- feature name
- business intuition
- raw inputs required
- update cadence
- lookback memory required
- hot-path eligibility
- expected consumer module

Exit criteria:

- We know which features are candidates.
- We know where each feature should be computed.
- We know which features are safe for V1 live trading.

### Phase 7: Feature Compute Framework

After the feature catalog exists, build the compute framework that can support it safely.

Deliverables:

- `src/data/featurespace/` layout
- rolling-state utilities
- feature registry metadata
- separation of hot-path, warm-path, and slow-path feature updates
- compact publication of heavy feature outputs back to the strategy layer
- support for both tick-based and time-based rolling windows
- a plan for vectorized batch recomputation outside the hot path
- initial lookup-table candidates for stable nonlinear transforms

Compute rules:

- hot-path features must be incremental and cheap
- warm-path features may use rolling state and periodic refreshes
- heavy or model-based features must publish compact outputs rather than large structures
- cross-symbol transforms must not recompute expensively on every symbol decision
- persistent history must never be queried from the hot path
- vectorized computation belongs in warm or slow paths unless measurement proves otherwise
- lookup tables should be used for stable bounded transforms, not as a substitute for good state design

Exit criteria:

- We can compute a small production-safe feature set reliably.
- We have an extension path for heavier feature families without redesigning the engine.

### Phase 8: History And Persistence Layer

Build the storage layer only after the live-state and rolling-state responsibilities are clear.

Deliverables:

- live-state versus rolling-state versus persistent-history separation
- bounded in-memory rolling windows
- asynchronous persistence for session events and sampled state
- replay-friendly event schema
- initial storage format for post-trade analysis
- explicit snapshot contracts and explicit delta or event contracts
- clear rules for what readers can consume directly versus what remains writer-private

Storage rules:

- live trading reads from memory, not from the database
- persistence is append-oriented and async
- rolling state stays bounded
- feature snapshots are sampled intentionally rather than dumped blindly
- snapshots must remain fixed-size or tightly bounded
- growth over time belongs in event logs, not in live snapshots

Exit criteria:

- We can replay a session or inspect feature evolution after the fact.
- Persistent storage does not interfere with trading latency.

### Phase 9: Feature Concurrency Contract

Before implementing a large feature engine, define the concurrency rules for feature state.

Deliverables:

- single-writer ownership rules for raw state, rolling state, and published features
- snapshot publication rules
- delta or event logging rules
- ring-buffer design for rolling windows
- clear decision on where snapshot swap is used and where private mutable buffers are used
- explicit guidance for when hybrid snapshot plus delta over SPSC is used
- message contract for cross-symbol aggregation transport
- decision on overload and resync behavior for bounded transport
- strategy or execution consumer-loop contract for event-driven ingest and timer-driven safety checks
- sequence-counter and versioning plan for transport and local state

Concurrency rules:

- published snapshots are compact and current-state only
- rolling buffers remain private to their writers
- event history grows through append-only logs
- no feature snapshot may silently accumulate unbounded history
- specialized lock-free structures are optional and only justified by profiling
- SPSC is a transport primitive, not the authoritative state model
- hybrid snapshot plus delta is used only where one producer and one consumer benefit from incremental handoff
- resync should happen via compact snapshots rather than by letting queues grow without bound
- strategy or execution should wake on new data or timer deadlines, not recompute endlessly in a blind loop
- local dirty-symbol scheduling should decide what to recompute after ingest

Exit criteria:

- We can describe exactly where each feature lives while being computed.
- We can describe exactly what a reader sees.
- We can explain how history accumulates without bloating snapshots.
- We can explain how cross-symbol aggregators receive updates and recover from lag or inconsistency.
- We can explain exactly how strategy or execution waits, wakes, ingests, and decides.

### Phase 10: Performance Hardening

Deliverables:

- Latency timing around market-data update, decision, submit, cancel, and reconcile loops.
- Reduced logging volume in live mode.
- Configurable symbol-universe size.
- measurements comparing scalar incremental updates versus vectorized batch recomputation where relevant
- validation of lookup-table approximations versus direct transforms where used

Exit criteria:

- We know where time is going.
- We can operate without obvious event-loop stalls.

### Phase 11: Competition Readiness

Deliverables:

- Session-start checklist.
- End-of-day flatten procedure.
- Recovery flow for disconnect/reconnect.
- Parameter presets for aggressive, normal, and safe trading modes.
- Daily fee/rebate estimator.
- Trade-count pacing dashboard or status output.

Exit criteria:

- Operator can run the system without touching code.
- Failure modes are documented and controlled.

## Latency Checklist

Before calling the system low-latency, verify:

- No dataframe usage in live execution code.
- No per-tick disk writes.
- No unbounded queues.
- No full portfolio scans in every symbol decision.
- No repeated deep book pulls unless required.
- No blocking sleeps in the execution path other than intentional loop pacing.
- No expensive object serialization in the hot path.
- Timestamps use `time.monotonic_ns()` for internal measurements.
- Cross-symbol analytics are not recomputed expensively on every symbol decision.
- Rolling windows are bounded and incremental.
- Persistent history writes are fully asynchronous.
- Vectorized code is not accidentally sitting in the per-symbol hot loop.
- Lookup-table approximations have been validated against their direct formulas.
- Published snapshots do not contain growing historical payloads.
- Rolling buffers are private and bounded.
- Session growth happens through async event logs, not through reader snapshots.
- Strategy or execution only recomputes when meaningful state changed or timer safety checks fire.
- Sequence counters and local versions prevent pointless repeated decision work.

## Suggested First Configuration

Start conservative:

- 2 to 4 symbols only.
- 1 passive bid and 1 passive ask per symbol.
- Small lot sizes.
- Tight inventory caps.
- Mandatory stale-book cancellation.
- Mandatory pre-close flattening enabled.
- Live tracking of trade count versus the `200/day` target.

Good initial symbol selection criteria:

- tighter spreads
- steady trade flow
- predictable depth
- limited gap behavior

Good first cross-symbol signals:

- relative spread-quality ranking
- recent passive fill opportunity ranking
- simple leader-laggard relationships
- sector or group exposure balancing

Good first production-safe per-symbol features:

- spread in ticks
- top-level imbalance
- microprice deviation from mid
- short realized volatility
- quote age
- recent fill rate

Good first production-safe model-based or heavier candidates:

- rolling fair-value residual
- leader-laggard residual score
- passive fill probability score
- inventory-adjusted ranking score

Good first rolling infrastructure:

- tick-window ring buffers for top-of-book derived values
- time-window statistics for volatility and pacing
- running sums and sums of squares for short rolling moments
- bounded fill-event history

Good first hybrid snapshot plus delta over SPSC uses:

- market-data to cross-symbol aggregator
- market-data to warm-path feature worker
- strategy or execution to telemetry writer

Good first lookup-table candidates:

- inventory-skew function
- spread-bucket quote-width mapping
- time-to-close urgency mapping
- clipped imbalance-to-quote-bias mapping

Good first data contracts:

- `SymbolLiveSnapshot`
- `SymbolRollingStore`
- `MarketEventLog`
- `CrossSymbolSnapshot`

Good first transport contracts:

- compact symbol delta event
- compact cross-symbol overlay snapshot
- bounded SPSC handoff policy with snapshot-based resync

Good first consumer-loop contracts:

- `stream_seq` for new transport activity
- `symbol_version` for per-symbol recompute gating
- `overlay_version` for cross-symbol overlay gating
- dirty-symbol scheduling after ingest
- timer-driven stale and close checks during idle periods

Good first allocation contracts:

- candidate strategy or symbol intents as allocator inputs
- budget or weight outputs from OCO-FTRL-like allocators
- risk clipping after allocation and before execution

## Anti-Patterns To Avoid

- Reusing any architecture from `Old model/`.
- Starting with all 30 names at once.
- Combining strategy, order routing, and risk logic in one class.
- Letting cross-symbol research code sit directly in the per-symbol hot loop.
- Building a huge feature library before defining feature metadata, dependencies, and compute cadence.
- Recomputing expensive transforms from scratch instead of maintaining rolling state.
- Using a database as part of live decision retrieval.
- Storing unbounded history in memory.
- Vectorizing tiny hot-path updates that are cheaper as scalar incremental math.
- Letting live snapshots quietly accumulate historical buffers over the session.
- Treating an SPSC queue as the authoritative state store.
- Sending large historical payloads through transport messages instead of publishing compact snapshots and deltas.
- Recomputing the full universe on every tiny transport event.
- Busy-spinning forever while no new transport data arrives.
- Using market orders for normal quoting behavior.
- Ignoring reserved buying power from limit orders.
- Waiting too long to flatten inventory near the close.
- Treating logging as free.
- Adding machine learning before the baseline engine is stable.
- Optimizing blindly without timings.

## Definition Of Done For V1

V1 is done when we have:

- a stable SHIFT connection
- a local market-data cache
- a working per-symbol market-making strategy
- a working cross-symbol overlay layer
- a documented feature catalog with compute tiers
- a production-safe V1 feature subset
- inventory and stale-data risk controls
- a fee/rebate-aware scoring model
- trade-count pacing toward `200/day`
- safe cancel/flatten behavior
- persistent post-trade logs for review
- measured loop timings

## Next Step

Implement the code in this order:

1. `src/app/main.py`
2. `src/core/session.py`
3. `src/data/state.py`
4. `src/data/book_cache.py`
5. `src/execution/order_state.py`
6. `src/execution/order_router.py`
7. `src/execution/reconciler.py`
8. `src/risk/limits.py`
9. `src/risk/kill_switch.py`
10. `src/strategy/market_maker.py`

Once that SHIFT-facing skeleton exists, we can wire up a minimal live runner and only then deepen strategy logic instead of guessing.
