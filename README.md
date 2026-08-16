# Stevens SHIFT HFT Trading System 

This repository contains a Python trading system for the SHIFT HFT simulator.
It collects market data, builds local state, generates market-making targets, applies risk controls, routes orders, and records an audit trail.

Stevens HFT Trading Competition was an interesting experience, definitely a lot of work and learning done with low latency and high fidelity trading systems. 

The system supports two broker-connected modes:

- `src.app.live_smoke`: bounded or continuous smoke sessions with order routing disabled by default.
- `run.py`: the deployment launcher with live order routing enabled.

Treat every session that uses `--execute-orders` or `run.py` as a live trading session.
Test credentials and symbols before you enable order routing.

## Repository map

| Path | Responsibility |
|---|---|
| `src/core/` | Runtime configuration, SHIFT session lifecycle, clocks, units, and concurrency primitives. |
| `src/data/` | Market-data polling, book normalization, state snapshots, and online feature statistics. |
| `src/strategy/` | Market-making decisions, quote ladders, signals, adaptive parameters, and allocation. |
| `src/risk/` | Position limits, buying-power checks, inventory state, and safe modes. |
| `src/execution/` | Order intents, order state, routing, cancellation, slippage, and reconciliation. |
| `src/telemetry/` | JSONL event logs, metrics, dry-run validation, and post-run analysis. |
| `src/app/` | Startup wiring, runtime loops, dashboard, replay, preflight, and operations. |
| `scripts/` | Read-only log viewers and fake-load profiling tools. |
| `tests/` | Deterministic unit and integration tests with fake SHIFT interfaces. |
| `wiki/` | Local SHIFT API reference used by the implementation. |

The hot-path dependency direction is:

`data -> strategy -> risk -> execution`

Telemetry records the result without owning live trading state.

## Implementation model

The system uses one process with a small number of cooperating runtime components.
The design keeps the fastest path local and keeps broker polling on a slower control path.

The main runtime flow is:

```text
SHIFT market data
        |
        v
market-data cache -> strategy targets -> risk evaluation -> order router
        ^                                      |
        |                                      v
        +--------- broker reconciliation <- working orders and fills
```

The market-data thread writes the latest local snapshot.
The strategy and execution path reads that snapshot and owns order-state updates.
The reconciliation path corrects local expectations with broker-reported state.

The broker remains authoritative for working orders, executions, positions, and buying power.
Local state provides low-latency decisions, but it must not override broker state after reconciliation.

### Market representation

The data layer stores a typed `MarketState` object with one `SymbolState` per symbol.
Each symbol contains normalized prices, sizes, book levels, timestamps, and derived features.

The model separates three market views:

- Global L1 data provides a reference price and a slow drift or imbalance signal.
- Local depth data provides the competitive multi-level book used for depth and queue features.
- The merged best bid and ask provide the executable touch for spread checks and quote placement.

The book cache removes the system's own orders from local competitive depth.
This prevents the strategy from treating its own displayed size as external liquidity.

The strategy uses the following market features:

- mid-price and spread in ticks
- local microprice and local depth imbalance
- global L1 imbalance and global-to-local price divergence
- cumulative depth and the number of levels that cover a volume fraction
- book freshness and quote age
- recent fill behavior and execution quality

The system represents order size in lots at the broker boundary.
One lot represents 100 shares in the SHIFT model.
Fee, rebate, inventory, and short-cover calculations convert between lots and shares explicitly.

### Strategy design

The primary strategy is a passive market maker.
It generates candidate quote targets from market state and inventory state.
It does not create broker orders or call SHIFT directly.

The strategy applies these stages:

1. Reject stale, invalid, or excessively wide books.
2. Build a fair-value estimate from the touch, microprice, imbalance, and bounded global drift.
3. Apply inventory skew so quotes encourage position reduction.
4. Select a quote width from spread, volatility, queue support, and expected fill quality.
5. Build a one-level or adaptive two-level passive quote ladder.
6. Apply flattening or aggressive overlays when risk or session timing requires them.
7. Emit targets and structured diagnostics for risk and telemetry.

The current model path combines two ideas:

- Cartea-Jaimungal-style inventory skew moves fair value away from unwanted inventory.
- GLFT-style width selection links quote distance to spread, volatility, queue state, and fill probability.

