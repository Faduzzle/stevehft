from __future__ import annotations

from dataclasses import dataclass

from src.data.state import SymbolState


@dataclass(slots=True)
class QuoteGateDecision:
    allow_quotes: bool
    reason: str
    age_ms: float
    spread_ticks: float


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0.0:
        raise ValueError("tick_size must be positive")
    return round(price / tick_size) * tick_size


def compute_quote_gate(
    state: SymbolState,
    *,
    now_ns: int,
    tick_size: float,
    max_staleness_ms: int,
    max_spread_ticks: int,
) -> QuoteGateDecision:
    age_ms = max((now_ns - state.last_book_update_ns) / 1_000_000, 0.0)
    spread_ticks = state.spread / tick_size if tick_size > 0.0 else 0.0
    if state.best_price.best_bid_px <= 0.0 or state.best_price.best_ask_px <= 0.0:
        return QuoteGateDecision(False, "missing_touch", age_ms, spread_ticks)
    if state.spread <= 0.0:
        return QuoteGateDecision(False, "locked_or_invalid_spread", age_ms, spread_ticks)
    if age_ms > max_staleness_ms:
        return QuoteGateDecision(False, "stale_book", age_ms, spread_ticks)
    if spread_ticks > max_spread_ticks:
        return QuoteGateDecision(False, "spread_too_wide", age_ms, spread_ticks)
    return QuoteGateDecision(True, "quoting_enabled", age_ms, spread_ticks)


def compute_inventory_skew(
    inventory_lots: int,
    *,
    tick_size: float,
    skew_ticks_per_lot: float,
    max_skew_ticks: float,
    inventory_limit_lots: int = 1,
) -> float:
    max_lots = max(abs(inventory_limit_lots), 1)
    signed_inventory = 1.0 if inventory_lots > 0 else -1.0 if inventory_lots < 0 else 0.0
    normalized_inventory = clamp(abs(inventory_lots) / max_lots, 0.0, 1.0)
    convex_inventory = signed_inventory * (normalized_inventory ** 2) * max_lots
    skew_ticks = clamp(
        convex_inventory * skew_ticks_per_lot,
        -max_skew_ticks,
        max_skew_ticks,
    )
    return skew_ticks * tick_size


def compute_fair_value(
    state: SymbolState,
    *,
    microprice_weight: float,
    imbalance_shift_ticks: float,
    tick_size: float,
    microprice_value: float | None = None,
    imbalance_value: float | None = None,
    drift_shift_ticks: float = 0.0,
) -> tuple[float, float]:
    mid = state.mid_price
    micro = (
        microprice_value
        if microprice_value is not None and microprice_value > 0.0
        else state.microprice
        if state.microprice > 0.0
        else mid
    )
    alpha_center = (1.0 - microprice_weight) * mid + microprice_weight * micro
    effective_imbalance = (
        imbalance_value if imbalance_value is not None else state.book_imbalance
    )
    imbalance_shift = effective_imbalance * imbalance_shift_ticks * tick_size
    drift_shift = drift_shift_ticks * tick_size
    fair_value = alpha_center + imbalance_shift + drift_shift
    return fair_value, imbalance_shift


def compute_half_width(
    state: SymbolState,
    *,
    tick_size: float,
    min_half_spread_ticks: int,
    max_half_spread_ticks: int,
    model_half_width_ticks: float | None = None,
) -> float:
    if model_half_width_ticks is not None and model_half_width_ticks > 0.0:
        model_half = model_half_width_ticks * tick_size
        return clamp(
            model_half,
            min_half_spread_ticks * tick_size,
            max_half_spread_ticks * tick_size,
        )
    observed_half = max(state.spread * 0.5, min_half_spread_ticks * tick_size)
    capped_half = min(observed_half, max_half_spread_ticks * tick_size)
    return capped_half
