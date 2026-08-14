# Pre-Live And Next-Work Checklist

## Purpose

This is the working checklist for what still needs to be done before, during,
and immediately after the first real SHIFT simulator runs.

It complements:

- [BUILD.md](/home/faduzzle/projects/stevehft/BUILD.md)
- [ARCHITECTURE.md](/home/faduzzle/projects/stevehft/ARCHITECTURE.md)
- [HEURISTIC_CONSTANTS.md](/home/faduzzle/projects/stevehft/src/strategy/HEURISTIC_CONSTANTS.md)

The goal is to keep the remaining work concrete and ordered so we do not keep
adding features while missing basic operational or economic checks.

## Legend

- `[ ]` not done
- `[~]` partially done, but needs another pass
- `[x]` implemented once, but still re-verify in live dry-run

## Phase 1: Hard Must-Do Before Any Live Order Routing

### Config And Startup Safety

- [x] Add a pre-live config verifier script that checks:
  - symbols are non-empty
  - `tick_size > 0`
  - market-data interval and reconciliation intervals are sane
  - flatten window starts early enough to survive cancel + fill races
  - max position, max gross, max open orders, and buying-power caps are not contradictory
  - telemetry output path is writable
- [x] Print one startup summary with all effective risk/model/session settings.
- [x] Refuse to start in live-order mode if required credentials, symbols, or session
  parameters are missing.
- [x] Confirm dry-run mode and live-order mode cannot be confused by a stale flag or
  stale CLI argument.

### Broker Boundary And Session Safety

- [~] Run one connect -> subscribe -> ingest -> reconcile -> disconnect smoke path
  with no live orders.
- [x] Verify unsubscribe and disconnect cleanup behavior if an exception happens during
  startup, not only during normal shutdown.
- [x] Add a reconnect/restart drill:
  - recover waiting orders
  - recover executions
  - recover portfolio
  - do not submit new orders until the recovered state is reconciled
- [x] Confirm malformed or unknown broker order types are surfaced loudly and do not
  silently flip order side.

### Reconciliation And Order-Lifecycle Invariants

- [x] Partial fills are tracked by `order_id`.
- [x] Duplicate fills are deduped.
- [x] Cancel-in-flight orders are still treated as fillable.
- [x] Replace is modeled as cancel + staged submit, not atomic replace.
- [x] Position mismatch compares broker inventory against locally tracked fills, not
  against pending order sizes.
- [~] Add one test that proves a pending-new order is not marked inactive before the
  broker has had a reasonable appearance grace period.
  - Current code has `new_order_grace_ms` and a regression test.
  - Keep this at `[~]` until a live dry-run confirms broker-side appearance latency
    does not exceed the configured grace window.
- [x] Add one test that proves an unknown order type or malformed execution payload
  escalates to a safe mode instead of quietly corrupting local state.
- [x] Add a bounded startup reconciliation mode that stays in `degraded_reconcile`
  until broker state is internally consistent.

### Safe Modes And Flatten Behavior

- [x] Safe modes exist: `normal`, `degraded_reconcile`, `position_mismatch`,
  `flatten_only`, `kill_switch`.
- [x] `FLATTEN` is allowed even in kill-switch mode.
- [x] Short-cover flattening can be chunked when one-shot cover BP is unavailable.
- [x] Add a kill-switch drill test that proves:
  - `normal -> degraded_reconcile -> flatten_only -> kill_switch`
  - no new opening-risk orders are emitted after escalation
  - telemetry is still flushed
- [x] Add a flatten convergence test with partial fills and repeated portfolio
  refreshes until inventory reaches zero.
- [x] Define one manual recovery procedure for exiting `kill_switch` after state is
  known clean.

### Threading And State Ownership

- [x] Market-data producer publishes cloned snapshots, not shared mutable `MarketState`.
- [x] SPSC queue exists for producer/consumer handoff.
- [x] Do one more thread-safety audit for every shared object touched by both:
  - market-data producer thread
  - strategy/execution thread
  - telemetry writer thread
- [x] Confirm no hot-path object is mutated after it has been placed on an async queue.
- [x] Confirm every loop has bounded wait/stop behavior and can shut down promptly.
- [x] Verify telemetry serialization and queue backpressure do not slow the trading
  thread under fake-load or dry-run load.
  - `JsonlEventLogger.log(...)` snapshots JSON-safe payloads on the caller thread.
  - The writer thread serializes asynchronously, and a queue-saturation regression
    verifies dropped-event behavior stays non-blocking.

