from __future__ import annotations

import time
from typing import Optional, Protocol

from .order_state import (
    OrderCommand,
    OrderIntentAction,
    OrderLedger,
    OrderLiquidity,
    OrderSide,
    WorkingOrder,
)
from src.risk.kill_switch import KillSwitchController, SafeMode
from src.risk.limits import RiskLimits
from src.telemetry.logger import EventLoggerLike


class TraderLike(Protocol):
    def submit_order(self, order) -> None: ...

    def submit_cancellation(self, order) -> None: ...


class ShiftOrderFactory(Protocol):
    def build_limit_order(self, symbol: str, side: OrderSide, size: int, price: float): ...

    def build_market_order(self, symbol: str, side: OrderSide, size: int): ...

    def build_cancel_order(self, live_order: WorkingOrder): ...


class OrderRouter:
    _RECENT_INACTIVE_DUPLICATE_GUARD_NS = 2_000_000_000

    def __init__(
        self,
        trader: TraderLike,
        order_factory: ShiftOrderFactory,
        order_ledger: OrderLedger,
        risk_limits: RiskLimits | None = None,
        kill_switch: KillSwitchController | None = None,
        event_logger: Optional[EventLoggerLike] = None,
    ) -> None:
        self._trader = trader
        self._order_factory = order_factory
        self._order_ledger = order_ledger
        self._risk_limits = risk_limits
        self._kill_switch = kill_switch
        self._event_logger = event_logger

    def apply(self, command: OrderCommand) -> None:
        if self._event_logger is not None:
            self._event_logger.log("order_command", command=command)
        if not self._preflight_allows(command):
            return
        if command.action == OrderIntentAction.SUBMIT:
            self._submit(command)
            return
        if command.action == OrderIntentAction.CANCEL:
            self._cancel(command)
            return
        if command.action == OrderIntentAction.REPLACE:
            self._replace(command)
            return
        if command.action == OrderIntentAction.FLATTEN:
            self._flatten(command)
            return
        if self._event_logger is not None:
            self._event_logger.log(
                "order_command_ignored",
                action=command.action,
                symbol=command.symbol,
                side=command.side,
                order_id=command.order_id,
                reason=command.reason,
            )

    def _preflight_allows(self, command: OrderCommand) -> bool:
        reduces_risk = self._reduces_risk(command)
        if self._kill_switch is not None and self._kill_switch.blocks(
            command.action,
            reduces_risk=reduces_risk,
        ):
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_blocked_safe_mode",
                    action=command.action,
                    symbol=command.symbol,
                    side=command.side,
                    order_id=command.order_id,
                    mode=self._kill_switch.mode.value,
                    reduces_risk=reduces_risk,
                )
            return False

        if self._risk_limits is not None:
            if self._kill_switch is not None:
                self._risk_limits.set_flatten_only(
                    self._kill_switch.mode
                    in {SafeMode.FLATTEN_ONLY, SafeMode.POSITION_MISMATCH},
                )
            evaluation = self._risk_limits.evaluate(command)
            if not evaluation.allowed:
                if self._event_logger is not None:
                    self._event_logger.log(
                        "order_blocked_risk",
                        action=command.action,
                        symbol=command.symbol,
                        side=command.side,
                        order_id=command.order_id,
                        reason=evaluation.first_reject_reason(),
                    )
                return False
        return True

    def _reduces_risk(self, command: OrderCommand) -> bool:
        if command.action in {OrderIntentAction.CANCEL, OrderIntentAction.FLATTEN}:
            return True
        if command.action == OrderIntentAction.SUBMIT:
            net_lots = 0
            if self._risk_limits is not None:
                net_lots = self._risk_limits.get_net_lots(command.symbol)
            if net_lots > 0 and command.side == OrderSide.ASK:
                return True
            if net_lots < 0 and command.side == OrderSide.BID:
                return True
            return False
        symbol_ledger = self._order_ledger.by_symbol.get(command.symbol)
        if symbol_ledger is None:
            return False
        live_order = symbol_ledger.get_order(
            command.side,
            level_index=command.level_index,
        )
        if command.action == OrderIntentAction.REPLACE and live_order is not None:
            return command.size <= live_order.remaining_size
        return False

    def _submit(self, command: OrderCommand) -> None:
        if command.price is None:
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_submit_skipped",
                    symbol=command.symbol,
                    side=command.side,
                    reason="missing_price",
            )
            return
        if self._submit_would_duplicate_side_order(command):
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_submit_skipped",
                    symbol=command.symbol,
                    side=command.side,
                    reason="duplicate_side_order_guard",
                )
            return
        order = self._order_factory.build_limit_order(
            symbol=command.symbol,
            side=command.side,
            size=command.size,
            price=command.price,
        )
        try:
            self._trader.submit_order(order)
        except Exception as exc:
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_submit_failed",
                    symbol=command.symbol,
                    side=command.side,
                    price=command.price,
                    size=command.size,
                    reason=command.reason,
                    error=repr(exc),
                )
            return

        now_ns = time.monotonic_ns()
        live_order = WorkingOrder(
            symbol=command.symbol,
            side=command.side,
            order_id=order.id,
            price=command.price,
            size=command.size,
            level_index=command.level_index,
            liquidity=OrderLiquidity.LIMIT,
            submitted_ts_ns=now_ns,
            last_update_ts_ns=now_ns,
            status="pending_new",
        )
        self._order_ledger.register_live_order(live_order)
        audit = self._order_ledger.ensure_audit(
            order_id=order.id,
            symbol=command.symbol,
            side=command.side,
            level_index=command.level_index,
            submitted_ts_ns=now_ns,
            submit_price=command.price,
            submit_size=command.size,
            decision_price=command.decision_price,
            arrival_bid_px=command.arrival_bid_px,
            arrival_ask_px=command.arrival_ask_px,
            liquidity=OrderLiquidity.LIMIT,
        )
        audit.current_status = "pending_new"
        audit.last_update_ts_ns = now_ns
        if self._event_logger is not None:
            self._event_logger.log(
                "order_submitted",
                symbol=command.symbol,
                side=command.side,
                order_id=order.id,
                price=command.price,
                size=command.size,
                reason=command.reason,
            )

    def _cancel(self, command: OrderCommand) -> None:
        live_order = self._order_ledger.find_by_order_id(command.order_id)
        if live_order is None:
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_cancel_skipped",
                    symbol=command.symbol,
                    side=command.side,
                    order_id=command.order_id,
                    reason="order_not_found",
                )
            return
        if live_order.pending_cancel:
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_cancel_skipped",
                    symbol=live_order.symbol,
                    side=live_order.side,
                    order_id=live_order.order_id,
                    reason="cancel_already_pending",
                )
            return

        cancel_order = self._order_factory.build_cancel_order(live_order)
        try:
            self._trader.submit_cancellation(cancel_order)
        except Exception as exc:
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_cancel_failed",
                    symbol=live_order.symbol,
                    side=live_order.side,
                    order_id=live_order.order_id,
                    reason=command.reason,
                    error=repr(exc),
                )
            return
        live_order.pending_cancel = True
        now_ns = time.monotonic_ns()
        live_order.last_update_ts_ns = now_ns
        audit = self._order_ledger.ensure_audit(
            order_id=live_order.order_id,
            symbol=live_order.symbol,
            side=live_order.side,
            level_index=live_order.level_index,
        )
        audit.cancel_requested_ts_ns = now_ns
        audit.last_update_ts_ns = now_ns
        if self._event_logger is not None:
            self._event_logger.log(
                "order_cancel_requested",
                symbol=live_order.symbol,
                side=live_order.side,
                order_id=live_order.order_id,
                reason=command.reason,
            )

    def _replace(self, command: OrderCommand) -> None:
        live_order = self._order_ledger.find_by_order_id(command.order_id)
        if live_order is not None:
            live_order.last_replace_request_ts_ns = time.monotonic_ns()
            live_order.pending_replace_price = command.price
            live_order.pending_replace_size = command.size
            live_order.pending_replace_reason = command.reason
            audit = self._order_ledger.ensure_audit(
                order_id=live_order.order_id,
                symbol=live_order.symbol,
                side=live_order.side,
                level_index=live_order.level_index,
            )
            audit.replace_requested_ts_ns = live_order.last_replace_request_ts_ns
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_replace_requested",
                    symbol=live_order.symbol,
                    side=live_order.side,
                    order_id=live_order.order_id,
                    new_price=command.price,
                    new_size=command.size,
                    reason=command.reason,
                )
        self._cancel(
            OrderCommand(
                action=OrderIntentAction.CANCEL,
                symbol=command.symbol,
                side=command.side,
                level_index=command.level_index,
                order_id=command.order_id,
                reason=command.reason,
            )
        )

    def _flatten(self, command: OrderCommand) -> None:
        if command.size <= 0:
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_flatten_skipped",
                    symbol=command.symbol,
                    side=command.side,
                    reason="missing_size",
                )
            return

        order = self._order_factory.build_market_order(
            symbol=command.symbol,
            side=command.side,
            size=command.size,
        )
        try:
            self._trader.submit_order(order)
        except Exception as exc:
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_flatten_failed",
                    symbol=command.symbol,
                    side=command.side,
                    size=command.size,
                    reason=command.reason,
                    error=repr(exc),
                )
            return

        now_ns = time.monotonic_ns()
        live_order = WorkingOrder(
            symbol=command.symbol,
            side=command.side,
            order_id=order.id,
            price=0.0,
            size=command.size,
            level_index=command.level_index,
            liquidity=OrderLiquidity.MARKET,
            submitted_ts_ns=now_ns,
            last_update_ts_ns=now_ns,
            status="pending_flatten",
        )
        self._order_ledger.register_live_order(live_order)
        audit = self._order_ledger.ensure_audit(
            order_id=order.id,
            symbol=command.symbol,
            side=command.side,
            level_index=command.level_index,
            submitted_ts_ns=now_ns,
            submit_price=0.0,
            submit_size=command.size,
            decision_price=command.decision_price,
            arrival_bid_px=command.arrival_bid_px,
            arrival_ask_px=command.arrival_ask_px,
            liquidity=OrderLiquidity.MARKET,
        )
        audit.current_status = "pending_flatten"
        audit.last_update_ts_ns = now_ns
        if self._event_logger is not None:
            self._event_logger.log(
                "order_flatten_submitted",
                symbol=command.symbol,
                side=command.side,
                order_id=order.id,
                size=command.size,
                reason=command.reason,
            )

    def _submit_would_duplicate_side_order(self, command: OrderCommand) -> bool:
        symbol_ledger = self._order_ledger.by_symbol.get(command.symbol)
        if symbol_ledger is None:
            return False
        existing_order = symbol_ledger.get_order(
            command.side,
            level_index=command.level_index,
        )
        if existing_order is None:
            return False
        if existing_order.is_live:
            return True
        if existing_order.has_pending_replace:
            return False
        if (
            existing_order.status == "inactive"
            and existing_order.inactive_ts_ns > 0
            and existing_order.last_replace_request_ts_ns <= 0
            and time.monotonic_ns() - existing_order.inactive_ts_ns
            < self._RECENT_INACTIVE_DUPLICATE_GUARD_NS
        ):
            return True
        return False
