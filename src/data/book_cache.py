from __future__ import annotations

import time
from typing import Iterable, List, Protocol

from src.execution.order_state import OrderLedger, OrderSide
from .state import BestPriceSnapshot, BookLevel, MarketState, SymbolState
from src.telemetry.logger import EventLoggerLike


class BestPriceLike(Protocol):
    def get_bid_price(self) -> float: ...

    def get_bid_size(self) -> int: ...

    def get_ask_price(self) -> float: ...

    def get_ask_size(self) -> int: ...

    def get_global_bid_price(self) -> float: ...

    def get_global_bid_size(self) -> int: ...

    def get_global_ask_price(self) -> float: ...

    def get_global_ask_size(self) -> int: ...

    def get_local_bid_price(self) -> float: ...

    def get_local_bid_size(self) -> int: ...

    def get_local_ask_price(self) -> float: ...

    def get_local_ask_size(self) -> int: ...


class OrderBookEntryLike(Protocol):
    price: float
    size: int
    destination: str
    time: object


class TraderMarketDataLike(Protocol):
    def get_best_price(self, symbol: str) -> BestPriceLike: ...

    def get_order_book(self, symbol: str, book_type, max_level: int = 99) -> list[OrderBookEntryLike]: ...

    def get_last_price(self, symbol: str) -> float: ...

    def get_last_size(self, symbol: str) -> int: ...


class OrderBookTypeLike(Protocol):
    GLOBAL_BID: object
    GLOBAL_ASK: object
    LOCAL_BID: object
    LOCAL_ASK: object