### Strategy / Feature Wiring That Must Be Correct Before Live

- [x] Switch all depth, concentration, and front-shape signals from global book to
  cleaned local book.
  - Global L1 should only be used as a slow reference / drift signal.
  - Local L2/L3 depth should drive microstructure, concentration, queue, and shape.
- [x] Fix `cancel_pressure` so replace-driven cancels do not penalize normal
  requoting.
- [x] Reset fee and `execution_cost_score` session statistics at a clean
  closed-to-open transition so stale prior-session market-order flattening does not
  bias next-session allocation.
- [x] Tighten `max_position_mismatch_lots` for smoke caps so restart/reconcile
  mismatches are not tolerated up to the full per-symbol position limit.

### Telemetry And Audit Completeness

- [x] Strategy traces, order commands, fills, portfolio updates, and session metrics
  are logged.
- [x] Logger snapshots payloads at `log()` time and serializes in the background thread.
- [x] Add a telemetry sanity test that runs one cycle and asserts the expected event
  family appears at least once:
  - `strategy_trace`
  - `strategy_target`
  - `order_fill`
  - `portfolio_snapshot`
  - `session_metrics`
- [x] Add one log-schema checklist so we know which fields must be present before
  trying live post-run analysis.

## Phase 2: First Read-Only Live Dry Run

### Highest Priority Signal Validation

- [~] Validate the dual-book signal contract first:
  - all multi-level depth / concentration / front-shape / queue features come from
    cleaned local book
  - global book contributes only L1 reference imbalance / drift
  - NBBO touch remains the quote-placement anchor
  - Code-level regression exists; keep this `[~]` until live telemetry confirms the
    same split against real SHIFT payloads.
- [ ] Confirm this dual-book split in live telemetry before tuning anything else.

### Market-Data Validation

- [ ] Verify the dual-book contract against live SHIFT responses:
  - Global L1 is treated as reference signal only
  - Local L2/L3 depth is available and cleaned of our own orders
  - NBBO best bid/ask are the actionable touch
- [ ] Compare raw broker local book snapshots against cleaned internal local book and
  confirm our own orders are removed exactly once.
- [ ] Verify timestamps and staleness thresholds behave sensibly during quiet and
  active periods.
- [ ] Check that local queue-ahead and queue-share estimates move in the expected
  direction as local depth changes.

### Feature And Model Sanity

- [x] Add a post-run telemetry validator for dry-run logs.
  - Run `python3 -m src.telemetry.dry_run_validator runs/<session>/events.jsonl --tick-size 0.01`
    after each read-only smoke.
  - This checks required event families, crossed/tick-misaligned quotes, disabled-side
    sizing, flatten-target shape, finite strategy diagnostics, bounded queue/toxicity
    fields, and bounded passive-fill ratios.
- [x] Add a compact post-run telemetry summary for dry-run logs.
  - Run `python3 -m src.telemetry.dry_run_summary runs/<session>/events.jsonl`
    after the validator.
  - Use this to inspect feature ranges, queue/toxicity levels, target mix, and simple
    local/global sign agreement before deeper plotting.
- [ ] Verify these feature families on live dry-run telemetry:
  - NBBO spread and mid
  - local weighted microprice
  - local depth imbalance
  - local front-shape / concentration
  - global L1 imbalance and global mid drift
  - spread / imbalance / drift z-scores
  - toxicity markout score
  - queue-fill support
- [ ] Plot or inspect a few symbols and confirm signs are intuitive:
  - bid-heavy local book should usually push fair value up
  - ask-heavy local book should usually push fair value down
  - positive global drift should bias center upward, but only mildly
  - high toxicity should widen or downsize quotes
- [x] Verify `DecayingWelford`, `PSquareQuantile`, CUSUM, and Page-Hinkley outputs are
  finite, bounded, and not stuck at constants.
  - Code-level regressions exist; still inspect these values on live dry-run telemetry.
- [x] Verify LUT outputs are monotone and stay inside the intended min/max ranges.
  - Code-level regression exists; still re-check calibrated LUT behavior on dry-run
    telemetry.

