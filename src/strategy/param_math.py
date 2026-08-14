"""Small pure numeric helpers shared by the adaptive parameter pipeline.

Kept dependency-free (no imports from params.py, sweep_reversion.py, or
markout_tracker.py) so all three can import from here without creating a
circular import.
"""

from __future__ import annotations


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _ramp_unit(value: float, lower: float, upper: float) -> float:
    if upper <= lower:
        return 1.0 if value >= upper else 0.0
    return _clamp((value - lower) / (upper - lower), 0.0, 1.0)


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0.0:
        return price
    return round(price / tick_size) * tick_size


def _ewma(
    previous: float,
    current: float,
    alpha: float,
    *,
    initialized: bool,
) -> float:
    bounded_alpha = _clamp(alpha, 0.0, 1.0)
    if not initialized:
        return current
    return (1.0 - bounded_alpha) * previous + bounded_alpha * current


def _merge_sweep_pending_levels(
    existing_levels: list[tuple[float, float, int]],
    new_entries: list[tuple[float, float, int]],
) -> list[tuple[float, float, int]]:
    """Merge new sweep-ladder entries into the existing pending levels.

    Keys by price so a re-quote at the same price replaces the old entry
    instead of duplicating it.
    """
    by_price = {entry[0]: entry for entry in existing_levels}
    for entry in new_entries:
        by_price[entry[0]] = entry
    return list(by_price.values())
