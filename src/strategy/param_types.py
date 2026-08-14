from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Protocol

from src.data.featurespace.lut import DualLut, PiecewiseLinearLut
from src.data.featurespace.online_stats import (
    CusumDrift,
    DecayingWelford,
    PSquareQuantile,
    PageHinkleyDrift,
    RecursiveLeastSquares,
)
from src.risk.kill_switch import SafeMode


@dataclass(slots=True)
class AdaptiveHistoryConfig:
    depth_levels: int = 3
    depth_fraction: float = 0.25
    min_side_volume_lk: int = 2
    max_side_25pct_levels: float = 3.0
    one_sided_obi_soft_abs: float = 0.45
    one_sided_obi_gate_abs: float = 0.75
    spread_alpha: float = 0.50
    imbalance_alpha: float = 0.25
    # Fast maker-side VOI memory: local depth-flow changes should move quotes quickly.
    local_voi_alpha: float = 0.45
    global_imbalance_alpha: float = 0.15
    global_voi_alpha: float = 0.15
    global_drift_alpha: float = 0.10
    # Slower taker/directional VOI memory: tape-linked confirmation is noisier and
    # should move center/inventory bias more slowly than local quote-flow VOI.
    trade_linked_voi_alpha: float = 0.12
    flow_entropy_alpha: float = 0.12
    taker_overlay_max_queue_share: float = 0.35
    taker_overlay_max_toxicity: float = 0.35
    taker_overlay_cooldown_updates: int = 4
    # AC-style exit: always-on participation rate scaled by urgency.
    # horizon_cycles: target number of cycles to fully exit at zero pressure.
    # urgency_scale: multiplier on adverse_pressure to accelerate exits.
    # max_lots_per_cycle: hard cap on lots per taker exit cycle.
    taker_exit_horizon_cycles: int = 20
    taker_exit_urgency_scale: float = 3.0
    taker_exit_max_lots_per_cycle: int = 2
    depth_alpha: float = 0.20
    depth_shape_alpha: float = 0.20
    return_var_alpha: float = 0.08
    quote_age_alpha: float = 0.20
    fill_rate_alpha: float = 0.12
    fill_arrival_age_alpha: float = 0.12
    fill_arrival_target_ms: float = 500.0
    queue_latency_half_life_ms: float = 500.0
    cancel_rate_alpha: float = 0.12
    toxicity_alpha: float = 0.10
    toxicity_markout_delay_ns: int = 500_000_000
    toxicity_max_pending_fills: int = 256
    # Physics momentum alphas — fast reacts in ~2-3 updates, slow in ~10+
    momentum_fast_alpha: float = 0.45
    momentum_slow_alpha: float = 0.12
    momentum_queue_alpha: float = 0.20
    # Cap on physics_momentum_shift_ticks before blending
    momentum_max_shift_ticks: float = 0.75
    # --- Sweep-reversion overlay ---
    # Strategy: price periodically sweeps away from a short MA, then snaps back.
    # We detect the sweep and front-load a passive limit order AT the short MA,
    # penny-jumping any existing orders there, so we fill on the snap-back.
    #
    # Two MAs from chart analysis:
    #   short_ma_alpha ≈ 0.35  → half-life ~2 updates → 2-4 second attractor
    #   long_ma_alpha  ≈ 0.08  → half-life ~8 updates → slow drift baseline
    #
    # The short MA is the snap-back TARGET.
    # The long MA biases which side we front-load (long MA rising → prefer bids).
    # α=0.10 → half-life ~6.5 updates.  Prior α=0.35 (half-life ~1.7 updates) was too
    # fast: the MA chased price into the sweep, shrinking the measured deviation
    # below the threshold before detection could fire.
    sweep_short_ma_alpha: float = 0.10
    sweep_long_ma_alpha: float = 0.03    # drift baseline MA — half-life ~23 updates
    # Sweep is detected when |price - short_MA| >= threshold ticks.
    sweep_threshold_ticks: float = 2.0
    # Minimum ticks of reversion edge expected after costs.
    sweep_min_edge_ticks: float = 1.0
    sweep_penny_jump_ticks: int = 1
    sweep_drift_bias_ticks: float = 0.5
    mean_reversion_momentum_fade: float = 0.70
    noise_fade_min_zscore: float = 2.0
    noise_fade_max_toxicity: float = 0.30
    noise_fade_max_book_age_ms: float = 250.0
    noise_fade_max_slippage_fraction: float = 0.50
    noise_fade_min_imbalance_counter: float = 0.15
    noise_fade_extra_width_ticks: int = 1
    noise_fade_cooldown_updates: int = 5
    # Mean-reversion residual horizons.  These are interpreted as true
    # time-constants, not per-update windows, so the observer converts each one
    # into alpha_h = 1 - exp(-dt / tau_h) on every book refresh.
    mean_reversion_tau_fast_s: float = 60.0
    mean_reversion_tau_medium_s: float = 300.0
    mean_reversion_tau_slow_s: float = 900.0
    mean_reversion_quality_alpha: float = 0.12
    # Cooldown: reduced to 2 — fast sweeps complete in 2-4 updates, 5 was too slow
    sweep_cooldown_updates: int = 2
    # Guards — counter-imbalance removed: it fires *after* reversal starts, too late
    sweep_max_toxicity: float = 0.30
    sweep_max_book_age_ms: float = 250.0
    # Ladder: how many passive limit levels to spread across the sweep.
    # Level 0 = penny-jumped short MA (snap-back target, lowest edge but most certain).
    # Each deeper level is 1 tick further into the sweep (better entry, rarer fill).
    # All levels are 1 lot each — independent bets, not stacked size.
    sweep_ladder_depth_levels: int = 3
    # Exit: unload the collected sweep position once price (or the short MA itself)
    # returns within this many ticks of the short MA.  Both conditions use the same
    # threshold — covers both "price reverts to mass" and "mass comes to price".
    sweep_exit_threshold_ticks: float = 1.0
    # Dedicated EMA bank for sweep-touch signature detection. This is separate
    # from the 1m/5m/15m momentum residual stack: here we want to infer which
    # shorter-horizon EMA layer a spike just reached/crossed so we can anchor
    # reversal orders at that layer.
    sweep_signature_tau_s: tuple[float, ...] = (
        2.0,
        5.0,
        15.0,
        30.0,
        60.0,
        120.0,
        300.0,
    )
    sleeve_learning_rate: float = 0.20
    sleeve_exploration_floor: float = 0.04
    sleeve_min_base_weight: float = 0.0
    # Global vs local divergence tracking alphas.
    # micro_divergence: EWMA of (local_micro - global_micro) in bps — fast to react.
    # imbalance_divergence: EWMA of (local_depth_imbalance - global_l1_imbalance).
    local_global_micro_divergence_alpha: float = 0.30
    local_global_imbalance_divergence_alpha: float = 0.20
    # Weight given to local depth imbalance vs global L1 imbalance in combined signal.
    # 0.0 = pure global, 1.0 = pure local, 0.4 = current default (40% local).
    imbalance_blend_local_weight: float = 0.40


