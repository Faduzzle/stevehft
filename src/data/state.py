from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List


@dataclass(slots=True)
class BookLevel:
    price: float
    size: int
    destination: str = ""
    book_time: datetime | None = None

    def clone(self) -> BookLevel:
        return BookLevel(
            price=self.price,
            size=self.size,
            destination=self.destination,
            book_time=self.book_time,
        )


@dataclass(slots=True)
class BestPriceSnapshot:
    symbol: str
    best_bid_px: float = 0.0
    best_bid_sz: int = 0
    best_ask_px: float = 0.0
    best_ask_sz: int = 0
    global_bid_px: float = 0.0
    global_bid_sz: int = 0
    global_ask_px: float = 0.0
    global_ask_sz: int = 0
    local_bid_px: float = 0.0
    local_bid_sz: int = 0
    local_ask_px: float = 0.0
    local_ask_sz: int = 0
    update_ts_ns: int = 0

    def clone(self) -> BestPriceSnapshot:
        return BestPriceSnapshot(
            symbol=self.symbol,
            best_bid_px=self.best_bid_px,
            best_bid_sz=self.best_bid_sz,
            best_ask_px=self.best_ask_px,
            best_ask_sz=self.best_ask_sz,
            global_bid_px=self.global_bid_px,
            global_bid_sz=self.global_bid_sz,
            global_ask_px=self.global_ask_px,
            global_ask_sz=self.global_ask_sz,
            local_bid_px=self.local_bid_px,
            local_bid_sz=self.local_bid_sz,
            local_ask_px=self.local_ask_px,
            local_ask_sz=self.local_ask_sz,
            update_ts_ns=self.update_ts_ns,
        )

    @property
    def mid_price(self) -> float:
        if self.best_bid_px > 0.0 and self.best_ask_px > 0.0:
            return 0.5 * (self.best_bid_px + self.best_ask_px)
        return 0.0

    @property
    def spread(self) -> float:
        if self.best_bid_px > 0.0 and self.best_ask_px > 0.0:
            return max(self.best_ask_px - self.best_bid_px, 0.0)
        return 0.0

    @property
    def microprice(self) -> float:
        total = self.best_bid_sz + self.best_ask_sz
        if total <= 0:
            return self.mid_price
        return (
            self.best_ask_px * self.best_bid_sz + self.best_bid_px * self.best_ask_sz
        ) / total

    @property
    def book_imbalance(self) -> float:
        total = self.best_bid_sz + self.best_ask_sz
        if total <= 0:
            return 0.0
        return (self.best_bid_sz - self.best_ask_sz) / total