class BookCache:
    def __init__(
        self,
        market_state: MarketState,
        *,
        order_book_type: OrderBookTypeLike | None = None,
        order_ledger: OrderLedger | None = None,
        event_logger: EventLoggerLike | None = None,
    ) -> None:
        self._market_state = market_state
        self._order_book_type = order_book_type
        self._order_ledger = order_ledger
        self._event_logger = event_logger

    @property
    def market_state(self) -> MarketState:
        return self._market_state

    def refresh_symbol(
        self,
        trader: TraderMarketDataLike,
        symbol: str,
        *,
        max_depth: int = 5,
        now_ns: int | None = None,
    ) -> SymbolState:
        ts_ns = now_ns or time.monotonic_ns()
        state = self._market_state.ensure_symbol(symbol)
        previous_best_price = state.best_price
        previous_local_bids = state.local_bids
        previous_local_asks = state.local_asks
        state.best_price = self._normalize_best_price(symbol, trader.get_best_price(symbol), ts_ns)
        state.last_book_update_ns = ts_ns

        if self._order_book_type is not None:
            state.global_bids = self._normalize_book_side(
                trader.get_order_book(symbol, self._order_book_type.GLOBAL_BID, max_depth)
            )
            state.global_asks = self._normalize_book_side(
                trader.get_order_book(symbol, self._order_book_type.GLOBAL_ASK, max_depth)
            )
            state.local_bids = self._subtract_own_local_orders(
                symbol,
                OrderSide.BID,
                self._normalize_book_side(
                    trader.get_order_book(symbol, self._order_book_type.LOCAL_BID, max_depth)
                ),
            )
            state.local_asks = self._subtract_own_local_orders(
                symbol,
                OrderSide.ASK,
                self._normalize_book_side(
                    trader.get_order_book(symbol, self._order_book_type.LOCAL_ASK, max_depth)
                ),
            )
            if state.local_bids:
                state.best_price.local_bid_px = state.local_bids[0].price
                state.best_price.local_bid_sz = state.local_bids[0].size
            else:
                state.best_price.local_bid_px = 0.0
                state.best_price.local_bid_sz = 0
            if state.local_asks:
                state.best_price.local_ask_px = state.local_asks[0].price
                state.best_price.local_ask_sz = state.local_asks[0].size
            else:
                state.best_price.local_ask_px = 0.0
                state.best_price.local_ask_sz = 0

        state.global_l1_voi = self._compute_global_l1_voi(
            previous_best_price,
            state.best_price,
        )
        state.local_multi_level_voi = self._compute_local_multi_level_voi(
            previous_local_bids,
            previous_local_asks,
            state.local_bids,
            state.local_asks,
            max_depth=max_depth,
        )
        try:
            state.last_trade_price = float(trader.get_last_price(symbol) or 0.0)
            state.last_trade_size = int(trader.get_last_size(symbol) or 0)
            state.last_trade_update_ns = ts_ns
        except AttributeError:
            state.last_trade_price = 0.0
            state.last_trade_size = 0
            state.last_trade_update_ns = 0

        if self._event_logger is not None and (
            previous_best_price.best_bid_px != state.best_price.best_bid_px
            or previous_best_price.best_ask_px != state.best_price.best_ask_px
            or previous_best_price.best_bid_sz != state.best_price.best_bid_sz
            or previous_best_price.best_ask_sz != state.best_price.best_ask_sz
        ):
            self._event_logger.log(
                "book_cache_update",
                symbol=symbol,
                best_bid_px=state.best_price.best_bid_px,
                best_ask_px=state.best_price.best_ask_px,
                best_bid_sz=state.best_price.best_bid_sz,
                best_ask_sz=state.best_price.best_ask_sz,
                spread=round(state.spread, 4),
                microprice=round(state.microprice, 4),
                book_imbalance=round(state.book_imbalance, 4),
                global_l1_voi=round(state.global_l1_voi, 4),
                local_multi_level_voi=round(state.local_multi_level_voi, 4),
                last_trade_price=round(state.last_trade_price, 4),
                last_trade_size=state.last_trade_size,
            )
        return state

    @staticmethod
    def _normalize_best_price(symbol: str, best_price: BestPriceLike, ts_ns: int) -> BestPriceSnapshot:
        return BestPriceSnapshot(
            symbol=symbol,
            best_bid_px=float(best_price.get_bid_price() or 0.0),
            best_bid_sz=int(best_price.get_bid_size() or 0),
            best_ask_px=float(best_price.get_ask_price() or 0.0),
            best_ask_sz=int(best_price.get_ask_size() or 0),
            global_bid_px=float(best_price.get_global_bid_price() or 0.0),
            global_bid_sz=int(best_price.get_global_bid_size() or 0),
            global_ask_px=float(best_price.get_global_ask_price() or 0.0),
            global_ask_sz=int(best_price.get_global_ask_size() or 0),
            local_bid_px=float(best_price.get_local_bid_price() or 0.0),
            local_bid_sz=int(best_price.get_local_bid_size() or 0),
            local_ask_px=float(best_price.get_local_ask_price() or 0.0),
            local_ask_sz=int(best_price.get_local_ask_size() or 0),
            update_ts_ns=ts_ns,
        )

    @staticmethod
    def _normalize_book_side(entries: Iterable[OrderBookEntryLike]) -> List[BookLevel]:
        levels: List[BookLevel] = []
        for entry in entries:
            levels.append(
                BookLevel(
                    price=float(entry.price or 0.0),
                    size=int(entry.size or 0),
                    destination=str(getattr(entry, "destination", "") or ""),
                    book_time=getattr(entry, "time", None),
                )
            )
        return levels

    def _subtract_own_local_orders(
        self,
        symbol: str,
        side: OrderSide,
        levels: List[BookLevel],
    ) -> List[BookLevel]:
        if self._order_ledger is None or not levels:
            return levels

        own_size_by_price: dict[int, int] = {}
        for order in self._order_ledger.live_orders(symbol=symbol, side=side):
            if (
                order.status in {"pending_new", "pending_flatten"}
                or order.price <= 0.0
                or order.remaining_size <= 0
            ):
                continue
            price_key = self._price_key(order.price)
            own_size_by_price[price_key] = (
                own_size_by_price.get(price_key, 0) + order.remaining_size
            )
        if not own_size_by_price:
            return levels

        cleaned_levels: List[BookLevel] = []
        for level in levels:
            level_size = max(level.size, 0)
            price_key = self._price_key(level.price)
            remaining_to_remove = own_size_by_price.get(price_key, 0)
            if remaining_to_remove > 0:
                level_size = max(level_size - remaining_to_remove, 0)
                own_size_by_price[price_key] = max(remaining_to_remove - level.size, 0)
            if level_size > 0:
                cleaned_levels.append(
                    BookLevel(
                        price=level.price,
                        size=level_size,
                        destination=level.destination,
                        book_time=level.book_time,
                    )
                )
        return cleaned_levels

    @staticmethod
    def _price_key(price: float) -> int:
        return int(round(price * 1_000_000_000))

    @staticmethod
    def _compute_global_l1_voi(
        previous: BestPriceSnapshot,
        current: BestPriceSnapshot,
    ) -> float:
        bid_flow = BookCache._l1_side_flow(
            previous_px=previous.global_bid_px,
            previous_size=previous.global_bid_sz,
            current_px=current.global_bid_px,
            current_size=current.global_bid_sz,
            side=OrderSide.BID,
        )
        ask_flow = BookCache._l1_side_flow(
            previous_px=previous.global_ask_px,
            previous_size=previous.global_ask_sz,
            current_px=current.global_ask_px,
            current_size=current.global_ask_sz,
            side=OrderSide.ASK,
        )
        depth_scale = max(
            current.global_bid_sz
            + current.global_ask_sz
            + previous.global_bid_sz
            + previous.global_ask_sz,
            1,
        )
        return max(-1.0, min((bid_flow - ask_flow) / depth_scale, 1.0))

    @staticmethod
    def _compute_local_multi_level_voi(
        previous_bids: List[BookLevel],
        previous_asks: List[BookLevel],
        current_bids: List[BookLevel],
        current_asks: List[BookLevel],
        *,
        max_depth: int,
    ) -> float:
        depth = max(max_depth, 1)
        bid_flow = BookCache._multi_level_side_flow(
            previous_bids[:depth],
            current_bids[:depth],
            side=OrderSide.BID,
        )
        ask_flow = BookCache._multi_level_side_flow(
            previous_asks[:depth],
            current_asks[:depth],
            side=OrderSide.ASK,
        )
        depth_scale = max(
            sum(max(level.size, 0) for level in previous_bids[:depth])
            + sum(max(level.size, 0) for level in previous_asks[:depth])
            + sum(max(level.size, 0) for level in current_bids[:depth])
            + sum(max(level.size, 0) for level in current_asks[:depth]),
            1,
        )
        return max(-1.0, min((bid_flow - ask_flow) / depth_scale, 1.0))

    @staticmethod
    def _l1_side_flow(
        *,
        previous_px: float,
        previous_size: int,
        current_px: float,
        current_size: int,
        side: OrderSide,
    ) -> float:
        if previous_px <= 0.0 and current_px <= 0.0:
            return 0.0
        if side == OrderSide.BID:
            if current_px > previous_px + 1e-12:
                return float(max(current_size, 0))
            if abs(current_px - previous_px) <= 1e-12:
                return float(max(current_size, 0) - max(previous_size, 0))
            return float(-max(previous_size, 0))
        if current_px < previous_px - 1e-12 or previous_px <= 0.0 < current_px:
            return float(max(current_size, 0))
        if abs(current_px - previous_px) <= 1e-12:
            return float(max(current_size, 0) - max(previous_size, 0))
        return float(-max(previous_size, 0))

    @staticmethod
    def _multi_level_side_flow(
        previous_levels: List[BookLevel],
        current_levels: List[BookLevel],
        *,
        side: OrderSide,
    ) -> float:
        previous_best_px = previous_levels[0].price if previous_levels else 0.0
        previous_best_size = previous_levels[0].size if previous_levels else 0
        current_best_px = current_levels[0].price if current_levels else 0.0
        current_best_size = current_levels[0].size if current_levels else 0
        flow = BookCache._l1_side_flow(
            previous_px=previous_best_px,
            previous_size=previous_best_size,
            current_px=current_best_px,
            current_size=current_best_size,
            side=side,
        )

        depth = max(len(previous_levels), len(current_levels))
        for level_idx in range(1, depth):
            previous_level = (
                previous_levels[level_idx]
                if level_idx < len(previous_levels)
                else BookLevel(price=0.0, size=0)
            )
            current_level = (
                current_levels[level_idx]
                if level_idx < len(current_levels)
                else BookLevel(price=0.0, size=0)
            )
            flow += BookCache._l1_side_flow(
                previous_px=previous_level.price,
                previous_size=previous_level.size,
                current_px=current_level.price,
                current_size=current_level.size,
                side=side,
            )
        return flow