@dataclass(frozen=True, slots=True)
class ParameterLookupTables:
    toxicity_width_multiplier: PiecewiseLinearLut = field(
        default_factory=lambda: PiecewiseLinearLut.from_pairs(
            (
                (0.0, 1.00),
                (0.2, 1.08),
                (0.4, 1.18),
                (0.7, 1.35),
                (1.0, 1.50),
            )
        )
    )
    toxicity_size_multiplier: PiecewiseLinearLut = field(
        default_factory=lambda: PiecewiseLinearLut.from_pairs(
            (
                (0.0, 1.00),
                (0.2, 0.94),
                (0.4, 0.84),
                (0.7, 0.66),
                (1.0, 0.50),
            )
        )
    )
    inventory_pressure_gamma_multiplier: PiecewiseLinearLut = field(
        default_factory=lambda: PiecewiseLinearLut.from_pairs(
            (
                (0.0, 1.00),
                (0.2, 1.03),
                (0.4, 1.08),
                (0.6, 1.16),
                (0.8, 1.25),
                (1.0, 1.35),
            )
        )
    )


@dataclass(slots=True)
class DynamicLookupTables:
    """Live-blending LUT tables for use during the competition session.

    Each field is a DualLut that starts at the hardcoded backtest breakpoints
    and gradually blends in live observations as samples accumulate.

    Usage:
        tables = DynamicLookupTables.from_backtest(
            toxicity_width_pairs=[(0.0, 1.00), (0.2, 1.08), ...],
            toxicity_size_pairs=[(0.0, 1.00), (0.2, 0.94), ...],
            inventory_gamma_pairs=[(0.0, 1.00), (0.2, 1.03), ...],
        )
        # After each fill, update the live LUT:
        tables.toxicity_width_multiplier.update(toxicity_score, actual_multiplier_used)
        # Evaluate:
        val = tables.toxicity_width_multiplier.evaluate(toxicity_score)
    """

    toxicity_width_multiplier: DualLut = field(
        default_factory=lambda: DualLut.from_static(
            PiecewiseLinearLut.from_pairs(
                (
                    (0.0, 1.00),
                    (0.2, 1.08),
                    (0.4, 1.18),
                    (0.7, 1.35),
                    (1.0, 1.50),
                )
            )
        )
    )
    toxicity_size_multiplier: DualLut = field(
        default_factory=lambda: DualLut.from_static(
            PiecewiseLinearLut.from_pairs(
                (
                    (0.0, 1.00),
                    (0.2, 0.94),
                    (0.4, 0.84),
                    (0.7, 0.66),
                    (1.0, 0.50),
                )
            )
        )
    )
    inventory_pressure_gamma_multiplier: DualLut = field(
        default_factory=lambda: DualLut.from_static(
            PiecewiseLinearLut.from_pairs(
                (
                    (0.0, 1.00),
                    (0.2, 1.03),
                    (0.4, 1.08),
                    (0.6, 1.16),
                    (0.8, 1.25),
                    (1.0, 1.35),
                )
            )
        )
    )

    @classmethod
    def from_backtest(
        cls,
        *,
        toxicity_width_pairs: tuple,
        toxicity_size_pairs: tuple,
        inventory_gamma_pairs: tuple,
        alpha: float = 0.05,
        min_samples_per_bucket: int = 30,
    ) -> DynamicLookupTables:
        """Build DualLuts from backtest breakpoints with live-blending enabled."""
        return cls(
            toxicity_width_multiplier=DualLut.from_static(
                PiecewiseLinearLut.from_pairs(toxicity_width_pairs),
                alpha=alpha,
                min_samples_per_bucket=min_samples_per_bucket,
            ),
            toxicity_size_multiplier=DualLut.from_static(
                PiecewiseLinearLut.from_pairs(toxicity_size_pairs),
                alpha=alpha,
                min_samples_per_bucket=min_samples_per_bucket,
            ),
            inventory_pressure_gamma_multiplier=DualLut.from_static(
                PiecewiseLinearLut.from_pairs(inventory_gamma_pairs),
                alpha=alpha,
                min_samples_per_bucket=min_samples_per_bucket,
            ),
        )

    def evaluate_toxicity_width(self, x: float) -> float:
        return self.toxicity_width_multiplier.evaluate(x)

    def evaluate_toxicity_size(self, x: float) -> float:
        return self.toxicity_size_multiplier.evaluate(x)

    def evaluate_inventory_gamma(self, x: float) -> float:
        return self.inventory_pressure_gamma_multiplier.evaluate(x)


