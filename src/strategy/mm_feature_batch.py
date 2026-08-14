from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Sequence

from src.data.state import MarketState, SymbolState
from src.risk.inventory import PortfolioLedger
from src.strategy.signals import QuoteGateDecision

try:
    import numpy as _np
except Exception:
    _np = None


NumericBatch = Sequence[float] | Sequence[int]


@dataclass(frozen=True, slots=True)
class MarketMakingFeatureRow:
    symbol: str
    state: SymbolState
    inventory_lots: int
    book_age_ms: float
    spread_ticks: float
    mid_price: float
    local_microprice: float
    local_depth_imbalance: float
    depth_entropy: float
    local_multi_level_voi: float
    global_l1_imbalance: float
    global_l1_voi: float
    has_valid_touch: bool


@dataclass(frozen=True, slots=True)
class MarketMakingFeatureBatch:
    rows: tuple[MarketMakingFeatureRow, ...]
    symbols: tuple[str, ...]
    inventory_lots: NumericBatch
    book_age_ms: NumericBatch
    spread_ticks: NumericBatch
    mid_prices: NumericBatch
    local_microprices: NumericBatch
    local_depth_imbalances: NumericBatch
    local_multi_level_vois: NumericBatch
    global_l1_imbalances: NumericBatch
    global_l1_vois: NumericBatch
    has_valid_touch: Sequence[bool]
    numpy_backend: bool = False

    def compute_quote_gates(
        self,
        *,
        max_staleness_ms: Sequence[int],
        max_spread_ticks: Sequence[int],
    ) -> tuple[QuoteGateDecision, ...]:
        if len(max_staleness_ms) != len(self.rows):
            raise ValueError("max_staleness_ms must match feature batch length")
        if len(max_spread_ticks) != len(self.rows):
            raise ValueError("max_spread_ticks must match feature batch length")

        if self.numpy_backend and _np is not None:
            return self._compute_quote_gates_numpy(
                max_staleness_ms=max_staleness_ms,
                max_spread_ticks=max_spread_ticks,
            )
        return self._compute_quote_gates_scalar(
            max_staleness_ms=max_staleness_ms,
            max_spread_ticks=max_spread_ticks,
        )

    def _compute_quote_gates_scalar(
        self,
        *,
        max_staleness_ms: Sequence[int],
        max_spread_ticks: Sequence[int],
    ) -> tuple[QuoteGateDecision, ...]:
        decisions: list[QuoteGateDecision] = []
        for row, stale_limit_ms, spread_limit_ticks in zip(
            self.rows,
            max_staleness_ms,
            max_spread_ticks,
        ):
            decisions.append(
                _row_quote_gate_decision(
                    row,
                    max_staleness_ms=int(stale_limit_ms),
                    max_spread_ticks=int(spread_limit_ticks),
                )
            )
        return tuple(decisions)

    def _compute_quote_gates_numpy(
        self,
        *,
        max_staleness_ms: Sequence[int],
        max_spread_ticks: Sequence[int],
    ) -> tuple[QuoteGateDecision, ...]:
        stale_limits = _np.asarray(max_staleness_ms, dtype=_np.float64)
        spread_limits = _np.asarray(max_spread_ticks, dtype=_np.float64)
        book_ages = _np.asarray(self.book_age_ms, dtype=_np.float64)
        spreads = _np.asarray(self.spread_ticks, dtype=_np.float64)
        valid_touch = _np.asarray(self.has_valid_touch, dtype=_np.bool_)

        stale_mask = book_ages > stale_limits
        wide_mask = spreads > spread_limits
        allow_mask = valid_touch & ~stale_mask & ~wide_mask

        decisions: list[QuoteGateDecision] = []
        for idx, row in enumerate(self.rows):
            if allow_mask[idx]:
                decisions.append(
                    QuoteGateDecision(
                        True,
                        "quoting_enabled",
                        row.book_age_ms,
                        row.spread_ticks,
                    )
                )
            elif not valid_touch[idx]:
                decisions.append(
                    QuoteGateDecision(
                        False,
                        "missing_touch" if row.mid_price <= 0.0 else "locked_or_invalid_spread",
                        row.book_age_ms,
                        row.spread_ticks,
                    )
                )
            elif stale_mask[idx]:
                decisions.append(
                    QuoteGateDecision(
                        False,
                        "stale_book",
                        row.book_age_ms,
                        row.spread_ticks,
                    )
                )
            else:
                decisions.append(
                    QuoteGateDecision(
                        False,
                        "spread_too_wide",
                        row.book_age_ms,
                        row.spread_ticks,
                    )
                )
        return tuple(decisions)


