from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExpectedSlippageModelConfig:
    """Explicit knobs for the first-pass expected slippage proxy."""

    base_adverse_selection: float = 0.15
    toxicity_adverse_selection_weight: float = 0.85
    queue_penalty_weight: float = 0.25
    liquidity_relief_weight: float = 0.10
    liquidity_relief_threshold: float = 1.0
    liquidity_relief_cap: float = 2.0
    half_spread_crossing_fraction: float = 0.5


def signed_shortfall_per_share(
    *,
    side: object,
    fill_price: float,
    benchmark_price: float,
) -> float:
    if fill_price <= 0.0 or benchmark_price <= 0.0:
        return 0.0
    side_value = str(getattr(side, "value", side)).lower()
    if side_value == "bid":
        return fill_price - benchmark_price
    return benchmark_price - fill_price


def signed_shortfall_ticks(
    *,
    side: object,
    fill_price: float,
    benchmark_price: float,
    tick_size: float,
) -> float:
    if tick_size <= 0.0:
        return 0.0
    return signed_shortfall_per_share(
        side=side,
        fill_price=fill_price,
        benchmark_price=benchmark_price,
    ) / tick_size


def estimate_expected_passive_slippage_ticks(
    *,
    spread_ticks: float,
    toxicity_score: float,
    queue_fill_support: float,
    liquidity_score: float,
    config: ExpectedSlippageModelConfig = ExpectedSlippageModelConfig(),
) -> float:
    adverse_selection = config.base_adverse_selection + (
        config.toxicity_adverse_selection_weight * max(toxicity_score, 0.0)
    )
    queue_penalty = config.queue_penalty_weight * max(1.0 - queue_fill_support, 0.0)
    liquidity_relief = config.liquidity_relief_weight * max(
        min(liquidity_score, config.liquidity_relief_cap)
        - config.liquidity_relief_threshold,
        0.0,
    )
    return max(
        0.0,
        config.half_spread_crossing_fraction
        * max(spread_ticks, 0.0)
        * adverse_selection
        + queue_penalty
        - liquidity_relief,
    )


def estimate_expected_aggressive_slippage_ticks(
    *,
    spread_ticks: float,
    config: ExpectedSlippageModelConfig = ExpectedSlippageModelConfig(),
) -> float:
    return max(
        config.half_spread_crossing_fraction * max(spread_ticks, 0.0),
        0.0,
    )