The implementation bounds model outputs before they affect a quote.
Static parameter providers support tests and explicit fallback values.
The adaptive provider uses rolling market and execution observations for live parameter resolution.

The strategy has two coordination levels:

- Per-symbol logic reacts quickly to the local book, inventory, quote age, and fills.
- Cross-symbol allocation adjusts participation, budgets, enablement, and relative priority.

Cross-symbol allocation does not bypass risk or submit orders.
It modifies candidate targets before the shared risk and execution path evaluates them.

### Risk model and controls

Risk runs immediately before order submission.
It evaluates the proposed action against current inventory, working orders, buying power, market freshness, and safe mode.

The risk layer controls:

- maximum position lots per symbol
- optional gross position lots across symbols
- reserved buying power for resting orders
- additional buying-power needs for short positions and short covers
- stale-book and invalid-price rejection
- passive versus aggressive order permissions
- close-window inventory reduction
- flatten-only and kill-switch behavior

Risk separates market exposure from state-consistency risk.
A position mismatch, stale reconciliation result, disconnect, or repeated rejection can stop new risk even when prices look valid.

The kill-switch controller provides explicit modes:

- `normal` permits standard quoting.
- `degraded_reconcile` reduces activity while broker state is uncertain.
- `position_mismatch` blocks normal quoting until exposure agrees.
- `flatten_only` cancels passive risk and permits inventory reduction.
- `kill_switch` blocks new risk and coordinates emergency shutdown.

The close process is risk-driven because SHIFT applies a punitive forced close.
Long inventory receives a 1% close-price deduction.
Short inventory receives a 1% close-price addition.
The strategy therefore reduces inventory and passive exposure as the close approaches.

### Execution design

Execution translates approved internal intents into concrete `shift.Order` objects.
Only the execution layer owns broker order construction and submission.

The order lifecycle uses an `OrderLedger` keyed by `order_id`.
Each audit record tracks submitted size, executed size, status, fills, benchmarks, and slippage.

The router provides these controls:

- duplicate-action suppression
- explicit limit and market order construction
- cancel and replace as separate non-atomic operations
- cancel and replace throttling
- passive-first order selection
- safe-mode and pre-trade gating
- emergency cancellation and flatten handling

The strategy treats a quote as a target.
The reconciler compares that target with the broker's working orders.
It then chooses whether to keep, cancel, replace, or submit an order.

This design handles fills that arrive during cancellation.
It also handles partial fills, delayed portfolio updates, and orders that disappear from the waiting list before execution accounting completes.

### Reconciliation and state correction

The reconciliation loop polls working orders, executed orders, portfolio items, and portfolio summary data.
It updates local ledgers from broker responses and emits audit events for each state change.

Reconciliation is slower than market-data processing.
It is still a safety-critical control loop because local expected state can drift from actual broker state.

Startup requires successful connection, subscription, initial state, and repeated normal reconciliation before standard quoting begins.
Shutdown stops market data, cancels live orders, disconnects, and flushes telemetry.

### Telemetry and evaluation

The system records the complete decision chain:

`strategy target -> risk decision -> order command -> broker state -> fill -> position update -> portfolio snapshot`

JSONL events support dry-run validation, strategy tracing, order-flow analysis, risk audits, and PnL review.
The ledger also calculates fill VWAP, fill-price TWAP, fee estimates, rebate estimates, and implementation shortfall.

These metrics evaluate both profitability and control quality.
For example, a session that reaches the trade-count target but uses poor flattening prices still fails the execution objective.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the full data contracts and concurrency design.
Read [src/strategy/README.md](src/strategy/README.md), [src/risk/README.md](src/risk/README.md), and [src/execution/README.md](src/execution/README.md) for component details.

## Systems-level design

The system treats trading as a controlled feedback system.
Market observations produce decisions, decisions produce orders, and executions produce new observations.

Each layer has a clear question:

| Layer | Question | Output |
|---|---|---|
| Market model | What is happening in the visible market? | Clean snapshots and features. |
| Strategy model | What action has positive expected value? | Quote targets and diagnostics. |
| Risk model | Is the action safe with current exposure and state certainty? | Approved, reduced, or blocked intent. |
| Execution model | How do we express the intent in a changing auction? | Orders, cancels, and replacements. |
| Reconciliation model | What did the broker actually accept and execute? | Corrected ledgers and health state. |
| Performance system | Did the decision and control process work? | Metrics, traces, alerts, and calibration inputs. |

