from __future__ import annotations

from dataclasses import dataclass, field

from src.data.state import MarketState
from src.execution.order_state import QuoteTarget
from src.risk.inventory import PortfolioLedger
from src.strategy.base import StrategyDecisionTrace, StrategyEngine


@dataclass(slots=True)
class ReplayFrame:
    market_state: MarketState
    portfolio_ledger: PortfolioLedger = field(default_factory=PortfolioLedger)


@dataclass(slots=True)
class ReplayStepResult:
    targets: list[QuoteTarget]
    traces: list[StrategyDecisionTrace]


def run_strategy_replay(
    strategy: StrategyEngine,
    frames: list[ReplayFrame],
) -> list[ReplayStepResult]:
    results: list[ReplayStepResult] = []
    for frame in frames:
        strategy.update_market_state(frame.market_state.clone())
        targets, traces = strategy.generate_targets(
            portfolio_ledger=frame.portfolio_ledger,
        )
        results.append(
            ReplayStepResult(
                targets=list(targets),
                traces=list(traces),
            )
        )
    return results
