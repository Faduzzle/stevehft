from __future__ import annotations

import math
from dataclasses import dataclass


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


@dataclass(slots=True)
class CjGlftParameters:
    cj_inventory_skew_ticks_per_lot: float
    cj_horizon_fraction: float
    glft_half_width_ticks: float
    glft_arrival_intensity: float
    glft_depth_sensitivity: float
    maker_net_edge_ticks: float
    maker_edge_intensity_factor: float


@dataclass(frozen=True, slots=True)
class CjGlftModelConfig:
    """Named coefficients for the first-pass CJ + GLFT approximation."""

    horizon_inventory_floor_weight: float = 0.35
    horizon_inventory_dynamic_weight: float = 0.65
    inventory_spread_weight: float = 0.5
    min_inventory_skew_ticks_per_lot: float = 0.05
    arrival_base_intensity: float = 0.05
    fill_probability_intensity_weight: float = 1.25
    queue_support_intensity_weight: float = 0.75
    liquidity_intensity_weight: float = 0.15
    min_arrival_intensity: float = 0.05
    max_arrival_intensity: float = 5.0
    maker_edge_reference_ticks: float = 1.0
    min_maker_edge_intensity_factor: float = 0.05
    max_maker_edge_intensity_factor: float = 1.5
    queue_depth_weight: float = 2.0
    min_depth_sensitivity: float = 0.05
    max_depth_sensitivity: float = 5.0
    risk_term_sigma_scale: float = 0.5
    risk_horizon_floor: float = 0.25
    intensity_discount_weight: float = 0.35
    spread_anchor_weight: float = 0.5


def estimate_cj_glft_parameters(
    *,
    smoothed_spread_ticks: float,
    sigma_realized: float,
    mid_price: float,
    tick_size: float,
    gamma_inventory: float,
    session_minutes_to_close: float,
    session_length_minutes: float,
    passive_fill_probability: float,
    queue_fill_support: float,
    liquidity_score: float,
    maker_net_edge_ticks: float,
    min_half_width_ticks: int,
    max_half_width_ticks: int,
    inventory_skew_cap_ticks: float,
    config: CjGlftModelConfig = CjGlftModelConfig(),
) -> CjGlftParameters:
    horizon_fraction = _clamp(
        session_minutes_to_close / max(session_length_minutes, 1.0),
        0.0,
        1.0,
    )
    sigma_ticks = max(
        sigma_realized * max(mid_price, tick_size) / max(tick_size, 1e-9),
        0.0,
    )

    cj_inventory_skew_ticks_per_lot = _clamp(
        gamma_inventory
        * (
            config.horizon_inventory_floor_weight
            + config.horizon_inventory_dynamic_weight * horizon_fraction
        )
        * (config.inventory_spread_weight * smoothed_spread_ticks + sigma_ticks),
        config.min_inventory_skew_ticks_per_lot,
        max(inventory_skew_cap_ticks, config.min_inventory_skew_ticks_per_lot),
    )

    raw_arrival_intensity = _clamp(
        config.arrival_base_intensity
        + config.fill_probability_intensity_weight * passive_fill_probability
        + config.queue_support_intensity_weight * queue_fill_support
        + config.liquidity_intensity_weight * liquidity_score,
        config.min_arrival_intensity,
        config.max_arrival_intensity,
    )
    maker_edge_intensity_factor = _clamp(
        0.5
        + 0.5
        * maker_net_edge_ticks
        / max(config.maker_edge_reference_ticks, 1e-9),
        config.min_maker_edge_intensity_factor,
        config.max_maker_edge_intensity_factor,
    )
    glft_arrival_intensity = _clamp(
        raw_arrival_intensity * maker_edge_intensity_factor,
        config.min_arrival_intensity,
        config.max_arrival_intensity,
    )
    glft_depth_sensitivity = _clamp(
        math.log1p(
            max(liquidity_score, 0.0)
            + config.queue_depth_weight * queue_fill_support
        )
        / max(smoothed_spread_ticks, 1.0),
        config.min_depth_sensitivity,
        config.max_depth_sensitivity,
    )
    risk_term = (
        config.risk_term_sigma_scale
        * gamma_inventory
        * (sigma_ticks ** 2)
        * (config.risk_horizon_floor + horizon_fraction)
    )
    execution_term = (1.0 / max(gamma_inventory, 1e-6)) * math.log1p(
        gamma_inventory / max(glft_depth_sensitivity, 1e-6)
    )
    intensity_discount = config.intensity_discount_weight * math.log1p(
        glft_arrival_intensity
    )
    glft_half_width_ticks = _clamp(
        config.spread_anchor_weight * smoothed_spread_ticks
        + risk_term
        + execution_term
        - intensity_discount,
        float(min_half_width_ticks),
        float(max_half_width_ticks),
    )

    return CjGlftParameters(
        cj_inventory_skew_ticks_per_lot=cj_inventory_skew_ticks_per_lot,
        cj_horizon_fraction=horizon_fraction,
        glft_half_width_ticks=glft_half_width_ticks,
        glft_arrival_intensity=glft_arrival_intensity,
        glft_depth_sensitivity=glft_depth_sensitivity,
        maker_net_edge_ticks=maker_net_edge_ticks,
        maker_edge_intensity_factor=maker_edge_intensity_factor,
    )