This separation prevents a profitable signal from bypassing exposure limits.
It also prevents a safe control from hiding poor signal quality or poor execution.

### Performance management and dashboards

Performance has three dimensions:

- Economic performance measures PnL, fees, rebates, fill quality, markout, and shortfall.
- Trading performance measures fills, passive-fill ratio, trade pace, quote activity, and inventory turnover.
- System performance measures market-data latency, decision latency, submit latency, reconciliation latency, cycle time, and dashboard cost.

The terminal dashboard is an operational control surface, not only a display.
It shows account buying power, position size, gross exposure, realized and unrealized PnL, trade pace, passive-fill ratio, fees, and slippage.
It also shows session state, time to close, strategy sleeves, symbol-level state, and recent decision diagnostics.

The runtime measures dashboard rendering separately from trading-cycle work.
This keeps observability visible without allowing terminal output to hide hot-path latency.

Operators must use the dashboard with event logs and post-run reports.
A green PnL value does not prove that the strategy is healthy.
The system must also show controlled inventory, acceptable shortfall, fresh reconciliation, and expected passive behavior.

### Decision-support systems

Models support decisions at different time scales.
The hot path uses compact features and bounded formulas.
The warm path updates rolling statistics and execution estimates.
The slow path performs replay, calibration, and model comparison.

This separation allows the system to use richer models without placing expensive computation in the quote loop.
Examples include:

- online volatility and spread estimates for quote width
- queue-share and fill-rate estimates for passive fill probability
- lead-lag and cross-symbol features for allocation overlays
- markout and shortfall models for adverse-selection detection
- ladder-level calibration for quote distance, size, churn, and queue behavior
- replay comparisons for model versions and parameter changes

Models must produce interpretable intermediate values.
Strategy traces record features, parameter bundles, quote gates, model version, allocation weight, and final targets.
This makes it possible to answer why the system quoted, widened, reduced size, or stopped quoting.

Models must not directly override hard controls.
Risk limits, safe modes, stale-data checks, and close logic remain deterministic guardrails around model output.

### Model and system improvement loop

The intended improvement loop is:

1. Capture market state, strategy traces, order events, fills, positions, and timing metrics.
2. Inspect the dashboard during the session for immediate control failures.
3. Run event validation and summary tools after the session.
4. Compare fill quality, markout, shortfall, inventory, and latency by symbol and strategy sleeve.
5. Replay the strategy against deterministic market-state frames.
6. Calibrate ladder levels and execution assumptions from recorded events.
7. Change one model, parameter, or control at a time.
8. Re-run focused tests, replay comparisons, fake-load profiling, and dry smoke sessions.
9. Promote a model only when its economic and operational metrics improve together.

The system favors measured improvement over untracked complexity.
Every new feature must have a decision role, a compute tier, a validation method, and a failure behavior.
Every new model must define its inputs, output range, fallback, diagnostics, and interaction with risk.

Read [src/data/featurespace/V1_FEATURE_SELECTION.md](src/data/featurespace/V1_FEATURE_SELECTION.md) for feature promotion rules.
Read [src/data/featurespace/ONLINE_ALGORITHMS.md](src/data/featurespace/ONLINE_ALGORITHMS.md) for online-state design.
Read [src/strategy/HEURISTIC_CONSTANTS.md](src/strategy/HEURISTIC_CONSTANTS.md) for calibration and replacement plans.
Read [src/app/POST_RUN_REVIEW_TEMPLATE.md](src/app/POST_RUN_REVIEW_TEMPLATE.md) for the review process.

## Requirements

- Python 3.10 or newer.
- The `shift` package for broker-connected sessions.
- NumPy is optional. The feature batch code uses a scalar fallback when NumPy is unavailable.
- `initiator.cfg`, `FIXT11.xml`, and `FIX50SP2.xml` for SHIFT connectivity.
- Credentials supplied through `SHIFT_USERNAME` and `SHIFT_PASSWORD`, or through the local `credentials.py` fallback.

Read [DEPENDENCIES.md](DEPENDENCIES.md) before installing packages.
Do not commit credentials or secret configuration values.

## Run the checks

The repository currently uses the standard library `unittest` runner through `pytest`-compatible test files.
Run the full test suite from the repository root:

```bash
python3 -m pytest -q
```