### Order-Intent Sanity In Dry Run

- [x] Confirm strategy never emits crossed quotes:
  - `bid_px < ask_px`
  - prices are tick-aligned
- [x] Confirm quote sizes are positive only on enabled sides.
- [x] Confirm flatten mode emits only risk-reducing targets.
- [x] Confirm stale-book gating suppresses opening quotes but does not suppress
  necessary flattening.
- [x] Confirm quote refresh logic does not churn away same-price good-queue orders.
  - Code-level regression exists; still verify live `stale_order_after_ms` and queue
    preservation behavior in Phase 3.

## Phase 3: First Small Live-Order Smoke Run

### Risk Envelope For First Live Orders

- [ ] Use a tiny symbol set and conservative max position/open-order caps.
- [ ] Start with one or two liquid symbols before enabling a larger universe.
- [ ] Keep short exposure especially conservative until BP behavior is observed live.
- [ ] Start with enough flatten buffer before close to tolerate a two-cycle cancel ->
  market-flatten sequence.

### Live-Order Verification

- [ ] Run a bounded cycle count with `--execute-orders`.
- [ ] Verify one complete lifecycle:
  - submit limit
  - partial fill or full fill
  - cancel or replace
  - portfolio update
  - optional flatten
- [ ] Compare local order ledger against broker waiting list and executions after each
  run.
- [ ] Compare local portfolio ledger against broker portfolio state.
- [ ] Inspect whether maker fills are actually earning rebates and market fills are
  correctly marked as taker fees.
- [ ] Verify replace -> cancel -> staged-resubmit round trips do not give up queue
  position unnecessarily.
  - Live-check `stale_order_after_ms`
  - Live-check `min_replace_interval_ms`
  - Confirm same-price / good-queue orders are preserved

### Immediate Post-Run Checks

- [ ] Check realized slippage / shortfall metrics for sign and units.
- [ ] Check VWAP/TWAP metrics for sanity.
- [ ] Check passive fill ratio and per-symbol fee score are not being polluted by old
  session state.
- [ ] Check allocation and queue-fill support are not being suppressed by our own
  normal replace activity.
- [ ] Confirm no symbol is stuck with stale live orders after shutdown.

## Phase 4: Economic Calibration Work

### Replace Heuristics With Estimators Where It Matters

- [x] Review [HEURISTIC_CONSTANTS.md](/home/faduzzle/projects/stevehft/src/strategy/HEURISTIC_CONSTANTS.md)
  and pick which `CALIBRATE_SOON` items must be tuned before scaling.
  - First post-smoke calibration families are already prioritized there:
    toxicity LUTs, inventory-pressure LUTs, passive fill probability,
    queue-support weighting, GLFT arrival/depth coefficients, CJ inventory
    risk, and slippage proxy weights.
- [ ] Replace or calibrate the most economically sensitive heuristics first:
  - toxicity -> width / size LUTs
  - inventory pressure -> skew / gamma LUTs
  - passive fill probability model
  - queue-support weighting
  - GLFT arrival-intensity / half-width coefficients
  - CJ inventory-risk coefficient
- [ ] Build a replay calibration notebook/script from recorded dry-run logs.
- [ ] Compare baseline heuristics against realized fill rate, adverse selection,
  and shortfall.

### Cross-Symbol Allocation

- [ ] Decide the first OCO-FTRL allocation state and feature set.
- [ ] Implement the allocation module under `src/strategy/allocation/` if we are ready
  to make cross-strategy or cross-symbol weights live.
- [ ] Add one rule that caps allocator output so risk remains authoritative.
- [ ] Feed per-symbol realized PnL per reserved-capital proxy into allocation.
- [ ] Verify allocator updates do not create quote oscillation or frequent enable/
  disable thrash.

### Model Upgrades Already Documented But Not Yet Fully Live

- [ ] Decide which of these should be promoted from docs/research into the live
  parameter path next:
  - VPIN
  - Hawkes arrivals
  - Roll clean-price / bounce-to-trend toxicity sensor
  - Ledoit-Wolf covariance
  - Hurst / recovery half-life
  - transfer entropy / mutual information / surprise
  - RLS / Misra-Gries / reservoir sampling / LZ complexity
- [ ] For each promoted model, define:
  - data source
  - state update cadence
  - hot/warm/slow tier
  - fallback behavior if the estimate is stale or missing
  - exactly which strategy field it modifies

