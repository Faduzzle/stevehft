from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.core.config import RuntimeConfig
from src.execution.order_router import OrderRouter
from src.execution.order_state import OrderLedger
from src.execution.reconciler import Reconciler, ReconciliationConfig, TraderLike
from src.execution.shift_orders import ShiftPyOrderFactory
from src.risk.inventory import PortfolioLedger
from src.risk.kill_switch import KillSwitchController, SafeModeConfig
from src.risk.limits import RiskLimits, RiskLimitsConfig
from src.telemetry.logger import EventLoggerLike


@dataclass(slots=True)
class TradingRuntimeStack:
    order_ledger: OrderLedger
    portfolio_ledger: PortfolioLedger
    risk_limits: RiskLimits
    kill_switch: KillSwitchController
    order_factory: ShiftPyOrderFactory
    router: OrderRouter
    reconciler: Reconciler


def build_trading_runtime_stack(
    trader: TraderLike,
    runtime_config: RuntimeConfig,
    *,
    event_logger: Optional[EventLoggerLike] = None,
) -> TradingRuntimeStack:
    order_ledger = OrderLedger(tick_size=runtime_config.strategy.tick_size)
    portfolio_ledger = PortfolioLedger()
    kill_switch = KillSwitchController(
        SafeModeConfig(
            max_waiting_list_staleness_ms=runtime_config.risk.stale_book_after_ms,
            max_portfolio_staleness_ms=runtime_config.risk.stale_book_after_ms,
            max_position_mismatch_lots=max(
                1,
                runtime_config.risk.max_position_lots_per_symbol // 3,
            ),
        ),
        event_logger=event_logger,
    )
    risk_limits = RiskLimits(
        RiskLimitsConfig(
            max_position_lots_per_symbol=runtime_config.risk.max_position_lots_per_symbol,
            max_gross_position_lots=runtime_config.risk.max_gross_position_lots,
            buying_power_reserve_fraction=(
                runtime_config.risk.buying_power_reserve_fraction
            ),
        ),
        portfolio_ledger=portfolio_ledger,
        order_ledger=order_ledger,
    )
    order_factory = ShiftPyOrderFactory()
    router = OrderRouter(
        trader,
        order_factory,
        order_ledger,
        risk_limits=risk_limits,
        kill_switch=kill_switch,
        event_logger=event_logger,
    )
    reconciler = Reconciler(
        trader,
        order_ledger,
        portfolio_ledger,
        ReconciliationConfig(
            stale_order_after_ms=runtime_config.risk.stale_order_after_ms,
            new_order_grace_ms=1_000,
            max_waiting_list_sync_failures=2,
            max_portfolio_sync_failures=2,
            max_parse_failures=2,
            tick_size=runtime_config.strategy.tick_size,
        ),
        kill_switch=kill_switch,
        event_logger=event_logger,
    )
    return TradingRuntimeStack(
        order_ledger=order_ledger,
        portfolio_ledger=portfolio_ledger,
        risk_limits=risk_limits,
        kill_switch=kill_switch,
        order_factory=order_factory,
        router=router,
        reconciler=reconciler,
    )
