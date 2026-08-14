from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Sequence

from src.data.state import MarketState
from src.execution.order_state import QuoteTarget
from src.risk.inventory import PortfolioLedger
from src.strategy.params import StrategyParameterBundle
from src.telemetry.logger import EventLoggerLike


@dataclass(slots=True)
class StrategyDiagnostics:
    fair_value_anchor: float = 0.0
    fair_value_center: float = 0.0
    quote_width: float = 0.0
    inventory_skew: float = 0.0
    alpha_bias: float = 0.0
    toxicity_score: float = 0.0
    allocation_weight: float = 1.0
    pace_multiplier: float = 1.0
    regime_code: str = ""
    participation_mode: str = ""
    parameter_source: str = ""
    parameter_version: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StrategyDecisionTrace:
    symbol: str
    strategy_name: str
    diagnostics: StrategyDiagnostics
    quote_target: Optional[QuoteTarget] = None
    decision_reason: str = ""
    model_version: str = ""


class StrategyEngine(Protocol):
    def update_market_state(self, market_state: MarketState) -> None: ...

    def generate_targets(
        self,
        *,
        portfolio_ledger: PortfolioLedger,
    ) -> tuple[Sequence[QuoteTarget], Sequence[StrategyDecisionTrace]]: ...


def log_strategy_trace(
    event_logger: Optional[EventLoggerLike],
    trace: StrategyDecisionTrace,
) -> None:
    if event_logger is None:
        return
    target = trace.quote_target
    event_logger.log(
        "strategy_trace",
        symbol=trace.symbol,
        strategy_name=trace.strategy_name,
        model_version=trace.model_version,
        decision_reason=trace.decision_reason,
        fair_value_anchor=trace.diagnostics.fair_value_anchor,
        fair_value_center=trace.diagnostics.fair_value_center,
        quote_width=trace.diagnostics.quote_width,
        inventory_skew=trace.diagnostics.inventory_skew,
        alpha_bias=trace.diagnostics.alpha_bias,
        toxicity_score=trace.diagnostics.toxicity_score,
        allocation_weight=trace.diagnostics.allocation_weight,
        pace_multiplier=trace.diagnostics.pace_multiplier,
        regime_code=trace.diagnostics.regime_code,
        participation_mode=trace.diagnostics.participation_mode,
        parameter_source=trace.diagnostics.parameter_source,
        parameter_version=trace.diagnostics.parameter_version,
        extra=trace.diagnostics.extra,
        quote_target={
            "symbol": target.symbol,
            "bid_px": target.bid_px,
            "ask_px": target.ask_px,
            "bid_size": target.bid_size,
            "ask_size": target.ask_size,
            "enable_bid": target.enable_bid,
            "enable_ask": target.enable_ask,
            "flatten_mode": target.flatten_mode,
            "reason": target.reason,
            "bid_levels": [
                {
                    "level_index": level.level_index,
                    "price": level.price,
                    "size": level.size,
                    "queue_ahead_lots": level.queue_ahead_lots,
                    "queue_share": level.queue_share,
                    "enabled": level.enabled,
                }
                for level in target.bid_levels
            ],
            "ask_levels": [
                {
                    "level_index": level.level_index,
                    "price": level.price,
                    "size": level.size,
                    "queue_ahead_lots": level.queue_ahead_lots,
                    "queue_share": level.queue_share,
                    "enabled": level.enabled,
                }
                for level in target.ask_levels
            ],
        } if target is not None else None,
    )