## Phase 5: Failure Drills And Edge-Case Coverage

### Broker / Network Failure Drills

- [x] Inject broker exceptions for:
  - submit
  - cancel
  - waiting-list poll
  - executed-order poll
  - portfolio poll
- [x] Verify each failure path emits telemetry and transitions to the right safe mode.
- [x] Verify repeated execution-sync failures cannot be masked by one success.
- [x] Verify missing portfolio symbols are handled as broker-authoritative flat
  positions without losing realized PnL.

### Market / Microstructure Failure Drills

- [x] Test stale local book, stale NBBO, wide spread, and crossed/invalid book cases.
- [x] Test partial fill during cancel-in-flight.
- [x] Test delayed fill discovery after order disappears from waiting list.
- [x] Test off-touch orders with weak queue position versus same-price orders with
  strong queue position.
- [x] Test close-window race conditions with one side still canceling while flatten is
  required.

### Restart / Recovery Drills

- [~] Start, submit orders, stop abruptly, restart, and verify no duplicate or unsafe
  opening-risk orders are emitted before reconciliation.
  - Code-level regression now covers broker-side preexisting waiting-order recovery
    with no duplicate same-side submit on the first strategy cycle.
  - Still run a true process-level stop/restart drill against SHIFT before marking `[x]`.
- [x] Verify audit/session telemetry survives restart enough to support manual review.
- [x] Define whether old session logs are immutable and how new runs are separated.
  - `events.jsonl` is append-only if `session_dir` is reused.
  - Use a fresh `session_dir` per run for immutable per-session logs.
  - Preflight warns if a non-empty `events.jsonl` already exists.

## Phase 6: Performance And Scalability

- [x] Run `scripts/profile_fake_load.py` at the intended symbol count and cadence.
- [x] Record mean, p95, and worst-case cycle latency.
- [x] Confirm event-driven loop can keep up with the target update rate.
- [~] Check memory growth over a long fake-load run:
  - order audits
  - toxicity markout queues
  - online stats state
  - telemetry queue
  - `scripts/profile_fake_load.py` reports peak traced memory for bounded fake-load runs;
    still do a longer soak before scaling symbol count.
- [x] Decide whether any nonlinear model paths should be moved to lookup tables for
  latency or monotonicity control.
  - Current decision: toxicity -> width/size and inventory-pressure -> gamma are
    already LUT-backed in `ParameterLookupTables`; keep GLFT and passive-fill
    probability formula-based until dry-run telemetry shows a reason to replace
    them with fitted monotone maps.
- [ ] If needed, split slow cross-symbol allocation onto a lower-frequency path while
  keeping per-symbol quote decisions hot.

## Phase 7: Documentation And Operating Discipline

- [x] Keep [BUILD.md](/home/faduzzle/projects/stevehft/BUILD.md),
  [ARCHITECTURE.md](/home/faduzzle/projects/stevehft/ARCHITECTURE.md), and this
  checklist aligned after each structural change.
  - Rechecked after the dual-book, queue-position, slippage, and pre-live docs updates.
- [x] Add a short operator runbook for:
  - dry-run startup
  - live smoke startup
  - shutdown
  - kill-switch recovery
  - post-run telemetry review
- [x] Document which constants are frozen for V1 live smoke and which are expected to
  be recalibrated quickly.
  - Policy is documented in
    [HEURISTIC_CONSTANTS.md](/home/faduzzle/projects/stevehft/src/strategy/HEURISTIC_CONSTANTS.md)
    under `V1 Live-Smoke Freeze Policy`.
- [x] Record one post-run checklist template so every simulator session gets reviewed
  the same way.

## Recommended Immediate Order

If we want the shortest path to a safe first live smoke, do these next in order:

1. Re-verify local-book feature wiring and global-L1 drift separation in a dry-run.
2. Stress-test telemetry queue/backpressure and confirm no hot-path slowdown.
3. Live-validate `pending_new` appearance grace and replace/cancel/resubmit
   queue-retention behavior.
4. Run a read-only live dry-run against one or two symbols.
5. Run a small live-order smoke with tiny caps and conservative short exposure.
6. Post-run calibration pass on toxicity, fill probability, and GLFT/CJ constants.