def build_market_making_feature_batch(
    market_state: MarketState,
    portfolio_ledger: PortfolioLedger,
    *,
    now_ns: int | None = None,
    tick_size: float,
) -> MarketMakingFeatureBatch:
    if tick_size <= 0.0:
        raise ValueError("tick_size must be positive")

    snapshot_ns = now_ns if now_ns is not None else time.monotonic_ns()
    symbols = tuple(market_state.by_symbol.keys())
    states = tuple(market_state.by_symbol[symbol] for symbol in symbols)
    inventory_values = tuple(
        portfolio_ledger.get_net_lots(symbol) for symbol in symbols
    )
    book_age_values = tuple(
        max((snapshot_ns - state.last_book_update_ns) / 1_000_000, 0.0)
        for state in states
    )
    spread_tick_values = tuple(state.spread / tick_size for state in states)
    mid_values = tuple(state.mid_price for state in states)
    local_micro_values = tuple(state.local_microprice for state in states)
    local_imbalance_values = tuple(
        state.local_depth_imbalance for state in states
    )
    depth_entropy_values = tuple(
        state.depth_entropy(levels=3, local=True) for state in states
    )
    local_voi_values = tuple(state.local_multi_level_voi for state in states)
    global_imbalance_values = tuple(state.global_l1_imbalance for state in states)
    global_voi_values = tuple(state.global_l1_voi for state in states)
    has_touch_values = tuple(
        state.best_price.best_bid_px > 0.0
        and state.best_price.best_ask_px > 0.0
        and state.spread > 0.0
        for state in states
    )
    rows = tuple(
        MarketMakingFeatureRow(
            symbol=symbol,
            state=state,
            inventory_lots=inventory_lots,
            book_age_ms=book_age_ms,
            spread_ticks=spread_ticks,
            mid_price=mid_price,
            local_microprice=local_microprice,
            local_depth_imbalance=local_depth_imbalance,
            depth_entropy=depth_entropy,
            local_multi_level_voi=local_multi_level_voi,
            global_l1_imbalance=global_l1_imbalance,
            global_l1_voi=global_l1_voi,
            has_valid_touch=has_valid_touch,
        )
        for (
            symbol,
            state,
            inventory_lots,
            book_age_ms,
            spread_ticks,
            mid_price,
            local_microprice,
            local_depth_imbalance,
            depth_entropy,
            local_multi_level_voi,
            global_l1_imbalance,
            global_l1_voi,
            has_valid_touch,
        ) in zip(
            symbols,
            states,
            inventory_values,
            book_age_values,
            spread_tick_values,
            mid_values,
            local_micro_values,
            local_imbalance_values,
            depth_entropy_values,
            local_voi_values,
            global_imbalance_values,
            global_voi_values,
            has_touch_values,
        )
    )

    if _np is None:
        return MarketMakingFeatureBatch(
            rows=rows,
            symbols=symbols,
            inventory_lots=inventory_values,
            book_age_ms=book_age_values,
            spread_ticks=spread_tick_values,
            mid_prices=mid_values,
            local_microprices=local_micro_values,
            local_depth_imbalances=local_imbalance_values,
            local_multi_level_vois=local_voi_values,
            global_l1_imbalances=global_imbalance_values,
            global_l1_vois=global_voi_values,
            has_valid_touch=has_touch_values,
            numpy_backend=False,
        )

    return MarketMakingFeatureBatch(
        rows=rows,
        symbols=symbols,
        inventory_lots=_np.asarray(inventory_values, dtype=_np.int64),
        book_age_ms=_np.asarray(book_age_values, dtype=_np.float64),
        spread_ticks=_np.asarray(spread_tick_values, dtype=_np.float64),
        mid_prices=_np.asarray(mid_values, dtype=_np.float64),
        local_microprices=_np.asarray(local_micro_values, dtype=_np.float64),
        local_depth_imbalances=_np.asarray(local_imbalance_values, dtype=_np.float64),
        local_multi_level_vois=_np.asarray(local_voi_values, dtype=_np.float64),
        global_l1_imbalances=_np.asarray(global_imbalance_values, dtype=_np.float64),
        global_l1_vois=_np.asarray(global_voi_values, dtype=_np.float64),
        has_valid_touch=_np.asarray(has_touch_values, dtype=_np.bool_),
        numpy_backend=True,
    )


def _row_quote_gate_decision(
    row: MarketMakingFeatureRow,
    *,
    max_staleness_ms: int,
    max_spread_ticks: int,
) -> QuoteGateDecision:
    if not row.has_valid_touch:
        reason = "missing_touch" if row.mid_price <= 0.0 else "locked_or_invalid_spread"
        return QuoteGateDecision(
            False,
            reason,
            row.book_age_ms,
            row.spread_ticks,
        )
    if row.book_age_ms > max_staleness_ms:
        return QuoteGateDecision(
            False,
            "stale_book",
            row.book_age_ms,
            row.spread_ticks,
        )
    if row.spread_ticks > max_spread_ticks:
        return QuoteGateDecision(
            False,
            "spread_too_wide",
            row.book_age_ms,
            row.spread_ticks,
        )
    return QuoteGateDecision(
        True,
        "quoting_enabled",
        row.book_age_ms,
        row.spread_ticks,
    )