@dataclass(slots=True)
class SymbolState:
    symbol: str
    best_price: BestPriceSnapshot
    global_bids: List[BookLevel] = field(default_factory=list)
    global_asks: List[BookLevel] = field(default_factory=list)
    local_bids: List[BookLevel] = field(default_factory=list)
    local_asks: List[BookLevel] = field(default_factory=list)
    global_l1_voi: float = 0.0
    local_multi_level_voi: float = 0.0
    last_trade_price: float = 0.0
    last_trade_size: int = 0
    last_trade_update_ns: int = 0
    last_book_update_ns: int = 0

    def clone(self) -> SymbolState:
        return SymbolState(
            symbol=self.symbol,
            best_price=self.best_price.clone(),
            global_bids=[level.clone() for level in self.global_bids],
            global_asks=[level.clone() for level in self.global_asks],
            local_bids=[level.clone() for level in self.local_bids],
            local_asks=[level.clone() for level in self.local_asks],
            global_l1_voi=self.global_l1_voi,
            local_multi_level_voi=self.local_multi_level_voi,
            last_trade_price=self.last_trade_price,
            last_trade_size=self.last_trade_size,
            last_trade_update_ns=self.last_trade_update_ns,
            last_book_update_ns=self.last_book_update_ns,
        )

    @property
    def mid_price(self) -> float:
        return self.best_price.mid_price

    @property
    def microprice(self) -> float:
        return self.weighted_microprice(levels=3, local=True)

    @property
    def spread(self) -> float:
        return self.best_price.spread

    @property
    def book_imbalance(self) -> float:
        return self.combined_book_imbalance(levels=3)

    @property
    def local_microprice(self) -> float:
        return self.weighted_microprice(levels=3, local=True)

    @property
    def global_microprice(self) -> float:
        return self.weighted_microprice(levels=3, local=False)

    @property
    def local_depth_imbalance(self) -> float:
        return self.depth_imbalance(levels=3, local=True)

    @property
    def global_l1_imbalance(self) -> float:
        total = self.best_price.global_bid_sz + self.best_price.global_ask_sz
        if total <= 0:
            return self.best_price.book_imbalance
        return (
            self.best_price.global_bid_sz - self.best_price.global_ask_sz
        ) / total

    @property
    def global_l1_mid(self) -> float:
        if self.best_price.global_bid_px > 0.0 and self.best_price.global_ask_px > 0.0:
            return 0.5 * (
                self.best_price.global_bid_px + self.best_price.global_ask_px
            )
        return self.mid_price

    @property
    def trade_signed_volume(self) -> float:
        if self.last_trade_size <= 0 or self.last_trade_price <= 0.0:
            return 0.0
        midpoint = self.mid_price
        if midpoint <= 0.0:
            return 0.0
        if self.last_trade_price > midpoint + 1e-12:
            return float(self.last_trade_size)
        if self.last_trade_price < midpoint - 1e-12:
            return -float(self.last_trade_size)
        micro = self.local_microprice
        if micro > midpoint + 1e-12:
            return float(self.last_trade_size)
        if micro < midpoint - 1e-12:
            return -float(self.last_trade_size)
        return 0.0

    def combined_book_imbalance(self, levels: int = 3) -> float:
        global_l1_imbalance = self.global_l1_imbalance
        local_depth_imbalance = self.depth_imbalance(levels=levels, local=True)
        return 0.6 * global_l1_imbalance + 0.4 * local_depth_imbalance

    def weighted_microprice(self, levels: int = 3, *, local: bool = False) -> float:
        bids = self._book_side("bid", local=local)[: max(levels, 0)]
        asks = self._book_side("ask", local=local)[: max(levels, 0)]
        bid_depth = sum(max(level.size, 0) for level in bids)
        ask_depth = sum(max(level.size, 0) for level in asks)
        if bid_depth <= 0 and ask_depth <= 0:
            return self._fallback_microprice(local=local)

        bid_vwap = (
            sum(max(level.size, 0) * level.price for level in bids) / bid_depth
            if bid_depth > 0
            else self._fallback_book_px("bid", local=local)
        )
        ask_vwap = (
            sum(max(level.size, 0) * level.price for level in asks) / ask_depth
            if ask_depth > 0
            else self._fallback_book_px("ask", local=local)
        )
        total_depth = bid_depth + ask_depth
        if total_depth <= 0:
            return self._fallback_microprice(local=local)
        return (ask_vwap * bid_depth + bid_vwap * ask_depth) / total_depth

    def cumulative_bid_depth(self, levels: int = 3, *, local: bool = False) -> int:
        book = self.local_bids if local else self.global_bids
        depth = sum(max(level.size, 0) for level in book[: max(levels, 0)])
        if depth > 0:
            return depth
        return max(self._fallback_book_sz("bid", local=local), 0)

    def cumulative_ask_depth(self, levels: int = 3, *, local: bool = False) -> int:
        book = self.local_asks if local else self.global_asks
        depth = sum(max(level.size, 0) for level in book[: max(levels, 0)])
        if depth > 0:
            return depth
        return max(self._fallback_book_sz("ask", local=local), 0)

    def depth_imbalance(self, levels: int = 3, *, local: bool = False) -> float:
        bid_depth = self.cumulative_bid_depth(levels, local=local)
        ask_depth = self.cumulative_ask_depth(levels, local=local)
        total = bid_depth + ask_depth
        if total <= 0:
            return self._fallback_book_imbalance(local=local)
        return (bid_depth - ask_depth) / total

    def one_sided_book_volume(
        self,
        side: str,
        *,
        levels: int | None = None,
        local: bool = False,
    ) -> int:
        book = self._book_side(side, local=local)
        if levels is not None:
            book = book[: max(levels, 0)]
        volume = sum(max(level.size, 0) for level in book)
        if volume > 0:
            return volume
        return max(
            self._fallback_book_sz(side, local=local),
            0,
        )

    def levels_to_cover_side_volume_fraction(
        self,
        side: str,
        fraction: float = 0.25,
        *,
        local: bool = False,
    ) -> int:
        if fraction <= 0.0:
            return 0
        book = self._book_side(side, local=local)
        total_volume = self.one_sided_book_volume(side, local=local)
        if total_volume <= 0:
            return 0
        if not book:
            return 1

        target_volume = fraction * total_volume
        cumulative = 0.0
        for index, level in enumerate(book, start=1):
            cumulative += max(level.size, 0)
            if cumulative >= target_volume:
                return index
        return len(book)

    def queue_ahead_lots(
        self,
        side: str,
        price: float,
        *,
        local: bool = True,
    ) -> int:
        if price <= 0.0:
            return 0

        side_name = side.lower()
        queue_ahead = 0
        for level in self._book_side(side_name, local=local):
            level_size = max(level.size, 0)
            if side_name == "bid":
                if level.price + 1e-12 < price:
                    break
                queue_ahead += level_size
            elif side_name == "ask":
                if level.price - 1e-12 > price:
                    break
                queue_ahead += level_size
            else:
                raise ValueError(f"unknown book side: {side}")
        return queue_ahead

    def queue_share(
        self,
        side: str,
        price: float,
        own_size: int,
        *,
        local: bool = True,
    ) -> float:
        if own_size <= 0:
            return 0.0
        queue_ahead = self.queue_ahead_lots(side, price, local=local)
        return own_size / max(queue_ahead + own_size, 1)

    def depth_entropy(self, levels: int = 3, *, local: bool = True) -> float:
        return 0.5 * (
            self._side_depth_entropy("bid", levels=levels, local=local)
            + self._side_depth_entropy("ask", levels=levels, local=local)
        )

    def _side_depth_entropy(
        self,
        side: str,
        *,
        levels: int,
        local: bool,
    ) -> float:
        depth_values = [
            float(max(level.size, 0))
            for level in self._book_side(side, local=local)[: max(levels, 0)]
            if level.size > 0
        ]
        if len(depth_values) <= 1:
            return 0.0

        total_depth = sum(depth_values)
        if total_depth <= 0.0:
            return 0.0

        entropy = 0.0
        for value in depth_values:
            probability = value / total_depth
            entropy -= probability * math.log(probability)
        return entropy / max(math.log(len(depth_values)), 1e-12)

    def _book_side(self, side: str, *, local: bool = False) -> List[BookLevel]:
        side_name = side.lower()
        if side_name == "bid":
            return self.local_bids if local else self.global_bids
        if side_name == "ask":
            return self.local_asks if local else self.global_asks
        raise ValueError(f"unknown book side: {side}")

    def _fallback_book_px(self, side: str, *, local: bool = False) -> float:
        side_name = side.lower()
        if side_name == "bid":
            if local and self.best_price.local_bid_px > 0.0:
                return self.best_price.local_bid_px
            return self.best_price.best_bid_px
        if side_name == "ask":
            if local and self.best_price.local_ask_px > 0.0:
                return self.best_price.local_ask_px
            return self.best_price.best_ask_px
        raise ValueError(f"unknown book side: {side}")

    def _fallback_book_sz(self, side: str, *, local: bool = False) -> int:
        side_name = side.lower()
        if side_name == "bid":
            if local and self.best_price.local_bid_sz > 0:
                return self.best_price.local_bid_sz
            return self.best_price.best_bid_sz
        if side_name == "ask":
            if local and self.best_price.local_ask_sz > 0:
                return self.best_price.local_ask_sz
            return self.best_price.best_ask_sz
        raise ValueError(f"unknown book side: {side}")

    def _fallback_microprice(self, *, local: bool = False) -> float:
        if not local:
            return self.best_price.microprice
        total = self.best_price.local_bid_sz + self.best_price.local_ask_sz
        if total <= 0:
            if self.best_price.local_bid_px > 0.0 and self.best_price.local_ask_px > 0.0:
                return 0.5 * (
                    self.best_price.local_bid_px + self.best_price.local_ask_px
                )
            return self.best_price.microprice
        return (
            self.best_price.local_ask_px * self.best_price.local_bid_sz
            + self.best_price.local_bid_px * self.best_price.local_ask_sz
        ) / total

    def _fallback_book_imbalance(self, *, local: bool = False) -> float:
        if not local:
            return self.best_price.book_imbalance
        total = self.best_price.local_bid_sz + self.best_price.local_ask_sz
        if total <= 0:
            return self.best_price.book_imbalance
        return (self.best_price.local_bid_sz - self.best_price.local_ask_sz) / total


@dataclass(slots=True)
class MarketState:
    by_symbol: Dict[str, SymbolState] = field(default_factory=dict)

    def clone(self) -> MarketState:
        return MarketState(
            by_symbol={
                symbol: state.clone()
                for symbol, state in self.by_symbol.items()
            }
        )

    def ensure_symbol(self, symbol: str) -> SymbolState:
        state = self.by_symbol.get(symbol)
        if state is None:
            state = SymbolState(symbol=symbol, best_price=BestPriceSnapshot(symbol=symbol))
            self.by_symbol[symbol] = state
        return state