If `pytest` is not installed, run the test modules directly:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Check the Python version and optional imports with the commands in [DEPENDENCIES.md](DEPENDENCIES.md).

## Run a broker-connected dry smoke session

The smoke entry point performs preflight checks, connects to SHIFT, subscribes to books, runs the runtime, and writes telemetry.
It does not route orders unless you pass `--execute-orders`.

Run a bounded session with explicit symbols:

```bash
SHIFT_USERNAME="your_user" SHIFT_PASSWORD="your_password" \
python3 -m src.app.live_smoke \
  --symbols AAPL XOM \
  --cycles 20 \
  --update-interval-ms 50 \
  --session-dir runs/live_smoke_dry
```

Omit `--cycles` to continue until `Ctrl-C`.
Omit `--symbols` to discover symbols from `trader.get_stock_list()`.
Add `--enable-dashboard` to show the terminal dashboard.

Inspect the generated event log after the session:

```bash
python3 -m src.telemetry.dry_run_validator runs/live_smoke_dry/events.jsonl --tick-size 0.01
python3 -m src.telemetry.dry_run_summary runs/live_smoke_dry/events.jsonl
python3 scripts/log_summary.py runs/live_smoke_dry/events.jsonl
```

The event schema and the required audit chain are documented in [src/telemetry/LOG_SCHEMA.md](src/telemetry/LOG_SCHEMA.md).

## Run a live-order smoke session

Complete the checks in [PRE_LIVE_CHECKLIST.md](PRE_LIVE_CHECKLIST.md) first.
Then add `--execute-orders` to the smoke command and use a small symbol set, short duration, and strict position limits.

```bash
SHIFT_USERNAME="your_user" SHIFT_PASSWORD="your_password" \
python3 -m src.app.live_smoke \
  --symbols AAPL \
  --cycles 20 \
  --max-position-lots-per-symbol 1 \
  --session-dir runs/live_smoke_orders \
  --execute-orders
```

The root launcher in `run.py` is the deployment path.
It enables live order routing and uses its constants for the deployment configuration.
Review [src/app/OPERATIONS_RUNBOOK.md](src/app/OPERATIONS_RUNBOOK.md) before using it.

## Configuration and safety

`src/core/config.py` defines the validated runtime configuration.
The main safety controls include position limits, gross exposure limits, buying-power reserves, stale-book handling, reconciliation health, and safe modes.

The normal runtime sequence is:

1. Load configuration and credentials.
2. Run preflight validation.
3. Connect to SHIFT and subscribe to market data.
4. Build local market state and the trading runtime stack.
5. Reconcile orders, fills, positions, and portfolio state.
6. Generate quote targets and pass them through risk controls.
7. Submit, cancel, or replace orders through the execution layer.
8. Stop market data, cancel live orders, flush telemetry, and disconnect.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for ownership, concurrency, state, and failure semantics.
Read [EDGE_CASES.md](EDGE_CASES.md) for trading and operational edge cases.

## Documentation index

- [BUILD.md](BUILD.md): design goals, SHIFT constraints, economics, and build order.
- [DEPENDENCIES.md](DEPENDENCIES.md): Python, SHIFT, NumPy, FIX files, and credential requirements.
- [ARCHITECTURE.md](ARCHITECTURE.md): system design and data contracts.
- [PRE_LIVE_CHECKLIST.md](PRE_LIVE_CHECKLIST.md): required validation before live routing.
- [src/app/README.md](src/app/README.md): startup, runtime loops, and shutdown.
- [src/app/OPERATIONS_RUNBOOK.md](src/app/OPERATIONS_RUNBOOK.md): operational recovery procedures.
- [src/data/README.md](src/data/README.md): market-data state and feature processing.
- [src/strategy/README.md](src/strategy/README.md): market-making and parameter logic.
- [src/execution/README.md](src/execution/README.md): order lifecycle and reconciliation.
- [src/risk/README.md](src/risk/README.md): inventory and safe-mode controls.
- [src/telemetry/README.md](src/telemetry/README.md): event logging and analysis commands.
- [tests/README.md](tests/README.md): test scope and profiling guidance.

## Development rule

Keep broker calls at the session, market-data, and execution boundaries.
Keep strategy logic deterministic and testable with fake SHIFT interfaces.
Add a focused test for every change that affects risk, order state, reconciliation, or quote generation.
