from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Dict


@dataclass(slots=True)
class Position:
    symbol: str
    long_shares: int = 0
    short_shares: int = 0
    long_price: float = 0.0
    short_price: float = 0.0
    realized_pl: float = 0.0
    last_update_ts_ns: int = 0

    @property
    def net_shares(self) -> int:
        return self.long_shares - self.short_shares

    @property
    def net_lots(self) -> int:
        return int(self.net_shares / 100)

    @property
    def estimated_short_close_buying_power(self) -> float:
        if self.short_shares <= 0 or self.short_price <= 0.0:
            return 0.0
        return self.short_shares * self.short_price

    def estimated_forced_close_penalty(self, *, mark_price: float) -> float:
        if self.long_shares > 0:
            reference_price = self.long_price if self.long_price > 0.0 else mark_price
            return self.long_shares * reference_price * 0.01
        if self.short_shares > 0:
            reference_price = self.short_price if self.short_price > 0.0 else mark_price
            return self.short_shares * reference_price * 0.01
        return 0.0


@dataclass(slots=True)
class PortfolioSummaryState:
    total_bp: float = 0.0
    total_shares: int = 0
    total_realized_pl: float = 0.0
    last_update_ts_ns: int = 0


@dataclass(slots=True)
class PortfolioLedger:
    positions: Dict[str, Position] = field(default_factory=dict)
    summary: PortfolioSummaryState = field(default_factory=PortfolioSummaryState)

    def update_position(
        self,
        symbol: str,
        *,
        long_shares: int,
        short_shares: int,
        long_price: float,
        short_price: float,
        realized_pl: float,
        ts_ns: int,
    ) -> None:
        self.positions[symbol] = Position(
            symbol=symbol,
            long_shares=long_shares,
            short_shares=short_shares,
            long_price=long_price,
            short_price=short_price,
            realized_pl=realized_pl,
            last_update_ts_ns=ts_ns,
        )

    def clear_missing_positions(
        self,
        active_symbols: Iterable[str],
        *,
        ts_ns: int,
    ) -> list[Position]:
        active_symbol_set = set(active_symbols)
        cleared_positions: list[Position] = []
        for symbol, position in list(self.positions.items()):
            if symbol in active_symbol_set:
                continue
            if position.net_shares == 0 and abs(position.realized_pl) <= 1e-12:
                continue
            cleared = Position(
                symbol=symbol,
                long_shares=0,
                short_shares=0,
                long_price=0.0,
                short_price=0.0,
                realized_pl=position.realized_pl,
                last_update_ts_ns=ts_ns,
            )
            self.positions[symbol] = cleared
            cleared_positions.append(cleared)
        return cleared_positions

    def get_net_lots(self, symbol: str) -> int:
        position = self.positions.get(symbol)
        if position is None:
            return 0
        return position.net_lots

    def estimate_forced_close_penalty(self, symbol: str, *, mark_price: float) -> float:
        position = self.positions.get(symbol)
        if position is None:
            return 0.0
        return position.estimated_forced_close_penalty(mark_price=mark_price)

    @property
    def estimated_short_close_buying_power(self) -> float:
        return sum(
            position.estimated_short_close_buying_power
            for position in self.positions.values()
        )