@dataclass(slots=True)
class PendingFillMarkout:
    side_sign: float
    fill_price: float
    event_ts_ns: int


@dataclass(slots=True)
class SymbolAdaptiveState:
    initialized: bool = False
    updates_seen: int = 0
    last_update_ns: int = 0
    last_mid_price: float = 0.0
    ewma_spread_ticks: float = 0.0
    ewma_signed_imbalance: float = 0.0
    ewma_abs_imbalance: float = 0.0
    ewma_local_voi: float = 0.0
    ewma_depth_lk: float = 0.0
    ewma_bid_depth_fraction_levels: float = 0.0
    ewma_ask_depth_fraction_levels: float = 0.0
    ewma_mid_return_var: float = 0.0
    ewma_quote_age_ms: float = 0.0
    ewma_fill_rate: float = 0.0
    ewma_passive_fill_arrival_ms: float = 0.0
    ewma_cancel_rate: float = 0.0
    ewma_toxicity_score: float = 0.0
    ewma_toxicity_markout_pct: float = 0.0
    ewma_global_imbalance: float = 0.0
    ewma_global_voi: float = 0.0
    ewma_global_mid_drift: float = 0.0
    ewma_trade_signed_volume: float = 0.0
    ewma_trade_linked_voi: float = 0.0
    ewma_trade_positive_probability: float = 0.5
    ewma_flow_entropy: float = 0.0
    ewma_depth_entropy: float = 0.0
    spread_welford: DecayingWelford = field(default_factory=DecayingWelford)
    imbalance_welford: DecayingWelford = field(default_factory=DecayingWelford)
    depth_welford: DecayingWelford = field(default_factory=DecayingWelford)
    global_drift_welford: DecayingWelford = field(default_factory=DecayingWelford)
    return_welford: DecayingWelford = field(default_factory=DecayingWelford)
    quote_age_welford: DecayingWelford = field(default_factory=DecayingWelford)
    spread_p90: PSquareQuantile = field(default_factory=lambda: PSquareQuantile(0.90))
    depth_p50: PSquareQuantile = field(default_factory=lambda: PSquareQuantile(0.50))
    quote_age_p90: PSquareQuantile = field(default_factory=lambda: PSquareQuantile(0.90))
    spread_cusum: CusumDrift = field(default_factory=lambda: CusumDrift(threshold=2.5))
    global_drift_page_hinkley: PageHinkleyDrift = field(
        default_factory=lambda: PageHinkleyDrift(threshold=0.8, alpha=0.08)
    )
    drift_rls: RecursiveLeastSquares = field(
        default_factory=lambda: RecursiveLeastSquares(
            feature_count=8,
            forgetting_factor=0.995,
            initial_covariance=10.0,
            # δ=1e-4 with λ=0.995 → steady-state diagonal ~0.02.
            # Prevents covariance blow-up during quiet/low-excitation periods
            # and bounds the effective prior to ~0.14 ticks per weight.
            ridge_coefficient=1e-4,
        )
    )
    prev_rls_features: list[float] = field(default_factory=list)
    rls_drift_prediction_ticks: float = 0.0
    rls_cosine_similarity: float = 0.0
    rls_prediction_variance: float = 1.0
    rls_confidence: float = 0.0
    prev_global_mid: float = 0.0
    # Physics-informed momentum overlay
    # fast_velocity: EWMA of per-update mid-move in ticks (reacts in ~2-3 cycles)
    # slow_velocity: EWMA of mid-move ticks aligned with RLS-predicted drift
    # queue_velocity: EWMA of local VOI * signed-trade-volume pressure
    # liquidity_mass_norm: normalized depth score (heavy book → more inertia)
    # impact_mass_norm: inverse normalized depth score (thin book → more dangerous)
    ewma_fast_velocity_ticks: float = 0.0
    ewma_slow_velocity_ticks: float = 0.0
    ewma_queue_velocity: float = 0.0
    liquidity_mass_norm: float = 0.5
    impact_mass_norm: float = 0.5
    pending_fill_markouts: list[PendingFillMarkout] = field(default_factory=list)
    toxicity_seen_fill_count_by_order_id: Dict[str, int] = field(default_factory=dict)
    last_fill_count: int = 0
    last_cancel_count: int = 0
    last_taker_overlay_update_seen: int = -1_000_000
    # Sweep-reversion tracking
    # ewma_return_zscore: fast EWMA of per-update return z-score (sweep detector)
    ewma_return_zscore: float = 0.0
    last_sweep_reversion_update_seen: int = -1_000_000
    # ewma_sweep_short_ma: fast attractor — snap-back TARGET price
    ewma_sweep_short_ma: float = 0.0
    # ewma_sweep_long_ma: slow drift baseline — biases which side to front-load
    ewma_sweep_long_ma: float = 0.0
    # Backward-compatible aliases used by the current noise-fade overlay.
    ewma_noise_fade_ma: float = 0.0
    last_noise_fade_update_seen: int = -1_000_000
    # OU-style residual stack around three EMA horizons:
    #   fast   ~ 1 minute
    #   medium ~ 5 minutes
    #   slow   ~ 15 minutes
    # For each horizon, `mr_error_*_ticks = mid - ema_h` in ticks and
    # `mr_reversion_quality_*` tracks whether prior error tends to mean-revert
    # (1) or trend-follow (0).
    mr_ema_fast_price: float = 0.0
    mr_ema_medium_price: float = 0.0
    mr_ema_slow_price: float = 0.0
    mr_error_fast_ticks: float = 0.0
    mr_error_medium_ticks: float = 0.0
    mr_error_slow_ticks: float = 0.0
    mr_reversion_quality_fast: float = 0.5
    mr_reversion_quality_medium: float = 0.5
    mr_reversion_quality_slow: float = 0.5
    mr_crossed_fast: bool = False
    mr_crossed_medium: bool = False
    mr_crossed_slow: bool = False
    mr_cross_direction: int = 0
    mr_cross_depth: int = 0
    mr_cross_anchor_price: float = 0.0
    # Sweep fill tracking: accumulates fills placed by the sweep-reversion overlay.
    # sweep_fill_price: VWAP of open sweep fills (0.0 = no open sweep position)
    # sweep_fill_lots:  net lots held from sweep fills (+ve = long/bought-dip,
    #                   -ve = short/sold-spike)
    # sweep_fill_side_sign: +1 if we bought the dip, -1 if we sold the spike
    sweep_fill_price: float = 0.0
    sweep_fill_lots: int = 0
    sweep_fill_side_sign: float = 0.0
    last_realized_symbol_pnl: float = 0.0
    last_sleeve_credit_by_name: Dict[str, float] = field(default_factory=dict)
    # Session state tracking: used to detect session-open transitions so stale
    # sweep fill state from the prior session can be zeroed out.
    last_session_open: bool = True
    # Pending detection: one entry per ladder level placed by the sweep overlay.
    # Each entry is (limit_price, side_sign, fills_already_seen_count).
    # Cleared per-entry once the order at that level is fully matched.
    sweep_pending_levels: list[tuple[float, float, int]] = field(default_factory=list)
    sweep_signature_ema_prices: list[float] = field(default_factory=list)
    sweep_cross_direction: int = 0
    sweep_cross_depth: int = 0
    sweep_cross_anchor_tau_s: float = 0.0
    sweep_cross_anchor_price: float = 0.0
    # Warmup tracking: monotonic ns timestamp of first update for this symbol.
    # Used to enforce a startup warmup period regardless of session clock position.
    first_update_ns: int = 0
    # Global vs local divergence: EWMA of (local_micro - global_micro) in bps.
    # Positive = local book microprice is running hot vs global baseline.
    ewma_local_global_micro_divergence_bps: float = 0.0
    # EWMA of (local_depth_imbalance - global_l1_imbalance).
    # Positive = local book has more relative bid depth than global baseline.
    ewma_imbalance_divergence: float = 0.0


