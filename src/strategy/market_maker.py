from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Sequence

from src.data.state import MarketState
from src.data.state import SymbolState
from src.core.session_clock import SessionClock
from src.execution.order_state import OrderLedger
from src.execution.order_state import QuoteTarget
from src.risk.inventory import PortfolioLedger
from src.risk.kill_switch import KillSwitchController
from src.strategy.base import StrategyDecisionTrace
from src.strategy.mm_feature_batch import build_market_making_feature_batch
from src.strategy.mm_pipeline import MarketMakingQuotePlan
from src.strategy.mm_pipeline import build_quote_plan
from src.strategy.mm_trace import build_market_making_trace
from src.strategy.params import (
    AdaptiveParameterProvider,
    StrategyParameterBundle,
    StrategyParameterProvider,
    SymbolStrategyParameters,
)


@dataclass(slots=True)
class TopOfBookMarketMakerConfig:
    tick_size: float = 0.01
    # Set <= 0 to disable static gross-lot rationing and rely on router BP reserve.
    max_gross_position_lots: int = 0
    strategy_name: str = "top_of_book_market_maker"
    model_version: str = "v1-cj-glft-mm"
    default_parameters: SymbolStrategyParameters = field(default_factory=SymbolStrategyParameters)


@dataclass(slots=True)
class _PreparedQuote:
    row_symbol: str
    row_state: SymbolState
    inventory_lots: int
    book_age_ms: float
    spread_ticks: float
    bundle: StrategyParameterBundle
    gate_reason: str
    plan: MarketMakingQuotePlan
    target: QuoteTarget
    priority_score: float
    live_bid_lots: int
    live_ask_lots: int
    gross_delta_lots: int


class TopOfBookMarketMaker:
    """A structured market maker with data-driven CJ/GLFT-style quoting blocks.

    Online estimator contract:
    - Fast EWMA signals (local OBI, queue, fill/cancel, toxicity) should move
      center/width/size quickly every book update.
    - Slower DecayingWelford / P-Square summaries should set baseline spread,
      quote-age, and volatility scales.
    - CUSUM / Page-Hinkley regime breaks should not liquidate by themselves;
      they apply a temporary width-up / size-down overlay while inventory and
      session state remain the main hard risk gates.
    - LUTs are the monotone nonlinear policy maps from toxicity/inventory
      pressure into multipliers, so those effects stay bounded and predictable.

    - quote gate / suppression
    - fair-value anchor
    - inventory skew
    - width selection
    - one-sided inventory protection
    """

    def __init__(
        self,
        market_state: MarketState,
        config: TopOfBookMarketMakerConfig | None = None,
        parameter_provider: StrategyParameterProvider | None = None,
        order_ledger: OrderLedger | None = None,
        session_clock: SessionClock | None = None,
        kill_switch: KillSwitchController | None = None,
    ) -> None:
        self._market_state = market_state
        self._config = config or TopOfBookMarketMakerConfig()
        self._order_ledger = order_ledger
        self._parameter_provider = parameter_provider or AdaptiveParameterProvider(
            market_state=self._market_state,
            tick_size=self._config.tick_size,
            order_ledger=order_ledger,
            portfolio_ledger=None,
            session_clock=session_clock,
            kill_switch=kill_switch,
            fallback=self._config.default_parameters,
            version=self._config.model_version,
        )

    def update_market_state(self, market_state: MarketState) -> None:
        self._market_state = market_state
        if isinstance(self._parameter_provider, AdaptiveParameterProvider):
            self._parameter_provider.market_state = market_state

    def reset_adaptive_state(self) -> None:
        if isinstance(self._parameter_provider, AdaptiveParameterProvider):
            self._parameter_provider.reset_adaptive_state()

    def generate_targets(
        self,
        *,
        portfolio_ledger: PortfolioLedger,
    ) -> tuple[Sequence[QuoteTarget], Sequence[StrategyDecisionTrace]]:
        if isinstance(self._parameter_provider, AdaptiveParameterProvider):
            self._parameter_provider.portfolio_ledger = portfolio_ledger
        targets: List[QuoteTarget] = []
        traces: List[StrategyDecisionTrace] = []
        now_ns = time.monotonic_ns()
        feature_batch = build_market_making_feature_batch(
            self._market_state,
            portfolio_ledger,
            now_ns=now_ns,
            tick_size=self._config.tick_size,
        )
        if isinstance(self._parameter_provider, AdaptiveParameterProvider):
            self._parameter_provider.update_lead_lag_graph(feature_batch.rows)

        bundles = []
        max_staleness_ms: list[int] = []
        max_spread_ticks: list[int] = []

        for row in feature_batch.rows:
            bundle = self._parameter_provider.for_symbol(
                row.symbol,
                inventory_lots=row.inventory_lots,
            )
            bundles.append(bundle)
            max_staleness_ms.append(bundle.values.quote_age_limit_ms)
            max_spread_ticks.append(bundle.values.max_live_spread_ticks)

        gates = feature_batch.compute_quote_gates(
            max_staleness_ms=max_staleness_ms,
            max_spread_ticks=max_spread_ticks,
        )

        live_order_lots_by_symbol = _live_order_lots_by_symbol(self._order_ledger)
        prepared_quotes: list[_PreparedQuote] = []
        for row, bundle, gate in zip(feature_batch.rows, bundles, gates):
            params = bundle.values
            plan = build_quote_plan(
                symbol_state=row.state,
                params=params,
                inventory_lots=row.inventory_lots,
                gate=gate,
                tick_size=self._config.tick_size,
            )
            target = plan.to_quote_target(
                symbol=row.symbol,
                params=params,
                tick_size=self._config.tick_size,
            )
            prepared_quotes.append(
                _PreparedQuote(
                    row_symbol=row.symbol,
                    row_state=row.state,
                    inventory_lots=row.inventory_lots,
                    book_age_ms=row.book_age_ms,
                    spread_ticks=row.spread_ticks,
                    bundle=bundle,
                    gate_reason="quotable" if gate.allow_quotes else gate.reason,
                    plan=plan,
                    target=target,
                    priority_score=_quote_priority_score(
                        params,
                        inventory_lots=row.inventory_lots,
                    ),
                    live_bid_lots=live_order_lots_by_symbol.get(row.symbol, (0, 0))[0],
                    live_ask_lots=live_order_lots_by_symbol.get(row.symbol, (0, 0))[1],
                    gross_delta_lots=_quote_gross_delta_lots(
                        target,
                        inventory_lots=row.inventory_lots,
                        live_bid_lots=live_order_lots_by_symbol.get(row.symbol, (0, 0))[0],
                        live_ask_lots=live_order_lots_by_symbol.get(row.symbol, (0, 0))[1],
                    ),
                )
            )

        _apply_gross_quote_budget(
            prepared_quotes,
            max_gross_position_lots=self._config.max_gross_position_lots,
        )

        for prepared in prepared_quotes:
            trace = build_market_making_trace(
                symbol=prepared.row_symbol,
                strategy_name=self._config.strategy_name,
                model_version=self._config.model_version,
                state=prepared.row_state,
                bundle=prepared.bundle,
                plan=prepared.plan,
                gate_reason=prepared.gate_reason,
                target=prepared.target,
                inventory_lots=prepared.inventory_lots,
                book_age_ms=prepared.book_age_ms,
                spread_ticks=prepared.spread_ticks,
            )
            targets.append(prepared.target)
            traces.append(trace)

        return targets, traces


