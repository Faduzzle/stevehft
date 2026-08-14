from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.execution.order_state import OrderSide, WorkingOrder


def _require_shift() -> Any:
    try:
        import shift  # type: ignore
    except ImportError as exc:
        raise RuntimeError("SHIFT Python package is not installed") from exc
    return shift


@dataclass(slots=True)
class ShiftPyOrderFactory:
    """Construct SHIFT order objects from internal execution intents."""

    def build_limit_order(self, symbol: str, side: OrderSide, size: int, price: float):
        if not symbol:
            raise ValueError("symbol is required")
        if size <= 0:
            raise ValueError("limit order size must be positive")
        if price <= 0:
            raise ValueError("limit order price must be positive")

        shift = _require_shift()
        order_type = (
            shift.Order.Type.LIMIT_BUY
            if side == OrderSide.BID
            else shift.Order.Type.LIMIT_SELL
        )
        return shift.Order(order_type, symbol, size, price)

    def build_market_order(self, symbol: str, side: OrderSide, size: int):
        if not symbol:
            raise ValueError("symbol is required")
        if size <= 0:
            raise ValueError("market order size must be positive")

        shift = _require_shift()
        order_type = (
            shift.Order.Type.MARKET_BUY
            if side == OrderSide.BID
            else shift.Order.Type.MARKET_SELL
        )
        return shift.Order(order_type, symbol, size)

    def build_cancel_order(self, live_order: WorkingOrder):
        if not live_order.order_id:
            raise ValueError("live order must have a broker order id")

        shift = _require_shift()
        order_type = (
            shift.Order.Type.CANCEL_BID
            if live_order.side == OrderSide.BID
            else shift.Order.Type.CANCEL_ASK
        )
        cancel_size = max(live_order.remaining_size, 1)
        return shift.Order(
            order_type,
            live_order.symbol,
            cancel_size,
            live_order.price,
            live_order.order_id,
        )
