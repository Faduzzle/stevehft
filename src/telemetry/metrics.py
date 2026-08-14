from __future__ import annotations

from dataclasses import dataclass

from src.execution.order_state import OrderLedger


@dataclass(slots=True)
class LatencySnapshot:
    market_data_ns: int = 0
    decision_ns: int = 0
    submit_ns: int = 0
    reconcile_ns: int = 0


@dataclass(slots=True)
class SessionMetrics:
    executed_trades: int = 0
    executed_shares: int = 0
    passive_fills: int = 0
    aggressive_fills: int = 0
    estimated_fees: float = 0.0
    estimated_rebates: float = 0.0
    estimated_net_fees: float = 0.0
    fill_vwap: float = 0.0
    fill_twap: float = 0.0
    arrival_shortfall: float = 0.0
    decision_shortfall: float = 0.0

    @property
    def passive_fill_ratio(self) -> float:
        total_fills = self.passive_fills + self.aggressive_fills
        if total_fills <= 0:
            return 0.0
        return self.passive_fills / total_fills


def build_session_metrics(order_ledger: OrderLedger) -> SessionMetrics:
    metrics = SessionMetrics(
        executed_trades=order_ledger.total_fill_count(),
        executed_shares=order_ledger.total_executed_shares(),
        passive_fills=order_ledger.passive_fill_count(),
        aggressive_fills=order_ledger.aggressive_fill_count(),
        estimated_fees=order_ledger.estimated_total_fee,
        estimated_rebates=order_ledger.estimated_total_rebate,
        estimated_net_fees=order_ledger.estimated_total_net_fee,
        fill_vwap=order_ledger.session_fill_vwap,
        fill_twap=order_ledger.session_fill_twap,
        arrival_shortfall=order_ledger.session_arrival_shortfall_total,
        decision_shortfall=order_ledger.session_decision_shortfall_total,
    )
    return metrics