def _quote_priority_score(
    params: SymbolStrategyParameters,
    *,
    inventory_lots: int,
) -> float:
    inventory_priority = 1.0 + 0.35 * min(abs(inventory_lots), params.inventory_limit_lots)
    return (
        params.allocation_weight
        * (0.50 + 0.50 * params.queue_fill_support)
        * params.slippage_quality_score
        * inventory_priority
    )


def _quote_gross_delta_lots(
    target: QuoteTarget,
    *,
    inventory_lots: int,
    live_bid_lots: int = 0,
    live_ask_lots: int = 0,
) -> int:
    current_abs_lots = max(
        abs(inventory_lots + max(live_bid_lots, 0)),
        abs(inventory_lots - max(live_ask_lots, 0)),
    )
    bid_lots = sum(level.size for level in target.bid_levels if level.enabled)
    ask_lots = sum(level.size for level in target.ask_levels if level.enabled)
    projected_abs_lots = max(
        abs(inventory_lots + max(max(live_bid_lots, 0), bid_lots)),
        abs(inventory_lots - max(max(live_ask_lots, 0), ask_lots)),
    )
    return max(projected_abs_lots - current_abs_lots, 0)


def _apply_gross_quote_budget(
    prepared_quotes: list[_PreparedQuote],
    *,
    max_gross_position_lots: int,
) -> None:
    if max_gross_position_lots <= 0:
        return

    used_gross_lots = sum(
        max(
            abs(prepared.inventory_lots + max(prepared.live_bid_lots, 0)),
            abs(prepared.inventory_lots - max(prepared.live_ask_lots, 0)),
        )
        for prepared in prepared_quotes
    )
    remaining_gross_lots = max(max_gross_position_lots - used_gross_lots, 0)
    ranked = sorted(
        prepared_quotes,
        key=lambda prepared: prepared.priority_score,
        reverse=True,
    )
    for rank_index, prepared in enumerate(ranked):
        min_slots_for_remaining_flat_symbols = sum(
            1
            for later in ranked[rank_index + 1:]
            if later.inventory_lots == 0 and later.gross_delta_lots > 0
        )
        max_symbol_opening_lots = remaining_gross_lots
        if (
            prepared.inventory_lots == 0
            and prepared.gross_delta_lots > 1
            and remaining_gross_lots <= min_slots_for_remaining_flat_symbols + 1
        ):
            max_symbol_opening_lots = 1

        if prepared.gross_delta_lots <= remaining_gross_lots:
            if (
                max_symbol_opening_lots < prepared.gross_delta_lots
                and _clip_opening_quote_to_gross_budget(
                    prepared.plan,
                    prepared.target,
                    inventory_lots=prepared.inventory_lots,
                    max_opening_lots=max_symbol_opening_lots,
                )
            ):
                prepared.gross_delta_lots = _quote_gross_delta_lots(
                    prepared.target,
                    inventory_lots=prepared.inventory_lots,
                    live_bid_lots=prepared.live_bid_lots,
                    live_ask_lots=prepared.live_ask_lots,
                )
            remaining_gross_lots -= prepared.gross_delta_lots
            continue
        if _clip_opening_quote_to_gross_budget(
            prepared.plan,
            prepared.target,
            inventory_lots=prepared.inventory_lots,
            max_opening_lots=max_symbol_opening_lots,
        ):
            prepared.gross_delta_lots = _quote_gross_delta_lots(
                prepared.target,
                inventory_lots=prepared.inventory_lots,
                live_bid_lots=prepared.live_bid_lots,
                live_ask_lots=prepared.live_ask_lots,
            )
            remaining_gross_lots -= prepared.gross_delta_lots
            continue
        _disable_opening_quote_sides(
            prepared.plan,
            prepared.target,
            inventory_lots=prepared.inventory_lots,
        )
        prepared.gate_reason = "gross_budget_suppressed"


