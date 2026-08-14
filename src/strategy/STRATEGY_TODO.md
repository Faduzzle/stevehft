# Strategy TODO

This document tracks higher-order market-making upgrades that are not in the V1
single-symbol CJ/GLFT loop yet, but are worth building once the single-symbol
path is stable and profiled.

## 1. Lead-Lag Alpha ("Shadow Quote")

Goal:

- use a more liquid or faster "leader" symbol as a predictive signal for a
  correlated "follower" symbol

Strategy idea:

- monitor leader OFI / OBI, VOI, trades, and L1 mid moves
- if the leader's touch moves, queue depth vanishes, or signed flow shifts
  strongly, pre-shift the follower's quote center/ladder before the follower
  itself trades

Why this helps:

- lowers adverse selection by reacting to the source-of-truth ticker, not only
  our own ticker's stale local book

Implementation notes:

- add a cross-symbol feature store keyed by leader/follower groups
- extend `AdaptiveParameterProvider` so follower symbols can consume a bounded
  lead-lag signal from leader state
- keep the shadow shift capped in ticks so it acts as a small center adjustment,
  not a second fully independent strategy
- add diagnostics to `mm_trace.py` such as `leader_symbol`,
  `leader_obi`, `leader_mid_drift`, and `lead_lag_shift_ticks`

## 2. Pairs Trading With A Market-Making Overlay

Goal:

- quote both legs while leaning inventory and fair value from the spread
  residual `A - beta * B`

Strategy idea:

- estimate `beta` and a rolling spread mean/z-score online
- if the spread is wide positive, bias A to sell and B to buy
- if the spread is wide negative, bias A to buy and B to sell
- keep both sides mostly passive so we still earn maker rebates on each leg

Why this helps:

- lets the MM capture bid/ask on both tickers while maintaining a more
  market-neutral position

Implementation notes:

- add a pair-model layer above per-symbol parameters
- estimate `beta` with an online method such as RLS and compute a spread
  z-score with `DecayingWelford`
- pass pair residual and pair pressure into `SymbolStrategyParameters`
- ensure the reconciler/risk layer understands that a pair overlay may want one
  symbol bid-heavy while its partner is ask-heavy
- start with a small pair-shift overlay only; do not rewrite the whole
  single-symbol MM into pair-only logic

## 3. Synthetic Mid / Cross-Symbol Fair Value

Goal:

- build a smoother fair-value anchor from a correlated basket instead of only
  one symbol's own mid

Strategy idea:

- compute a volume-weighted synthetic mid from a correlation group:
  `synthetic_mid = sum(w_i * mid_i) / sum(w_i)`
- compare each symbol's own mid/microprice to that synthetic mid and use the
  difference as a small fair-value correction

Why this helps:

- filters idiosyncratic one-symbol noise
- reduces overreaction in the CJ/GLFT center when one ticker has a temporary
  odd lot or local-book distortion

Implementation notes:

- define static or learned correlation groups
- add a basket fair-value service in `src/strategy/`
- expose `synthetic_mid`, `synthetic_mid_basis_ticks`, and
  `basket_deviation_zscore` in strategy traces
- be careful with stale constituents: if one group member has stale books,
  exclude it or haircut its weight

## 4. Cross-Symbol Inventory Management

Goal:

- manage net exposure at the correlation-group level, not only one symbol at a
  time

Strategy idea:

- estimate group-level net delta from current positions and beta weights
- if long A and short B are mostly offsetting, avoid over-skewing A just because
  its standalone inventory is nonzero
- only apply strong inventory-hocking when group net delta or gross exposure is
  truly large

Why this helps:

- prevents unnecessary one-leg liquidation when another correlated leg already
  hedges the exposure

Implementation notes:

- add a group exposure model on top of `PortfolioLedger`
- compute both symbol inventory and group net delta / group gross
- extend `QuoteTarget` or strategy params with group inventory pressure
- apply a group-aware multiplier to `gamma_inventory` and inventory skew
- keep hard per-symbol and gross caps in `src/risk/`; this feature should soften
  skew, not bypass safety limits

## 5. Single-Symbol Physics-Informed Momentum

Goal:

- add a single-symbol momentum overlay that reacts to short-horizon price/flow
  acceleration, but scales that signal by a notion of market "mass"

Strategy idea:

- track multiple "velocities" for each symbol:
  - fast mid-price velocity from short-horizon mid moves
  - slower drift velocity from EWMA / RLS predicted mid moves
  - queue-flow velocity from local VOI / OBI changes
  - tape velocity from trade-linked VOI and signed trade volume
- track two complementary "mass" terms:
  - liquidity mass `m_liq ~ local_depth + recent_trade_volume`, meaning thick
    books should have more inertia and smaller center jumps
  - impact mass `m_imp ~ 1 / max(local_depth + recent_trade_volume, eps)`,
    meaning thin books should treat the same velocity as more dangerous because
    a small flow shock can move price more
- form bounded momentum/force features such as:
  - `stable_momentum = v_slow * m_liq_norm`
  - `impact_force = v_fast * m_imp_norm`
  - `net_momentum_shift_ticks = clip(a * stable_momentum + b * impact_force, ...)`

Why this helps:

- lets the strategy distinguish "same velocity in a heavy book" from
  "same velocity in a thin book"
- can reduce adverse selection by widening or skewing faster when a thin book
  starts moving, while avoiding overreacting to noisy prints in deep books

Implementation notes:

- compute multi-horizon velocity features in `param_observer.py`
- reuse `DecayingWelford`, `PSquareQuantile`, and `RecursiveLeastSquares` for
  normalized velocity and predicted drift state
- derive `liquidity_mass`, `impact_mass`, and a bounded
  `physics_momentum_shift_ticks` in `params.py`
- blend this shift into `mm_pipeline.py` center construction with a small cap
  so momentum cannot dominate inventory control or quote validity
- expose `fast_velocity_ticks`, `slow_velocity_ticks`, `liquidity_mass`,
  `impact_mass`, and `physics_momentum_shift_ticks` in `mm_trace.py`
- do not let this overlay directly trigger hard flatten; it should first widen,
  skew, or reduce size, and only influence taker exits when inventory and
  markout are already unfavorable

## Build Order

Suggested order after V1 single-symbol stability:

1. synthetic mid from static correlation groups
2. cross-symbol inventory pressure
3. lead-lag shadow quote from a small manually chosen leader/follower map
4. pair spread residual overlay with online beta
5. single-symbol physics-informed momentum overlay

Reason:

- synthetic mid and group inventory are easier to validate and less likely to
  create accidental overtrading
- lead-lag and pair residuals are stronger alpha ideas, but they need cleaner
  calibration and tighter trace diagnostics first