@dataclass(slots=True)
class SymbolStrategyParameters:
    gamma_inventory: float = 1.0
    quote_size_lots: int = 1
    passive_ladder_depth_levels: int = 2
    bid_size_multiplier: float = 1.0
    ask_size_multiplier: float = 1.0
    bid_width_multiplier: float = 1.0
    ask_width_multiplier: float = 1.0
    spread_floor_ticks: int = 1
    spread_ceiling_ticks: int = 4
    max_live_spread_ticks: int = 8
    quote_age_limit_ms: int = 750
    microprice_weight: float = 0.75
    imbalance_shift_ticks: float = 0.5
    inventory_skew_cap_ticks: float = 3.0
    inventory_limit_lots: int = 5
    # Signed target inventory the strategy wants to hold at rest.
    # Derived from long MA vs short MA drift direction.  +1 means hold 1 extra long,
    # -1 means hold 1 extra short.  Effective bid limit = limit + target,
    # effective ask limit = limit - target (asymmetric tolerance).
    inventory_target_lots: int = 0
    symbol_enable_flag: bool = True
    bid_enable_flag: bool = True
    ask_enable_flag: bool = True
    allocation_weight: float = 1.0
    pace_multiplier: float = 1.0
    sigma_realized: float = 0.0
    passive_fill_probability: float = 0.5
    passive_fill_arrival_ms: float = 0.0
    fill_arrival_speed_score: float = 0.0
    live_quote_age_ms: float = 0.0
    recent_fill_rate: float = 0.0
    recent_cancel_rate: float = 0.0
    bid_book_volume_lk: int = 0
    ask_book_volume_lk: int = 0
    depth_baseline_lk: float = 0.0
    depth_zscore: float = 0.0
    bid_25pct_levels: float = 1.0
    ask_25pct_levels: float = 1.0
    depth_concentration_score: float = 0.0
    front_shape_imbalance: float = 0.0
    front_shape_shift_ticks: float = 0.0
    side_size_tilt: float = 0.0
    side_width_tilt: float = 0.0
    session_elapsed_fraction: float = 0.0
    session_minutes_to_close: float = 390.0
    expected_trade_count: float = 0.0
    observed_trade_count: int = 0
    trade_count_ratio: float = 1.0
    close_urgency_multiplier: float = 1.0
    flatten_only_mode: bool = False
    safe_mode: str = SafeMode.NORMAL.value
    spread_mean_ticks: float = 0.0
    spread_std_ticks: float = 0.0
    spread_p90_ticks: float = 0.0
    spread_zscore: float = 0.0
    local_imbalance_zscore: float = 0.0
    one_sided_obi_zscore: float = 0.0
    global_drift_zscore: float = 0.0
    spread_regime_signal: int = 0
    global_drift_regime_signal: int = 0
    quote_age_mean_ms: float = 0.0
    quote_age_p90_ms: float = 0.0
    estimated_rebate: float = 0.0
    estimated_fee: float = 0.0
    estimated_net_fee: float = 0.0
    passive_fill_ratio: float = 0.0
    avg_net_fee_per_fill: float = 0.0
    execution_cost_score: float = 0.0
    expected_passive_slippage_ticks: float = 0.0
    expected_aggressive_slippage_ticks: float = 0.0
    maker_net_edge_ticks: float = 0.0
    maker_edge_intensity_factor: float = 1.0
    slippage_quality_score: float = 1.0
    queue_fill_support: float = 0.0
    queue_latency_haircut: float = 1.0
    bid_queue_ahead_lots: int = 0
    ask_queue_ahead_lots: int = 0
    bid_queue_share: float = 0.0
    ask_queue_share: float = 0.0
    local_microprice: float = 0.0
    local_depth_imbalance: float = 0.0
    local_multi_level_voi: float = 0.0
    global_l1_imbalance: float = 0.0
    global_l1_voi: float = 0.0
    global_mid_drift: float = 0.0
    rls_drift_prediction_ticks: float = 0.0
    rls_confidence: float = 0.0
    rls_cosine_similarity: float = 0.0
    rls_prediction_variance: float = 1.0
    lead_lag_shift_ticks: float = 0.0
    lead_lag_confidence: float = 0.0
    lead_lag_edge_score: float = 0.0
    lead_lag_leader_symbol: str = ""
    pair_spread_shift_ticks: float = 0.0
    pair_spread_confidence: float = 0.0
    pair_spread_residual_ticks: float = 0.0
    pair_spread_peer_symbol: str = ""
    inventory_hedge_multiplier: float = 1.0
    inventory_hedge_confidence: float = 0.0
    inventory_hedge_ratio: float = 0.0
    inventory_hedge_symbol: str = ""
    trade_signed_volume: float = 0.0
    trade_linked_voi_score: float = 0.0
    depth_entropy: float = 0.0
    flow_entropy: float = 0.0
    entropy_confidence_score: float = 1.0
    fast_maker_voi_pressure: float = 0.0
    slow_trade_voi_pressure: float = 0.0
    taker_overlay_mode: bool = False
    taker_overlay_size_lots: int = 0
    taker_overlay_edge_ticks: float = 0.0
    global_drift_shift_ticks: float = 0.0
    drift_agreement_multiplier: float = 1.0
    drift_inventory_bias_ticks: float = 0.0
    realized_symbol_pnl: float = 0.0
    pnl_allocation_score: float = 0.5
    toxicity_score: float = 0.0
    toxicity_markout_pct: float = 0.0
    toxicity_width_multiplier: float = 1.0
    toxicity_size_multiplier: float = 1.0
    cj_inventory_skew_ticks_per_lot: float = 1.0
    cj_horizon_fraction: float = 1.0
    glft_half_width_ticks: float = 1.0
    glft_arrival_intensity: float = 0.0
    glft_depth_sensitivity: float = 0.0
    inventory_close_penalty: float = 0.0
    inventory_pressure_score: float = 0.0
    max_affordable_bid_flatten_lots: int = 0
    estimated_reserved_buying_power: float = 0.0
    estimated_short_close_buying_power: float = 0.0
    estimated_available_buying_power: float = 0.0
    buying_power_usage_ratio: float = 0.0
    buying_power_scale: float = 1.0
    # Physics-informed momentum overlay
    fast_velocity_ticks: float = 0.0
    slow_velocity_ticks: float = 0.0
    queue_velocity: float = 0.0
    liquidity_mass_norm: float = 0.5
    impact_mass_norm: float = 0.5
    physics_momentum_shift_ticks: float = 0.0
    # Sweep-reversion overlay
    sweep_deviation_ticks: float = 0.0     # current |price - short_MA| in ticks
    sweep_short_ma_price: float = 0.0      # the snap-back target price
    sweep_long_ma_price: float = 0.0       # slow drift baseline
    sweep_detected: bool = False           # sweep threshold crossed this update
    # Ladder of (price, size) pairs for the sweep entry, one per level.
    # Level 0 = penny-jumped short MA; deeper levels fan into the sweep.
    sweep_reversion_bid_levels: tuple[tuple[float, int], ...] = ()
    sweep_reversion_ask_levels: tuple[tuple[float, int], ...] = ()
    sweep_reversion_bid_px: float = 0.0
    sweep_reversion_ask_px: float = 0.0
    sweep_reversion_mode: bool = False     # overlay is active
    sweep_reversion_size_lots: int = 0     # total lots across all levels
    # Exit: unload accumulated sweep position when price/MA returns to the mass
    sweep_exit_mode: bool = False          # exit overlay active this update
    sweep_exit_lots: int = 0              # lots to unload
    sweep_exit_side_sign: float = 0.0    # +1 = sell to exit long, -1 = buy to exit short
    sweep_exit_trigger: str = ""          # "price_reverted" | "ma_came_to_price" | ""
    # VWAP price of open sweep fills — used to compute minimum-edge exit price
    sweep_fill_price: float = 0.0
    sweep_cross_direction: int = 0
    sweep_cross_depth: int = 0
    sweep_cross_anchor_tau_s: float = 0.0
    sweep_cross_anchor_price: float = 0.0
    mean_reversion_score: float = 0.0     # used to gate momentum suppression
    noise_fade_shift_ticks: float = 0.0   # passive center shift (marker mode bias)
    mr_error_fast_ticks: float = 0.0
    mr_error_medium_ticks: float = 0.0
    mr_error_slow_ticks: float = 0.0
    mr_reversion_quality_fast: float = 0.5
    mr_reversion_quality_medium: float = 0.5
    mr_reversion_quality_slow: float = 0.5
    mr_crossed_fast: bool = False
    mr_crossed_medium: bool = False
    mr_crossed_slow: bool = False
    mr_cross_direction: int = 0
    mr_cross_depth: int = 0
    mr_cross_anchor_price: float = 0.0
    return_zscore: float = 0.0
    noise_fade_taker_mode: bool = False
    noise_fade_taker_size_lots: int = 0
    noise_fade_ma_price: float = 0.0
    noise_fade_bid_px: float = 0.0
    noise_fade_ask_px: float = 0.0
    sleeve_weight_base_mm: float = 1.0
    sleeve_weight_lead_lag: float = 0.0
    sleeve_weight_pair_spread: float = 0.0
    sleeve_weight_inv_hedge: float = 0.0
    sleeve_weight_taker_exit: float = 0.0
    sleeve_weight_noise_fade: float = 0.0
    sleeve_weight_cash: float = 0.0
    sleeve_credit_base_mm: float = 0.0
    # Global vs local divergence output signals
    local_global_micro_divergence_bps: float = 0.0
    local_global_imbalance_divergence: float = 0.0
    local_global_divergence_shift_ticks: float = 0.0
    sleeve_credit_lead_lag: float = 0.0
    sleeve_credit_pair_spread: float = 0.0
    sleeve_credit_inv_hedge: float = 0.0
    sleeve_credit_taker_exit: float = 0.0
    sleeve_credit_noise_fade: float = 0.0


@dataclass(slots=True)
class StrategyParameterBundle:
    symbol: str
    values: SymbolStrategyParameters
    source: str = "static_defaults"
    version: str = "v1"


class StrategyParameterProvider(Protocol):
    def for_symbol(
        self,
        symbol: str,
        *,
        inventory_lots: int = 0,
    ) -> StrategyParameterBundle: ...