def _clip_opening_quote_to_gross_budget(
    plan: MarketMakingQuotePlan,
    target: QuoteTarget,
    *,
    inventory_lots: int,
    max_opening_lots: int,
) -> bool:
    if max_opening_lots <= 0:
        return False
    if inventory_lots > 0:
        return _clip_target_side_levels(
            plan,
            target,
            side="bid",
            max_total_lots=max_opening_lots,
        )
    if inventory_lots < 0:
        return _clip_target_side_levels(
            plan,
            target,
            side="ask",
            max_total_lots=max_opening_lots,
        )
    bid_ok = _clip_target_side_levels(
        plan,
        target,
        side="bid",
        max_total_lots=max_opening_lots,
    )
    ask_ok = _clip_target_side_levels(
        plan,
        target,
        side="ask",
        max_total_lots=max_opening_lots,
    )
    return bid_ok or ask_ok


def _clip_target_side_levels(
    plan: MarketMakingQuotePlan,
    target: QuoteTarget,
    *,
    side: str,
    max_total_lots: int,
) -> bool:
    levels = list(target.side_levels(side))
    remaining_lots = max(max_total_lots, 0)
    for level in levels:
        if not level.enabled or level.size <= 0:
            continue
        level.size = min(level.size, remaining_lots)
        level.enabled = level.size > 0
        remaining_lots -= level.size

    top_size = levels[0].size if levels and levels[0].enabled else 0
    enabled = any(level.enabled and level.size > 0 for level in levels)
    if side == "bid":
        target.bid_size = top_size
        target.enable_bid = enabled
        plan.bid_size = top_size
        plan.enable_bid = enabled
    else:
        target.ask_size = top_size
        target.enable_ask = enabled
        plan.ask_size = top_size
        plan.enable_ask = enabled
    return enabled


def _disable_opening_quote_sides(
    plan: MarketMakingQuotePlan,
    target: QuoteTarget,
    *,
    inventory_lots: int,
) -> None:
    if inventory_lots > 0:
        plan.enable_bid = False
        plan.bid_size = 0
        target.enable_bid = False
        target.bid_size = 0
        for level in target.bid_levels:
            level.enabled = False
            level.size = 0
        return
    if inventory_lots < 0:
        plan.enable_ask = False
        plan.ask_size = 0
        target.enable_ask = False
        target.ask_size = 0
        for level in target.ask_levels:
            level.enabled = False
            level.size = 0
        return
    plan.enable_bid = False
    plan.enable_ask = False
    plan.bid_size = 0
    plan.ask_size = 0
    plan.reason = "gross_budget_suppressed"
    target.enable_bid = False
    target.enable_ask = False
    target.bid_size = 0
    target.ask_size = 0
    target.reason = "gross_budget_suppressed"
    for level in (*target.bid_levels, *target.ask_levels):
        level.enabled = False
        level.size = 0


def _live_order_lots_by_symbol(
    order_ledger: OrderLedger | None,
) -> dict[str, tuple[int, int]]:
    if order_ledger is None:
        return {}
    live_order_lots_by_symbol: dict[str, list[int]] = {}
    for order in order_ledger.live_orders():
        side_lots = live_order_lots_by_symbol.setdefault(order.symbol, [0, 0])
        if order.side.value == "bid":
            side_lots[0] += max(order.remaining_size, 0)
        else:
            side_lots[1] += max(order.remaining_size, 0)
    return {
        symbol: (side_lots[0], side_lots[1])
        for symbol, side_lots in live_order_lots_by_symbol.items()
    }
