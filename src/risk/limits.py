from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.execution.order_state import (
    LOT_SIZE_SHARES,
    OrderCommand,
    OrderIntentAction,
    OrderLedger,
    OrderSide,
)
from src.risk.inventory import PortfolioLedger


@dataclass(slots=True)
class RiskLimitsConfig:
    max_position_lots_per_symbol: int = 5
    max_gross_position_lots: int = 0
    max_open_orders_per_symbol: int = 4
    min_buying_power: float = 0.0
    buying_power_reserve_fraction: float = 0.0
    flatten_only: bool = False


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str = ""


@dataclass(slots=True)
class RiskEvaluation:
    decisions: List[RiskDecision] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return all(decision.allowed for decision in self.decisions)

    def first_reject_reason(self) -> str:
        for decision in self.decisions:
            if not decision.allowed:
                return decision.reason
        return ""


class RiskLimits:
    def __init__(
        self,
        config: RiskLimitsConfig,
        portfolio_ledger: PortfolioLedger,
        order_ledger: OrderLedger,
    ) -> None:
        self._config = config
        self._portfolio_ledger = portfolio_ledger
        self._order_ledger = order_ledger

    def evaluate(self, command: OrderCommand) -> RiskEvaluation:
        decisions = [
            self._check_flatten_only(command),
            self._check_symbol_position_limit(command),
            self._check_gross_position_limit(command),
            self._check_open_order_limit(command),
            self._check_buying_power(command),
        ]
        return RiskEvaluation(decisions=decisions)

    def is_allowed(self, command: OrderCommand) -> bool:
        return self.evaluate(command).allowed

    def set_flatten_only(self, flatten_only: bool) -> None:
        self._config.flatten_only = flatten_only

    def get_net_lots(self, symbol: str) -> int:
        return self._portfolio_ledger.get_net_lots(symbol)

    def _check_flatten_only(self, command: OrderCommand) -> RiskDecision:
        if not self._config.flatten_only:
            return RiskDecision(True)
        if command.action in {OrderIntentAction.CANCEL, OrderIntentAction.FLATTEN}:
            return RiskDecision(True)

        net_lots = self._portfolio_ledger.get_net_lots(command.symbol)
        if net_lots > 0 and command.side.value == "ask":
            return RiskDecision(True)
        if net_lots < 0 and command.side.value == "bid":
            return RiskDecision(True)
        return RiskDecision(False, "flatten_only_mode")

    def _check_symbol_position_limit(self, command: OrderCommand) -> RiskDecision:
        if command.action != OrderIntentAction.SUBMIT:
            return RiskDecision(True)

        projected_abs_lots = self._projected_symbol_abs_lots(
            command.symbol,
            command=command,
        )
        if projected_abs_lots > self._config.max_position_lots_per_symbol:
            return RiskDecision(False, "symbol_position_limit")
        return RiskDecision(True)

    def _check_gross_position_limit(self, command: OrderCommand) -> RiskDecision:
        if (
            command.action != OrderIntentAction.SUBMIT
            or self._config.max_gross_position_lots <= 0
        ):
            return RiskDecision(True)

        gross_lots = sum(
            self._projected_symbol_abs_lots(symbol)
            for symbol in set(self._portfolio_ledger.positions)
            | set(self._order_ledger.by_symbol)
        )
        projected_gross_lots = (
            gross_lots
            - self._projected_symbol_abs_lots(command.symbol)
            + self._projected_symbol_abs_lots(
                command.symbol,
                command=command,
            )
        )
        if projected_gross_lots > self._config.max_gross_position_lots:
            return RiskDecision(False, "gross_position_limit")
        return RiskDecision(True)

    def _check_open_order_limit(self, command: OrderCommand) -> RiskDecision:
        if command.action != OrderIntentAction.SUBMIT:
            return RiskDecision(True)

        open_orders = len(
            self._order_ledger.live_orders(symbol=command.symbol)
        )
        if open_orders >= self._config.max_open_orders_per_symbol and command.action == OrderIntentAction.SUBMIT:
            return RiskDecision(False, "open_order_limit")
        return RiskDecision(True)

    def _check_buying_power(self, command: OrderCommand) -> RiskDecision:
        if command.action not in {
            OrderIntentAction.SUBMIT,
            OrderIntentAction.FLATTEN,
        }:
            return RiskDecision(True)
        if (
            command.action == OrderIntentAction.FLATTEN
            and command.side != OrderSide.BID
        ):
            return RiskDecision(True)
        projected_available_bp = (
            self._portfolio_ledger.summary.total_bp
            - self._order_ledger.estimated_reserved_buying_power
            - self._estimated_existing_short_order_cover_bp()
            - self._estimated_command_bp(command)
        )
        required_bp_buffer = max(
            self._config.min_buying_power,
            self._portfolio_ledger.summary.total_bp
            * max(self._config.buying_power_reserve_fraction, 0.0),
        )
        if projected_available_bp < required_bp_buffer:
            return RiskDecision(False, "buying_power_floor")
        return RiskDecision(True)

    def _estimated_command_bp(self, command: OrderCommand) -> float:
        command_price = command.price
        if command_price is None or command_price <= 0.0:
            command_price = command.decision_price
        if command_price <= 0.0 or command.size <= 0:
            return 0.0
        notional = command.size * LOT_SIZE_SHARES * command_price
        if command.action == OrderIntentAction.FLATTEN:
            return notional
        if command.side.value != "ask":
            return notional

        current_long_lots = max(self._portfolio_ledger.get_net_lots(command.symbol), 0)
        existing_ask_lots = self._working_order_lots(command.symbol, "ask")
        uncovered_before = max(existing_ask_lots - current_long_lots, 0)
        uncovered_after = max(existing_ask_lots + command.size - current_long_lots, 0)
        new_short_lots = max(uncovered_after - uncovered_before, 0)
        return 2.0 * new_short_lots * LOT_SIZE_SHARES * command_price

    def _estimated_existing_short_order_cover_bp(self) -> float:
        reserved_short_cover_bp = 0.0
        for symbol in self._order_ledger.by_symbol:
            ask_orders = [
                order
                for order in self._order_ledger.live_orders(
                    symbol=symbol,
                    side=OrderSide.ASK,
                )
                if order.price > 0.0 and order.status == "pending_new"
            ]
            if not ask_orders:
                continue
            current_long_lots = max(self._portfolio_ledger.get_net_lots(symbol), 0)
            total_ask_lots = sum(order.remaining_size for order in ask_orders)
            worst_cover_px = max(order.price for order in ask_orders)
            uncovered_ask_lots = max(total_ask_lots - current_long_lots, 0)
            reserved_short_cover_bp += (
                uncovered_ask_lots * LOT_SIZE_SHARES * worst_cover_px
            )
        return reserved_short_cover_bp

    def _working_order_lots(self, symbol: str, side_value: str) -> int:
        side = OrderSide.BID if side_value == "bid" else OrderSide.ASK
        return sum(
            order.remaining_size
            for order in self._order_ledger.live_orders(symbol=symbol, side=side)
        )

    def _projected_symbol_abs_lots(
        self,
        symbol: str,
        *,
        command: OrderCommand | None = None,
    ) -> int:
        net_lots = self._portfolio_ledger.get_net_lots(symbol)
        bid_lots = self._working_order_lots(symbol, "bid")
        ask_lots = self._working_order_lots(symbol, "ask")
        if command is not None:
            if command.side.value == "bid":
                bid_lots += command.size
            else:
                ask_lots += command.size
        projected_long = net_lots + bid_lots
        projected_short = net_lots - ask_lots
        return max(abs(projected_long), abs(projected_short))
