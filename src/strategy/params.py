from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict

from src.core.session_clock import SessionClock, SessionProgress
from src.data.state import MarketState, SymbolState
from src.execution.order_state import (
    LIMIT_REBATE_PER_SHARE,
    MARKET_FEE_PER_SHARE,
    OrderLedger,
    OrderLiquidity,
    OrderSide,
)
from src.execution.slippage import (
    estimate_expected_aggressive_slippage_ticks,
    estimate_expected_passive_slippage_ticks,
)
from src.risk.inventory import PortfolioLedger
from src.risk.kill_switch import KillSwitchController, SafeMode
from src.strategy.cj_glft import estimate_cj_glft_parameters
from src.strategy.lead_lag_graph import OnlineLeadLagGraph
from src.strategy.param_observer import observe_symbol_state
from src.telemetry.calibration_logger import CalibrationLogger, build_calibration_row
from src.strategy.param_types import (
    AdaptiveHistoryConfig,
    DynamicLookupTables,
    ParameterLookupTables,
    PendingFillMarkout,
    StrategyParameterBundle,
    StrategyParameterProvider,
    SymbolAdaptiveState,
    SymbolStrategyParameters,
)
from src.strategy.strategy_allocator import (
    OnlineStrategyAllocator,
    OnlineStrategyAllocatorConfig,
    sleeve_scale,
)


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


def _signed_min_abs(
    value: float,
    direction_source: float,
    min_abs: float,
) -> float:
    if value == 0.0 and direction_source == 0.0:
        return 0.0
    direction = 1.0 if (value if value != 0.0 else direction_source) > 0.0 else -1.0
    return direction * max(abs(value), min_abs)


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


def _exploration_probe_sleeve(updates_seen: int) -> str:
    probe_cycle = ("LEAD_LAG", "PAIR_SPREAD", "INV_HEDGE", "TAKER_EXIT", "NOISE_FADE")
    return probe_cycle[max(updates_seen, 0) % len(probe_cycle)]


def _normalize_sleeve_credit(
    sleeve_credit_by_name: dict[str, float],
) -> dict[str, float]:
    total_credit = sum(max(float(value), 0.0) for value in sleeve_credit_by_name.values())
    if total_credit <= 1e-12:
        return {}
    return {
        sleeve_name: max(float(value), 0.0) / total_credit
        for sleeve_name, value in sleeve_credit_by_name.items()
        if value > 0.0
    }


def _sleeve_rewards_from_realized_pnl(
    *,
    pnl_delta: float,
    previous_credit_by_name: dict[str, float],
    mid_price: float,
) -> dict[str, float]:
    if abs(pnl_delta) <= 1e-9 or not previous_credit_by_name:
        return {}
    pnl_reward = _clamp(
        pnl_delta / max(abs(mid_price) * 100.0 * 0.01, 1.0),
        -1.0,
        1.0,
    )
    return {
        sleeve_name: _clamp(pnl_reward * credit, -1.0, 1.0)
        for sleeve_name, credit in previous_credit_by_name.items()
    }


@dataclass(slots=True)
class StaticParameterProvider:
    default: SymbolStrategyParameters = field(default_factory=SymbolStrategyParameters)
    overrides: Dict[str, SymbolStrategyParameters] = field(default_factory=dict)
    source: str = "static_defaults"
    version: str = "v1"

    def for_symbol(
        self,
        symbol: str,
        *,
        inventory_lots: int = 0,
    ) -> StrategyParameterBundle:
        del inventory_lots
        override = self.overrides.get(symbol)
        values = override if override is not None else self.default
        return StrategyParameterBundle(
            symbol=symbol,
            values=values,
            source=self.source,
            version=self.version,
        )


@dataclass(slots=True)
class AdaptiveParameterProvider:
    market_state: MarketState
    tick_size: float
    # Per-symbol overrides for tick size.  Symbols not present here fall back to
    # the global `tick_size`.
    tick_size_by_symbol: Dict[str, float] = field(default_factory=dict)
    order_ledger: OrderLedger | None = None
    portfolio_ledger: PortfolioLedger | None = None
    session_clock: SessionClock | None = None
    kill_switch: KillSwitchController | None = None
    fallback: SymbolStrategyParameters = field(default_factory=SymbolStrategyParameters)
    history_config: AdaptiveHistoryConfig = field(default_factory=AdaptiveHistoryConfig)
    lookup_tables: ParameterLookupTables | DynamicLookupTables = field(default_factory=ParameterLookupTables)
    lead_lag_graph: OnlineLeadLagGraph = field(default_factory=OnlineLeadLagGraph)
    calibration_logger: CalibrationLogger | None = None
    source: str = "adaptive_live_state"
    version: str = "v1-adaptive"
    state_by_symbol: Dict[str, SymbolAdaptiveState] = field(default_factory=dict)
    sleeve_allocator_by_symbol: Dict[str, OnlineStrategyAllocator] = field(
        default_factory=dict
    )

    def for_symbol(
        self,
        symbol: str,
        *,
        inventory_lots: int = 0,
    ) -> StrategyParameterBundle:
        state = self.market_state.by_symbol.get(symbol)
        if state is None:
            return StrategyParameterBundle(
                symbol=symbol,
                values=self.fallback,
                source=f"{self.source}:fallback",
                version=self.version,
            )

        now_ns = time.monotonic_ns()
        adaptive_state = self._observe_state(symbol, state, now_ns=now_ns)
        session_progress = self._session_progress()
        values = self._derive_from_state(
            state,
            adaptive_state,
            now_ns=now_ns,
            inventory_lots=inventory_lots,
            session_progress=session_progress,
        )
        if self.calibration_logger is not None and adaptive_state.initialized:
            depth_levels = self.history_config.depth_levels
            book_depth = float(
                state.cumulative_bid_depth(depth_levels, local=True)
                + state.cumulative_ask_depth(depth_levels, local=True)
            )
            signed_imbalance = state.depth_imbalance(depth_levels, local=True)
            local_voi = state.local_multi_level_voi
            local_trade_linked_voi = (
                max(-1.0, min(local_voi, 1.0))
                * max(-1.0, min(state.trade_signed_volume / max(book_depth, 1.0), 1.0))
            )
            rls_features = [
                1.0,
                max(-1.0, min(signed_imbalance, 1.0)),
                max(-1.0, min(local_voi, 1.0)),
                max(-1.0, min(state.global_l1_imbalance, 1.0)),
                max(-1.0, min(state.global_l1_voi, 1.0)),
                max(-1.0, min(local_trade_linked_voi, 1.0)),
            ]
            global_mid = state.global_l1_mid
            _sym_tick = self._tick_size_for(symbol)
            global_mid_drift_ticks = (
                (global_mid - adaptive_state.prev_global_mid) / max(_sym_tick, 1e-9)
                if adaptive_state.prev_global_mid > 0.0 and global_mid > 0.0
                else 0.0
            )
            spread_ticks_now = state.spread / _sym_tick if _sym_tick > 0.0 else 0.0
            self.calibration_logger.log_row(
                build_calibration_row(
                    symbol=symbol,
                    ts_ns=now_ns,
                    adaptive_state=adaptive_state,
                    spread_ticks=spread_ticks_now,
                    book_depth_lk=book_depth,
                    signed_imbalance=signed_imbalance,
                    local_voi=local_voi,
                    global_imbalance=state.global_l1_imbalance,
                    global_voi=state.global_l1_voi,
                    global_mid_drift_ticks=global_mid_drift_ticks,
                    depth_entropy=state.depth_entropy(depth_levels, local=True),
                    local_trade_linked_voi=local_trade_linked_voi,
                    trade_signed_volume=state.trade_signed_volume,
                    book_age_ms=max(
                        (now_ns - state.last_book_update_ns) / 1_000_000, 0.0
                    ) if state.last_book_update_ns > 0 else 0.0,
                    quote_age_ms=values.live_quote_age_ms,
                    rls_features=rls_features,
                    inventory_lots=inventory_lots,
                    toxicity_markout_pct=adaptive_state.ewma_toxicity_markout_pct,
                    session_elapsed_fraction=values.session_elapsed_fraction,
                    session_minutes_to_close=values.session_minutes_to_close,
                    sweep_deviation_ticks=values.sweep_deviation_ticks,
                    sweep_short_ma_price=values.sweep_short_ma_price,
                    sweep_long_ma_price=values.sweep_long_ma_price,
                    sweep_detected=values.sweep_detected,
                    sweep_reversion_mode=values.sweep_reversion_mode,
                    sweep_exit_mode=values.sweep_exit_mode,
                    sweep_exit_trigger=values.sweep_exit_trigger,
                )
            )
        return StrategyParameterBundle(
            symbol=symbol,
            values=values,
            source=self.source,
            version=self.version,
        )

    def reset_adaptive_state(self) -> None:
        self.state_by_symbol.clear()
        self.sleeve_allocator_by_symbol.clear()
        self.lead_lag_graph = OnlineLeadLagGraph()

    def update_lead_lag_graph(self, observations) -> None:
        self.lead_lag_graph.update(
            observations,
            tick_size=self.tick_size,
        )

    def _observe_state(
        self,
        symbol: str,
        state: SymbolState,
        *,
        now_ns: int,
    ) -> SymbolAdaptiveState:
        return observe_symbol_state(self, symbol, state, now_ns=now_ns)

    def _sleeve_allocator_for_symbol(
        self,
        symbol: str,
    ) -> OnlineStrategyAllocator:
        allocator = self.sleeve_allocator_by_symbol.get(symbol)
        if allocator is None:
            allocator = OnlineStrategyAllocator(
                config=OnlineStrategyAllocatorConfig(
                    learning_rate=self.history_config.sleeve_learning_rate,
                    exploration_floor=self.history_config.sleeve_exploration_floor,
                    min_base_weight=self.history_config.sleeve_min_base_weight,
                )
            )
            self.sleeve_allocator_by_symbol[symbol] = allocator
        return allocator

    def _derive_from_state(
        self,
        state: SymbolState,
        adaptive_state: SymbolAdaptiveState,
        *,
        now_ns: int,
        inventory_lots: int,
        session_progress: SessionProgress | None,
    ) -> SymbolStrategyParameters:
        tick_size = self._tick_size_for(state.symbol)
        spread_ticks = state.spread / tick_size if tick_size > 0.0 else 0.0
        smoothed_spread_ticks = (
            adaptive_state.spread_welford.mean
            if adaptive_state.initialized
            else spread_ticks
        )
        spread_std_ticks = (
            adaptive_state.spread_welford.std
            if adaptive_state.initialized
            else 0.0
        )
        spread_zscore = (
            adaptive_state.spread_welford.zscore(spread_ticks)
            if adaptive_state.initialized
            else 0.0
        )
        spread_p90_ticks = (
            adaptive_state.spread_p90.estimate
            if adaptive_state.initialized
            else spread_ticks
        )
        depth_levels = self.history_config.depth_levels
        depth_fraction = self.history_config.depth_fraction
        book_depth = (
            state.cumulative_bid_depth(depth_levels, local=True)
            + state.cumulative_ask_depth(depth_levels, local=True)
        )
        bid_book_volume = state.cumulative_bid_depth(depth_levels, local=True)
        ask_book_volume = state.cumulative_ask_depth(depth_levels, local=True)
        smoothed_depth = (
            adaptive_state.ewma_depth_lk
            if adaptive_state.initialized
            else float(book_depth)
        )
        depth_baseline_lk = max(
            adaptive_state.depth_p50.estimate
            if adaptive_state.initialized
            else float(book_depth),
            1.0,
        )
        depth_zscore = (
            adaptive_state.depth_welford.zscore(float(book_depth), min_std=1.0)
            if adaptive_state.initialized
            else 0.0
        )
        imbalance_now = state.depth_imbalance(depth_levels, local=True)
        smoothed_signed_imbalance = (
            adaptive_state.ewma_signed_imbalance
            if adaptive_state.initialized
            else imbalance_now
        )
        local_imbalance_zscore = (
            adaptive_state.imbalance_welford.zscore(imbalance_now)
            if adaptive_state.initialized
            else 0.0
        )
        imbalance_abs = abs(imbalance_now)
        smoothed_imbalance = (
            adaptive_state.ewma_abs_imbalance
            if adaptive_state.initialized
            else imbalance_abs
        )
        bid_fraction_levels = (
            adaptive_state.ewma_bid_depth_fraction_levels
            if adaptive_state.initialized
            else float(
                state.levels_to_cover_side_volume_fraction(
                    "bid",
                    depth_fraction,
                    local=True,
                ),
            )
        )
        ask_fraction_levels = (
            adaptive_state.ewma_ask_depth_fraction_levels
            if adaptive_state.initialized
            else float(
                state.levels_to_cover_side_volume_fraction("ask", depth_fraction, local=True),
            )
        )
        avg_fraction_levels = 0.5 * (bid_fraction_levels + ask_fraction_levels)
        depth_concentration_score = _clamp(
            1.0 - ((avg_fraction_levels - 1.0) / max(depth_levels - 1.0, 1.0)),
            0.0,
            1.0,
        )
        front_shape_imbalance = _clamp(
            (ask_fraction_levels - bid_fraction_levels)
            / max(ask_fraction_levels + bid_fraction_levels, 1.0),
            -1.0,
            1.0,
        )
        front_shape_shift_ticks = _clamp(
            0.75 * front_shape_imbalance * depth_concentration_score,
            -0.75,
            0.75,
        )
        smoothed_global_imbalance = (
            adaptive_state.ewma_global_imbalance
            if adaptive_state.initialized
            else state.global_l1_imbalance
        )
        smoothed_local_voi = (
            adaptive_state.ewma_local_voi
            if adaptive_state.initialized
            else state.local_multi_level_voi
        )
        smoothed_global_voi = (
            adaptive_state.ewma_global_voi
            if adaptive_state.initialized
            else state.global_l1_voi
        )
        smoothed_trade_signed_volume = (
            adaptive_state.ewma_trade_signed_volume
            if adaptive_state.initialized
            else state.trade_signed_volume
        )
        smoothed_trade_linked_voi = (
            adaptive_state.ewma_trade_linked_voi
            if adaptive_state.initialized
            else 0.0
        )
        depth_entropy = (
            adaptive_state.ewma_depth_entropy
            if adaptive_state.initialized
            else state.depth_entropy(depth_levels, local=True)
        )
        flow_entropy = (
            adaptive_state.ewma_flow_entropy
            if adaptive_state.initialized
            else 0.0
        )
        entropy_confidence_score = _clamp(
            1.0 - 0.12 * depth_entropy - 0.18 * flow_entropy,
            0.70,
            1.0,
        )
        fast_maker_voi_pressure = _clamp(
            smoothed_signed_imbalance + 0.35 * smoothed_local_voi,
            -1.0,
            1.0,
        )
        slow_trade_voi_pressure = _clamp(
            0.70 * smoothed_trade_linked_voi + 0.30 * smoothed_global_voi,
            -1.0,
            1.0,
        )
        local_pressure_signal = _clamp(
            0.85 * fast_maker_voi_pressure + 0.15 * slow_trade_voi_pressure,
            -1.0,
            1.0,
        )
        smoothed_global_drift = (
            adaptive_state.ewma_global_mid_drift
            if adaptive_state.initialized
            else 0.0
        )
        global_drift_ticks_now = smoothed_global_drift / max(tick_size, 1e-9)
        rls_drift_prediction_ticks_raw = (
            adaptive_state.rls_drift_prediction_ticks
            if adaptive_state.initialized
            else 0.0
        )
        rls_confidence = (
            adaptive_state.rls_confidence
            if adaptive_state.initialized
            else 0.0
        )
        rls_cosine_similarity = (
            adaptive_state.rls_cosine_similarity
            if adaptive_state.initialized
            else 0.0
        )
        rls_prediction_variance = (
            adaptive_state.rls_prediction_variance
            if adaptive_state.initialized
            else 1.0
        )
        lead_lag_overlay = self.lead_lag_graph.overlay_for(state.symbol)
        pair_spread_overlay = self.lead_lag_graph.spread_overlay_for(state.symbol)
        # Scale the RLS contribution by confidence before it influences
        # any center/shift calculations.  Full-confidence prediction uses
        # the raw value; zero-confidence falls back to zero (no contribution).
        rls_drift_prediction_ticks = rls_drift_prediction_ticks_raw * rls_confidence
        global_drift_zscore = (
            adaptive_state.global_drift_welford.zscore(global_drift_ticks_now)
            if adaptive_state.initialized
            else 0.0
        )
        spread_regime_signal = adaptive_state.spread_cusum.last_signal
        global_drift_regime_signal = (
            adaptive_state.global_drift_page_hinkley.last_signal
        )
        regime_widen_multiplier = _clamp(
            1.0
            + 0.12 * abs(spread_regime_signal)
            + 0.08 * abs(global_drift_regime_signal),
            1.0,
            1.25,
        )
        regime_size_multiplier = _clamp(
            1.0
            - 0.08 * abs(spread_regime_signal)
            - 0.04 * abs(global_drift_regime_signal),
            0.80,
            1.0,
        )
        # Base drift estimate, before the lead-lag sleeve signal is blended in
        # below (see global_drift_shift_ticks near the end of this method).
        # drift_inventory_bias_ticks and the taker-exit overlay read this
        # pre-lead-lag value, so they are not contaminated by the sleeve signal.
        # Kept unclamped here so the final blend (which reuses this same
        # weighted sum) is not double-clamped before its own -2..2 range.
        drift_components_ticks = (
            0.35 * global_drift_ticks_now
            + 0.25 * smoothed_global_voi
            + 0.15 * slow_trade_voi_pressure
            + 0.25 * rls_drift_prediction_ticks
        )
        base_global_drift_shift_ticks = _clamp(drift_components_ticks, -1.0, 1.0)
        drift_agreement_multiplier = 1.0 + 0.3 * _clamp(
            local_pressure_signal
            * (
                0.60 * smoothed_global_imbalance
                + 0.20 * smoothed_global_voi
                + 0.20 * slow_trade_voi_pressure
            ),
            0.0,
            1.0,
        )
        side_size_tilt = _clamp(
            0.6 * smoothed_signed_imbalance
            + 0.4 * front_shape_imbalance
            + 0.15 * smoothed_local_voi
            + 0.10 * slow_trade_voi_pressure,
            -1.0,
            1.0,
        )
        toxicity_score = _clamp(adaptive_state.ewma_toxicity_score, 0.0, 1.0)
        toxicity_markout_pct = max(adaptive_state.ewma_toxicity_markout_pct, 0.0)
        toxicity_width_multiplier = _clamp(
            self.lookup_tables.toxicity_width_multiplier.evaluate(toxicity_score),
            1.0,
            1.5,
        )
        toxicity_size_multiplier = _clamp(
            self.lookup_tables.toxicity_size_multiplier.evaluate(toxicity_score),
            0.5,
            1.0,
        )
        bid_size_multiplier = _clamp(
            (1.0 - 0.5 * side_size_tilt)
            * toxicity_size_multiplier
            * entropy_confidence_score
            * regime_size_multiplier,
            0.25,
            2.0,
        )
        ask_size_multiplier = _clamp(
            (1.0 + 0.5 * side_size_tilt)
            * toxicity_size_multiplier
            * entropy_confidence_score
            * regime_size_multiplier,
            0.25,
            2.0,
        )
        side_width_tilt = _clamp(
            0.4 * fast_maker_voi_pressure
            + 0.6 * front_shape_imbalance
            + 0.05 * slow_trade_voi_pressure,
            -1.0,
            1.0,
        )
        bid_width_multiplier = _clamp(
            (1.0 + 0.35 * side_width_tilt)
            * toxicity_width_multiplier
            * (1.0 + 0.35 * (1.0 - entropy_confidence_score))
            * regime_widen_multiplier,
            0.5,
            1.5,
        )
        ask_width_multiplier = _clamp(
            (1.0 - 0.35 * side_width_tilt)
            * toxicity_width_multiplier
            * (1.0 + 0.35 * (1.0 - entropy_confidence_score))
            * regime_widen_multiplier,
            0.5,
            1.5,
        )
        bid_liquidity_factor = _clamp(
            min(
                bid_book_volume / max(self.history_config.min_side_volume_lk, 1),
                self.history_config.max_side_25pct_levels / max(bid_fraction_levels, 1.0),
            ),
            0.25,
            1.0,
        )
        ask_liquidity_factor = _clamp(
            min(
                ask_book_volume / max(self.history_config.min_side_volume_lk, 1),
                self.history_config.max_side_25pct_levels / max(ask_fraction_levels, 1.0),
            ),
            0.25,
            1.0,
        )
        bid_size_multiplier = _clamp(bid_size_multiplier * bid_liquidity_factor, 0.25, 2.0)
        ask_size_multiplier = _clamp(ask_size_multiplier * ask_liquidity_factor, 0.25, 2.0)
        one_sided_obi_soft_abs = _clamp(
            self.history_config.one_sided_obi_soft_abs,
            0.0,
            1.0,
        )
        one_sided_obi_gate_abs = _clamp(
            self.history_config.one_sided_obi_gate_abs,
            max(one_sided_obi_soft_abs + 1e-9, 0.0),
            1.0,
        )
        one_sided_obi_zscore = _clamp(
            abs(local_imbalance_zscore) / 3.0,
            0.0,
            1.0,
        )
        effective_bid_adverse_obi = max(
            -fast_maker_voi_pressure,
            one_sided_obi_zscore if local_imbalance_zscore < 0.0 else 0.0,
        )
        effective_ask_adverse_obi = max(
            fast_maker_voi_pressure,
            one_sided_obi_zscore if local_imbalance_zscore > 0.0 else 0.0,
        )
        one_sided_gate_ready = adaptive_state.updates_seen >= 3
        adverse_bid_pressure = (
            _ramp_unit(
                effective_bid_adverse_obi,
                one_sided_obi_soft_abs,
                one_sided_obi_gate_abs,
            )
            if one_sided_gate_ready
            else 0.0
        )
        adverse_ask_pressure = (
            _ramp_unit(
                effective_ask_adverse_obi,
                one_sided_obi_soft_abs,
                one_sided_obi_gate_abs,
            )
            if one_sided_gate_ready
            else 0.0
        )
        # Adverse pressure widens quotes to protect against toxic flow.
        # Size asymmetry is already handled by side_size_tilt above — applying it
        # again here from the same imbalance signal would double-suppress the same
        # side. Width-only response avoids that compounding.
        bid_width_multiplier = _clamp(
            bid_width_multiplier * (1.0 + 0.75 * adverse_bid_pressure),
            0.5,
            1.5,
        )
        ask_width_multiplier = _clamp(
            ask_width_multiplier * (1.0 + 0.75 * adverse_ask_pressure),
            0.5,
            1.5,
        )
        bid_enable_flag = bid_book_volume > 0
        ask_enable_flag = ask_book_volume > 0
        realized_sigma = adaptive_state.return_welford.std
        fill_support = _clamp(adaptive_state.ewma_fill_rate, 0.0, 2.0)
        cancel_pressure = _clamp(adaptive_state.ewma_cancel_rate, 0.0, 2.0)
        live_quote_age_ms = max(adaptive_state.ewma_quote_age_ms, 0.0)
        passive_fill_arrival_ms = max(
            adaptive_state.ewma_passive_fill_arrival_ms,
            0.0,
        )
        fill_arrival_speed_score = _clamp(
            self.history_config.fill_arrival_target_ms
            / max(passive_fill_arrival_ms, 1.0)
            if passive_fill_arrival_ms > 0.0
            else 0.0,
            0.0,
            1.5,
        )
        quote_age_mean_ms = (
            adaptive_state.quote_age_welford.mean
            if adaptive_state.initialized
            else live_quote_age_ms
        )
        quote_age_p90_ms = (
            adaptive_state.quote_age_p90.estimate
            if adaptive_state.initialized
            else live_quote_age_ms
        )
        spread_quality = _clamp(1.0 / max(smoothed_spread_ticks, 1.0), 0.25, 1.0)
        liquidity_score = _clamp(smoothed_depth / depth_baseline_lk, 0.5, 2.0)
        volatility_penalty = _clamp(realized_sigma * 100.0, 0.0, 1.0)
        session_elapsed_fraction = 0.0
        session_minutes_to_close = self.fallback.session_minutes_to_close
        session_length_minutes = self.fallback.session_minutes_to_close
        expected_trade_count = 0.0
        observed_trade_count = self._total_fill_count()
        trade_count_ratio = 1.0
        close_urgency_multiplier = 1.0
        flatten_only_mode = False
        session_open = True
        session_pace_multiplier = 1.0
        estimated_rebate, estimated_fee, passive_fill_ratio = self._symbol_fee_stats(state.symbol)
        estimated_net_fee = estimated_fee - estimated_rebate
        estimated_reserved_buying_power = self._reserved_buying_power()
        estimated_short_close_buying_power = self._short_close_buying_power()
        broker_available_buying_power = self._gross_buying_power()
        max_affordable_bid_flatten_lots = self._max_affordable_bid_flatten_lots(
            mark_price=state.mid_price,
        )
        local_shadow_buying_power = estimated_reserved_buying_power
        estimated_available_buying_power = max(
            broker_available_buying_power - local_shadow_buying_power,
            0.0,
        )
        total_effective_buying_power = (
            broker_available_buying_power
            + estimated_short_close_buying_power
            + local_shadow_buying_power
        )
        if total_effective_buying_power > 0.0:
            buying_power_usage_ratio = _clamp(
                1.0
                - estimated_available_buying_power / total_effective_buying_power,
                0.0,
                1.0,
            )
        else:
            buying_power_usage_ratio = 0.0
        buying_power_scale = _clamp(
            1.0 - 2.0 * max(buying_power_usage_ratio - 0.75, 0.0),
            0.2,
            1.0,
        )
        inventory_abs_lots = abs(inventory_lots)
        inventory_close_penalty = self._inventory_close_penalty(
            state.symbol,
            fallback_price=state.mid_price,
            inventory_abs_lots=inventory_abs_lots,
        )
        inventory_hedge_overlay = self.lead_lag_graph.inventory_overlay_for(
            state.symbol,
            inventory_lots_by_symbol=self._inventory_lots_by_symbol(),
        )
        inventory_pressure_score = _clamp(
            (
                inventory_abs_lots
                / max(self.fallback.inventory_limit_lots, 1)
            )
            * inventory_hedge_overlay.hedge_multiplier,
            0.0,
            1.0,
        )
        drift_inventory_bias_ticks = _clamp(
            0.5 * base_global_drift_shift_ticks * (1.0 - inventory_pressure_score),
            -0.5,
            0.5,
        )
        symbol_raw_fill_count = self._fill_count(state.symbol)
        avg_net_fee_per_fill = (
            estimated_net_fee / symbol_raw_fill_count
            if symbol_raw_fill_count > 0
            else 0.0
        )
        (
            queue_fill_support,
            queue_latency_haircut,
            bid_queue_ahead_lots,
            ask_queue_ahead_lots,
            bid_queue_share,
            ask_queue_share,
        ) = self._queue_fill_support(state, now_ns=now_ns)
        realized_symbol_pnl = self._realized_symbol_pnl(state.symbol)
        symbol_fill_count = max(symbol_raw_fill_count, 1)
        pnl_allocation_score = _clamp(
            0.5
            + realized_symbol_pnl
            / max(
                max(state.mid_price, 1.0)
                * 100.0
                * symbol_fill_count
                * 0.01,
                1.0,
            ),
            0.25,
            0.75,
        )
        execution_cost_score = _clamp(
            0.70 * passive_fill_ratio
            + 0.30 * (0.5 - avg_net_fee_per_fill / 0.30),
            0.0,
            1.0,
        )
        expected_passive_slippage_ticks = estimate_expected_passive_slippage_ticks(
            spread_ticks=smoothed_spread_ticks,
            toxicity_score=toxicity_score,
            queue_fill_support=queue_fill_support,
            liquidity_score=liquidity_score,
        )
        expected_aggressive_slippage_ticks = estimate_expected_aggressive_slippage_ticks(
            spread_ticks=smoothed_spread_ticks,
        )
        slippage_quality_score = _clamp(
            1.0
            + 0.20
            * (expected_aggressive_slippage_ticks - expected_passive_slippage_ticks),
            0.7,
            1.3,
        )
        maker_net_edge_ticks = (
            0.5 * smoothed_spread_ticks
            + LIMIT_REBATE_PER_SHARE / max(tick_size, 1e-9)
            - expected_passive_slippage_ticks
        )

        if session_progress is not None:
            session_elapsed_fraction = session_progress.elapsed_fraction
            session_minutes_to_close = session_progress.minutes_to_close
            session_length_minutes = session_progress.session_length_minutes
            expected_trade_count = session_progress.expected_trades_so_far
            flatten_only_mode = session_progress.flatten_only_mode
            session_open = session_progress.is_session_open
            if not session_progress.is_session_open and inventory_abs_lots > 0:
                flatten_only_mode = True
                session_open = True
            # Suppress quoting during warmup: either the session clock says we're
            # in the open warmup window, OR we haven't had warmup_minutes of live
            # data yet since system startup (handles mid-session start).
            warmup_ns = session_progress.warmup_minutes * 60 * 1_000_000_000
            startup_elapsed_ns = (
                now_ns - adaptive_state.first_update_ns
                if adaptive_state.first_update_ns > 0
                else 0
            )
            in_startup_warmup = startup_elapsed_ns < warmup_ns
            if session_progress.in_warmup or in_startup_warmup:
                session_open = False
            if expected_trade_count > 1e-9:
                trade_count_ratio = observed_trade_count / expected_trade_count
                session_pace_multiplier = _clamp(
                    1.0 + 0.75 * max(1.0 - trade_count_ratio, 0.0),
                    1.0,
                    1.75,
                )
            flatten_window = max(session_progress.flatten_window_minutes, 1)
            close_progress = _clamp(
                1.0 - session_minutes_to_close / flatten_window,
                0.0,
                1.0,
            )
            close_urgency_multiplier = 1.0 + 0.5 * close_progress

        safe_mode = self._safe_mode()
        if safe_mode == SafeMode.KILL_SWITCH.value:
            session_open = False
        elif safe_mode in {
            SafeMode.POSITION_MISMATCH.value,
            SafeMode.FLATTEN_ONLY.value,
        }:
            flatten_only_mode = True
            session_open = True

        # Session-open transition: zero stale sweep fill state from prior session.
        # A False→True transition means the market just opened; any sweep fills
        # carried over from yesterday are no longer valid exit candidates.
        if session_open and not adaptive_state.last_session_open:
            adaptive_state.sweep_fill_price = 0.0
            adaptive_state.sweep_fill_lots = 0
            adaptive_state.sweep_fill_side_sign = 0.0
            adaptive_state.sweep_pending_levels = []
        adaptive_state.last_session_open = session_open

        spread_floor_ticks = max(1, min(round(max(smoothed_spread_ticks * 0.5, 1.0)), self.fallback.spread_ceiling_ticks))
        spread_ceiling_ticks = max(
            spread_floor_ticks,
            min(
                self.fallback.spread_ceiling_ticks,
                max(spread_floor_ticks + 1, round(max(smoothed_spread_ticks * 1.5, spread_floor_ticks + 1))),
            ),
        )
        max_live_spread_ticks = max(spread_ceiling_ticks + 2, self.fallback.max_live_spread_ticks)
        max_live_spread_ticks = max(
            max_live_spread_ticks,
            int(round(spread_p90_ticks + 1.0)),
        )
        microprice_weight = _clamp(0.30 + 0.25 * smoothed_imbalance, 0.30, 0.75)
        # Base shift from depth-shape asymmetry only — smoothed_imbalance is
        # already embedded in drift_agreement_multiplier via local_pressure_signal,
        # so excluding it here prevents double-counting that was saturating the cap.
        imbalance_shift_ticks = _clamp(
            0.35 * (1.0 + 0.50 * abs(front_shape_imbalance))
            * drift_agreement_multiplier,
            0.25,
            1.0,
        )
        gamma_inventory = _clamp(
            (
                0.85
                + 0.2 * smoothed_imbalance
                + 1.0 * volatility_penalty
            )
            * close_urgency_multiplier,
            0.75,
            6.0,
        )
        gamma_inventory = _clamp(
            gamma_inventory
            * self.lookup_tables.inventory_pressure_gamma_multiplier.evaluate(
                inventory_pressure_score
            ),
            0.75,
            6.0,
        )
        allocation_weight = _clamp(
            spread_quality
            * min(liquidity_score, 1.5)
            * (0.85 + 0.15 * depth_concentration_score)
            * (1.0 + 0.15 * fill_support),
            0.10,
            3.00,
        )
        if safe_mode == SafeMode.DEGRADED_RECONCILE.value:
            allocation_weight = _clamp(
                allocation_weight * 0.80,
                0.10,
                3.00,
            )
        if not flatten_only_mode:
            allocation_weight = _clamp(
                allocation_weight
                * (0.80 + 0.40 * execution_cost_score)
                * (0.75 + 0.50 * pnl_allocation_score)
                * slippage_quality_score
                * buying_power_scale,
                0.10,
                3.00,
            )
        pace_multiplier = _clamp(
            (
                1.0
                + 0.25 * max(liquidity_score - 1.0, 0.0)
            )
            * session_pace_multiplier,
            1.0,
            1.75,
        )
        if not flatten_only_mode:
            pace_multiplier = _clamp(
                pace_multiplier * (0.85 + 0.30 * execution_cost_score),
                1.0,
                1.75,
            )
        # Size is now bounded by configured inventory/risk caps instead of an
        # unrelated hardcoded 5-lot ceiling.
        quote_size_lots = max(
            1,
            min(
                int(self.fallback.quote_size_lots),
                max(int(self.fallback.inventory_limit_lots), 1),
            ),
        )
        # Start from the configured ladder depth and trim only when market
        # conditions are hostile enough that deeper passive levels are likely
        # stale or toxic. This avoids a hidden "upgrade gate" that silently
        # collapses normal two-level quoting to one level.
        passive_ladder_depth_levels = max(
            int(self.fallback.passive_ladder_depth_levels),
            1,
        )
        if (
            flatten_only_mode
            or toxicity_score >= 0.65
            or liquidity_score <= 0.70
            or depth_concentration_score <= 0.15
        ):
            passive_ladder_depth_levels = 1
        passive_fill_probability = _clamp(
            0.15
            + 0.10 * liquidity_score
            + 0.10 * spread_quality
            + 0.35 * queue_fill_support
            + 0.10 * fill_support
            + 0.15 * fill_arrival_speed_score
            + 0.05 * passive_fill_ratio,
            0.1,
            0.9,
        )
        sigma_realized = max(
            realized_sigma,
            0.5 * state.spread / max(state.mid_price, tick_size),
        )
        cj_glft_params = estimate_cj_glft_parameters(
            smoothed_spread_ticks=smoothed_spread_ticks,
            sigma_realized=sigma_realized,
            mid_price=state.mid_price,
            tick_size=tick_size,
            gamma_inventory=gamma_inventory,
            session_minutes_to_close=session_minutes_to_close,
            session_length_minutes=session_length_minutes,
            passive_fill_probability=passive_fill_probability,
            queue_fill_support=queue_fill_support,
            liquidity_score=liquidity_score,
            maker_net_edge_ticks=maker_net_edge_ticks,
            min_half_width_ticks=spread_floor_ticks,
            max_half_width_ticks=spread_ceiling_ticks,
            inventory_skew_cap_ticks=self.fallback.inventory_skew_cap_ticks,
        )
        quote_age_limit_ms = max(
            100,
            int(
                self.fallback.quote_age_limit_ms
                * _clamp(
                    1.0 + 0.1 * liquidity_score,
                    0.35,
                    1.5,
                )
            ),
        )
        quote_age_limit_ms = max(
            quote_age_limit_ms,
            int(round(1.25 * quote_age_p90_ms)),
        )
        taker_overlay_mode = False
        taker_overlay_size_lots = 0
        taker_overlay_edge_ticks = 0.0
        overlay_cooldown_ready = (
            adaptive_state.updates_seen
            - adaptive_state.last_taker_overlay_update_seen
            >= max(self.history_config.taker_overlay_cooldown_updates, 0)
        )
        if (
            session_open
            and not flatten_only_mode
            and inventory_abs_lots > 0
            and overlay_cooldown_ready
            and toxicity_score <= self.history_config.taker_overlay_max_toxicity
        ):
            if inventory_lots > 0:
                # Long: adverse pressure = selling flow or downward drift
                adverse_pressure = _clamp(
                    -0.70 * slow_trade_voi_pressure
                    - 0.30 * base_global_drift_shift_ticks,
                    0.0,
                    1.0,
                )
                exit_queue_share = ask_queue_share
                max_overlay_size = inventory_abs_lots
            else:
                # Short: adverse pressure = buying flow or upward drift
                adverse_pressure = _clamp(
                    0.70 * slow_trade_voi_pressure
                    + 0.30 * base_global_drift_shift_ticks,
                    0.0,
                    1.0,
                )
                exit_queue_share = bid_queue_share
                max_overlay_size = min(
                    inventory_abs_lots,
                    max(max_affordable_bid_flatten_lots, 0),
                )
            # Edge ticks kept for logging and sleeve credit — not used as a gate.
            taker_overlay_edge_ticks = max(
                adverse_pressure - expected_aggressive_slippage_ticks,
                0.0,
            )
            if (
                exit_queue_share <= self.history_config.taker_overlay_max_queue_share
                and max_overlay_size > 0
            ):
                # Almgren-Chriss-style continuous urgency: always participate at a
                # base rate (inventory / horizon), scaled up by how adverse conditions
                # are. No pressure threshold gate — urgency drives the size, not a
                # binary on/off. This exits earlier in adverse moves rather than
                # waiting until pressure is already extreme.
                horizon = max(self.history_config.taker_exit_horizon_cycles, 1)
                urgency = 1.0 + self.history_config.taker_exit_urgency_scale * adverse_pressure
                raw_size = (inventory_abs_lots / horizon) * urgency
                max_per_cycle = self.history_config.taker_exit_max_lots_per_cycle
                taker_overlay_size_lots = min(
                    max(int(round(raw_size)), 1),
                    min(max_overlay_size, max_per_cycle),
                )
                taker_overlay_mode = True
                adaptive_state.last_taker_overlay_update_seen = (
                    adaptive_state.updates_seen
                )

        # --- Physics-informed momentum ---
        fast_velocity_ticks = (
            adaptive_state.ewma_fast_velocity_ticks if adaptive_state.initialized else 0.0
        )
        slow_velocity_ticks = (
            adaptive_state.ewma_slow_velocity_ticks if adaptive_state.initialized else 0.0
        )
        queue_velocity = (
            adaptive_state.ewma_queue_velocity if adaptive_state.initialized else 0.0
        )
        liq_mass = adaptive_state.liquidity_mass_norm
        imp_mass = adaptive_state.impact_mass_norm

        # Physics momentum: only the slow, confidence-gated RLS prediction.
        # fast_velocity_ticks (raw last-move) was removed — it's one-shot noise that
        # chases price and creates a positive-feedback loop: price moves → center chases
        # → aggressive fill → inventory deepens → more chasing.  The RLS slow signal
        # already captures genuine drift; adding the fast raw move double-counts it.
        stable_momentum = _clamp(slow_velocity_ticks * liq_mass, -1.5, 1.5)

        # Queue confirmation gate: [0.5, 1.0] scalar.
        # When queue_velocity agrees → pass; when it disagrees → half-attenuate.
        queue_gate = _clamp(
            0.5 + 0.5 * queue_velocity * (1.0 if stable_momentum >= 0.0 else -1.0),
            0.5,
            1.0,
        )
        raw_momentum_shift = stable_momentum * queue_gate

        # Hard cap before inventory suppression
        momentum_max = self.history_config.momentum_max_shift_ticks
        physics_momentum_shift_ticks = _clamp(raw_momentum_shift, -momentum_max, momentum_max)

        # Direction-aware inventory suppression: suppress momentum that would
        # deepen the inventory position; allow momentum that helps exit it.
        if inventory_lots != 0:
            inventory_sign = 1.0 if inventory_lots > 0 else -1.0
            momentum_deepens_position = inventory_sign * physics_momentum_shift_ticks > 0
            if momentum_deepens_position:
                physics_momentum_shift_ticks = _clamp(
                    physics_momentum_shift_ticks * (1.0 - 0.6 * inventory_pressure_score),
                    -momentum_max,
                    momentum_max,
                )

        # Absorb any fills that came back for the pending sweep order.
        # Must happen before the sweep overlay logic so sweep_fill_lots is current.
        self._absorb_sweep_fills(state.symbol, adaptive_state)

        # --- Sweep-reversion overlay ---
        # Strategy: price sweeps away from a short MA (~α=0.35, half-life ~2 updates)
        # then snaps back to it.  On sweep detection we front-load a passive limit
        # order AT the short MA, penny-jumping existing orders there so we get
        # queue priority and fill on the snap-back.
        #
        # The long MA (~α=0.08) is the slow drift baseline.  It biases which side
        # we prefer: if the long MA is rising, down-sweeps are more likely to revert
        # (drift supports them), so we preferentially front-load bids on dips.
        #
        # Momentum suppression: when mid is IN a sweep (away from short MA),
        # physics_momentum_shift is chasing noise.  Suppress it proportionally.

        # Pull the two MA prices from adaptive state
        sweep_short_ma = adaptive_state.mr_ema_fast_price
        sweep_long_ma = adaptive_state.mr_ema_slow_price
        mr_error_fast_ticks = adaptive_state.mr_error_fast_ticks
        mr_error_medium_ticks = adaptive_state.mr_error_medium_ticks
        mr_error_slow_ticks = adaptive_state.mr_error_slow_ticks
        mr_reversion_quality_fast = adaptive_state.mr_reversion_quality_fast
        mr_reversion_quality_medium = adaptive_state.mr_reversion_quality_medium
        mr_reversion_quality_slow = adaptive_state.mr_reversion_quality_slow
        mr_crossed_fast = adaptive_state.mr_crossed_fast
        mr_crossed_medium = adaptive_state.mr_crossed_medium
        mr_crossed_slow = adaptive_state.mr_crossed_slow
        mr_cross_direction = adaptive_state.mr_cross_direction
        mr_cross_depth = adaptive_state.mr_cross_depth
        mr_cross_anchor_price = adaptive_state.mr_cross_anchor_price
        sweep_cross_direction = adaptive_state.sweep_cross_direction
        sweep_cross_depth = adaptive_state.sweep_cross_depth
        sweep_cross_anchor_tau_s = adaptive_state.sweep_cross_anchor_tau_s
        sweep_cross_anchor_price = adaptive_state.sweep_cross_anchor_price
        return_zscore = (
            adaptive_state.ewma_return_zscore if adaptive_state.initialized else 0.0
        )
        if sweep_short_ma <= 0.0:
            sweep_short_ma = adaptive_state.ewma_sweep_short_ma or state.mid_price
        if sweep_long_ma <= 0.0:
            sweep_long_ma = adaptive_state.ewma_sweep_long_ma or state.mid_price

        # Sweep deviation now comes from the fast 1m residual around the time-based
        # EMA. Medium and slow residuals contribute to the passive center shift
        # below, but the fast residual remains the trigger for snapback entries.
        sweep_deviation_ticks = mr_error_fast_ticks
        if sweep_deviation_ticks == 0.0 and sweep_short_ma > 0.0:
            sweep_deviation_ticks = (
                state.mid_price - sweep_short_ma
            ) / max(tick_size, 1e-9)

        abs_sweep_ticks = abs(sweep_deviation_ticks)
        sweep_thresh = self.history_config.sweep_threshold_ticks
        # sweep_magnitude_score: how far price has moved from the short MA, normalised.
        # Ramps from 0 at threshold to 1 at threshold+2 ticks.  Not RLS-gated here —
        # RLS confidence only suppresses momentum, not sweep detection.
        sweep_magnitude_score = _ramp_unit(abs_sweep_ticks, sweep_thresh, sweep_thresh + 2.0)
        # mean_reversion_score: sweep magnitude gated by RLS confidence.
        # High rls_confidence → likely a real trend → suppress reversion.
        # Used only for momentum suppression and center bias, NOT for sweep_detected.
        residual_reversion_quality = _clamp(
            0.50 * mr_reversion_quality_fast
            + 0.30 * mr_reversion_quality_medium
            + 0.20 * mr_reversion_quality_slow,
            0.0,
            1.0,
        )
        sweep_direction = (
            1
            if sweep_deviation_ticks > 0.0
            else (-1 if sweep_deviation_ticks < 0.0 else 0)
        )
        cross_signature_detected = (
            sweep_cross_depth > 0
            and sweep_cross_direction != 0
            and (
                sweep_direction == 0
                or sweep_cross_direction == sweep_direction
            )
            and residual_reversion_quality >= 0.55
        )
        sweep_entry_ma_price = (
            sweep_cross_anchor_price
            if cross_signature_detected and sweep_cross_anchor_price > 0.0
            else sweep_short_ma
        )
        mean_reversion_score = _clamp(
            max(
                sweep_magnitude_score,
                0.35 * sweep_cross_depth * float(cross_signature_detected),
            )
            * (1.0 - rls_confidence)
            * _ramp_unit(residual_reversion_quality, 0.45, 0.70),
            0.0,
            1.0,
        )

        # Momentum suppression: when momentum is chasing the sweep direction,
        # fade it back toward zero proportionally to mean_reversion_score.
        if mean_reversion_score > 0.0:
            fade = self.history_config.mean_reversion_momentum_fade
            fade_factor = 1.0 - fade * mean_reversion_score
            sweep_sign = 1.0 if sweep_deviation_ticks > 0.0 else -1.0
            if physics_momentum_shift_ticks * sweep_sign > 0:
                physics_momentum_shift_ticks = _clamp(
                    physics_momentum_shift_ticks * fade_factor,
                    -momentum_max,
                    momentum_max,
                )

        # Passive center bias: shift the maker quotes slightly toward the short MA
        # so fills happen at better prices even in normal maker mode.
        # Negative sweep_deviation → price below MA → shift center up (toward MA).
        mean_reversion_residual_ticks = (
            0.50 * mr_reversion_quality_fast * mr_error_fast_ticks
            + 0.30 * mr_reversion_quality_medium * mr_error_medium_ticks
            + 0.20 * mr_reversion_quality_slow * mr_error_slow_ticks
        )
        noise_fade_shift_ticks = _clamp(
            -0.35 * mean_reversion_residual_ticks * mean_reversion_score,
            -1.0,
            1.0,
        )

        # Long MA drift bias: if long MA > short MA, drift is downward from
        # the longer baseline — prefer bids (buying the dip is with the drift).
        # If long MA < short MA, drift is upward — prefer asks.
        long_ma_drift_ticks = (sweep_long_ma - sweep_short_ma) / max(tick_size, 1e-9)
        drift_bias_ticks = _clamp(
            long_ma_drift_ticks * self.history_config.sweep_drift_bias_ticks,
            -self.history_config.sweep_drift_bias_ticks,
            self.history_config.sweep_drift_bias_ticks,
        )

        # --- Standing sweep-reversion passive ladder ---
        # Orders sit in the book BEFORE any spike happens — no reactive detection.
        # Anchor: short MA (the mean price reverts to).
        # Bid levels fan BELOW the short MA; ask levels fan ABOVE.
        # Level 0 is at short_MA - min_edge_ticks, level 1 at -min_edge-1, etc.
        # Size grows with depth: level i gets (i+1) lots — more size at better edge.
        #
        # Long MA direction gates which side is live:
        #   long_MA > short_MA (slow drift up) → bids only (buy dips, drift supports)
        #   long_MA < short_MA (slow drift down) → asks only (sell spikes)
        #   long_MA ≈ short_MA (flat) → both sides active
        #
        # Inventory cap gates each side independently so we never exceed the symbol
        # position limit regardless of how many sweeps fire consecutively.
        sweep_reversion_mode = False
        sweep_reversion_size_lots = 0
        sweep_detected = sweep_magnitude_score > 0.0 or cross_signature_detected
        sweep_reversion_bid_levels: list[tuple[float, int]] = []
        sweep_reversion_ask_levels: list[tuple[float, int]] = []
        current_book_age_ms = (
            max((now_ns - state.last_book_update_ns) / 1_000_000, 0.0)
            if state.last_book_update_ns > 0 else 9999.0
        )
        if (
            session_open
            and not flatten_only_mode
            and toxicity_score <= self.history_config.sweep_max_toxicity
            and current_book_age_ms <= self.history_config.sweep_max_book_age_ms
            and adaptive_state.updates_seen >= 6
            and sweep_short_ma > 0.0
        ):
            min_edge = self.history_config.sweep_min_edge_ticks
            max_levels = max(self.history_config.sweep_ladder_depth_levels, 1)
            sweep_inv_cap = self.fallback.inventory_limit_lots

            # Long MA drift score: positive = drift up, negative = drift down.
            # Threshold of 0.33 ≈ 1 tick of drift, suppresses noise.
            long_ma_drift_score_raw = _clamp(
                long_ma_drift_ticks / max(3.0, 1e-9), -1.0, 1.0
            )
            # Bid side active unless drift is strongly downward
            bids_allowed_by_drift = long_ma_drift_score_raw >= -0.33
            # Ask side active unless drift is strongly upward
            asks_allowed_by_drift = long_ma_drift_score_raw <= 0.33
            # Inventory cap: don't add more longs if already at limit
            bids_blocked_by_inv = inventory_lots >= sweep_inv_cap
            # Inventory cap: don't add more shorts if already at limit
            asks_blocked_by_inv = inventory_lots <= -sweep_inv_cap

            # Bid ladder: levels below the short MA, fanning into potential dips.
            if bids_allowed_by_drift and not bids_blocked_by_inv:
                new_pending_bids: list[tuple[float, float, int]] = []
                for lvl in range(max_levels):
                    # Level 0 at short_MA - min_edge, level 1 one tick lower, etc.
                    offset = (min_edge + lvl) * tick_size
                    level_px = _round_to_tick(sweep_short_ma - offset, tick_size)
                    if level_px <= 0.0:
                        break
                    # Must be strictly below best_bid to be a resting passive order.
                    if level_px >= state.best_price.best_bid_px:
                        continue
                    # Size grows with depth: level 0 = 1 lot, level 1 = 2 lots, etc.
                    level_size = lvl + 1
                    sweep_reversion_bid_levels.append((level_px, level_size))
                    new_pending_bids.append((level_px, 1.0, 0))
                if sweep_reversion_bid_levels:
                    sweep_reversion_mode = True
                    adaptive_state.sweep_pending_levels = _merge_sweep_pending_levels(
                        adaptive_state.sweep_pending_levels,
                        new_pending_bids,
                    )

            # Ask ladder: levels above the short MA, fanning into potential spikes.
            if asks_allowed_by_drift and not asks_blocked_by_inv:
                new_pending_asks: list[tuple[float, float, int]] = []
                for lvl in range(max_levels):
                    offset = (min_edge + lvl) * tick_size
                    level_px = _round_to_tick(sweep_short_ma + offset, tick_size)
                    # Must be strictly above best_ask to be a resting passive order.
                    if level_px <= state.best_price.best_ask_px:
                        continue
                    level_size = lvl + 1
                    sweep_reversion_ask_levels.append((level_px, level_size))
                    new_pending_asks.append((level_px, -1.0, 0))
                if sweep_reversion_ask_levels:
                    sweep_reversion_mode = True
                    adaptive_state.sweep_pending_levels = _merge_sweep_pending_levels(
                        adaptive_state.sweep_pending_levels,
                        new_pending_asks,
                    )

            if sweep_reversion_mode:
                sweep_reversion_size_lots = sum(
                    sz for _, sz in sweep_reversion_bid_levels + sweep_reversion_ask_levels
                )

        # --- Sweep exit: unload accumulated sweep position when price returns to mass ---
        # Two equivalent exit conditions:
        #   1. "price reverted"  — mid_price is within exit_threshold of sweep_short_ma
        #   2. "ma came to price" — sweep_short_ma itself has tracked to the fill price
        #                           (mass moved to where price was; same net result)
        # Both are treated identically: unload all sweep_fill_lots at market center.
        sweep_exit_mode = False
        sweep_exit_lots = 0
        sweep_exit_side_sign = 0.0
        sweep_exit_trigger = ""
        sweep_exit_fill_price = 0.0
        if adaptive_state.sweep_fill_lots != 0 and sweep_short_ma > 0.0:
            exit_thresh = self.history_config.sweep_exit_threshold_ticks * tick_size
            fill_px = adaptive_state.sweep_fill_price
            # Only exit if the fill was genuinely far from the mass — we need at
            # least min_edge ticks of expected reversion P&L to have been captured.
            # A fill at the mass itself means the overlay misfired; skip.
            fill_was_in_sweep = (
                fill_px > 0.0
                and abs(fill_px - sweep_short_ma) >= self.history_config.sweep_min_edge_ticks * tick_size
            )
            if fill_was_in_sweep:
                # Condition 1: price has returned to within exit_thresh of short MA
                price_reverted = abs(state.mid_price - sweep_short_ma) <= exit_thresh
                # Condition 2: the short MA has itself tracked to within exit_thresh
                # of the fill price (mass moved to where price was — zero out).
                ma_came_to_price = abs(sweep_short_ma - fill_px) <= exit_thresh
                if price_reverted or ma_came_to_price:
                    sweep_exit_mode = True
                    sweep_exit_lots = abs(adaptive_state.sweep_fill_lots)
                    # To exit a long (bought dip), we sell. To exit a short (sold spike), we buy.
                    sweep_exit_side_sign = -adaptive_state.sweep_fill_side_sign
                    sweep_exit_trigger = "price_reverted" if price_reverted else "ma_came_to_price"
                    sweep_exit_fill_price = fill_px
                    # Clear the tracked position — pipeline will unload it this cycle.
                    adaptive_state.sweep_fill_price = 0.0
                    adaptive_state.sweep_fill_lots = 0
                    adaptive_state.sweep_fill_side_sign = 0.0

        # --- Long-MA inventory target ---
        # Front-run the drift direction by holding a small inventory bias toward
        # the long MA.  When long MA > short MA, the slow trend is up — prefer longs.
        # Capped at 1 lot to avoid large unhedged exposure.
        inventory_target_lots = 0
        if sweep_long_ma > 0.0 and sweep_short_ma > 0.0:
            long_ma_drift = sweep_long_ma - sweep_short_ma
            long_ma_drift_score = _clamp(
                long_ma_drift / max(tick_size * 3.0, 1e-9),
                -1.0,
                1.0,
            )
            # Only bias when drift is meaningful (|score| >= 0.33 ≈ 1 tick of drift)
            # and not in flatten mode
            if not flatten_only_mode and abs(long_ma_drift_score) >= 0.33:
                inventory_target_lots = 1 if long_ma_drift_score > 0.0 else -1

        noise_fade_taker_mode = sweep_reversion_mode
        noise_fade_taker_size_lots = sweep_reversion_size_lots
        noise_fade_ma_price = sweep_entry_ma_price
        # Best (closest to mass) level price for backward-compatible single-price fields
        noise_fade_bid_px = sweep_reversion_bid_levels[0][0] if sweep_reversion_bid_levels else 0.0
        noise_fade_ask_px = sweep_reversion_ask_levels[0][0] if sweep_reversion_ask_levels else 0.0

        sleeve_signal_scores = {
            "BASE_MM": _clamp(
                0.25 * execution_cost_score
                + 0.20 * queue_fill_support
                + 0.20 * entropy_confidence_score
                - 0.15 * toxicity_score,
                -1.0,
                1.0,
            ),
            "LEAD_LAG": _clamp(
                abs(lead_lag_overlay.shift_ticks)
                * lead_lag_overlay.confidence,
                0.0,
                1.0,
            ),
            "PAIR_SPREAD": _clamp(
                abs(pair_spread_overlay.shift_ticks)
                * pair_spread_overlay.confidence,
                0.0,
                1.0,
            ),
            "INV_HEDGE": _clamp(
                (1.0 - inventory_hedge_overlay.hedge_multiplier)
                * inventory_hedge_overlay.hedge_confidence,
                0.0,
                1.0,
            ),
            "TAKER_EXIT": _clamp(
                taker_overlay_edge_ticks
                * float(taker_overlay_size_lots > 0),
                0.0,
                1.0,
            ),
            "NOISE_FADE": _clamp(
                abs(noise_fade_shift_ticks) * mean_reversion_score
                + 0.25 * float(noise_fade_taker_mode),
                0.0,
                1.0,
            ),
            "CASH": 0.0,  # do-nothing; stable zero return, zero variance
        }
        pnl_delta = realized_symbol_pnl - adaptive_state.last_realized_symbol_pnl
        sleeve_rewards = _sleeve_rewards_from_realized_pnl(
            pnl_delta=pnl_delta,
            previous_credit_by_name=adaptive_state.last_sleeve_credit_by_name,
            mid_price=state.mid_price,
        )
        adaptive_state.last_realized_symbol_pnl = realized_symbol_pnl

        sleeve_allocator = self._sleeve_allocator_for_symbol(state.symbol)
        sleeve_weights = sleeve_allocator.update(
            sleeve_signal_scores,
            reward_multiplier=_clamp(
                0.60
                + 0.40 * execution_cost_score
                + 0.40 * (pnl_allocation_score - 0.5)
                - 0.25 * toxicity_score,
                0.25,
                1.75,
            ),
            sleeve_rewards=sleeve_rewards,
        )
        base_mm_scale = _clamp(sleeve_scale(sleeve_weights.base_mm), 0.0, 2.0)
        lead_lag_scale = sleeve_scale(sleeve_weights.lead_lag)
        pair_spread_scale = sleeve_scale(sleeve_weights.pair_spread)
        inv_hedge_scale = sleeve_scale(sleeve_weights.inv_hedge)
        taker_exit_scale = sleeve_scale(sleeve_weights.taker_exit)
        noise_fade_scale = sleeve_scale(sleeve_weights.noise_fade)

        probe_sleeve = _exploration_probe_sleeve(adaptive_state.updates_seen)
        if probe_sleeve == "LEAD_LAG" and abs(lead_lag_overlay.shift_ticks) > 0.0:
            lead_lag_scale = max(lead_lag_scale, 0.85)
        elif probe_sleeve == "PAIR_SPREAD" and abs(pair_spread_overlay.shift_ticks) > 0.0:
            pair_spread_scale = max(pair_spread_scale, 0.85)
        elif (
            probe_sleeve == "INV_HEDGE"
            and inventory_hedge_overlay.hedge_confidence > 0.0
            and inventory_hedge_overlay.hedge_multiplier < 1.0
        ):
            inv_hedge_scale = max(inv_hedge_scale, 0.85)
        elif probe_sleeve == "TAKER_EXIT" and taker_overlay_size_lots > 0:
            taker_exit_scale = max(taker_exit_scale, 0.85)
        elif (
            probe_sleeve == "NOISE_FADE"
            and (abs(noise_fade_shift_ticks) > 0.0 or noise_fade_taker_mode)
        ):
            noise_fade_scale = max(noise_fade_scale, 0.85)

        allocation_weight = _clamp(
            allocation_weight * base_mm_scale,
            0.10,
            3.00,
        )
        lead_lag_shift_ticks = _clamp(
            lead_lag_overlay.shift_ticks * lead_lag_scale,
            -2.0,
            2.0,
        )
        if probe_sleeve == "LEAD_LAG" and abs(lead_lag_overlay.shift_ticks) > 0.0:
            lead_lag_shift_ticks = _signed_min_abs(
                lead_lag_shift_ticks,
                lead_lag_overlay.shift_ticks,
                0.50,
            )
        pair_spread_shift_ticks = _clamp(
            pair_spread_overlay.shift_ticks * pair_spread_scale,
            -2.0,
            2.0,
        )
        if probe_sleeve == "PAIR_SPREAD" and abs(pair_spread_overlay.shift_ticks) > 0.0:
            pair_spread_shift_ticks = _signed_min_abs(
                pair_spread_shift_ticks,
                pair_spread_overlay.shift_ticks,
                0.50,
            )
        # Final drift signal: blend base components + sleeve-scaled lead-lag.
        _ll_w = 0.20 * lead_lag_overlay.confidence
        _base_w = 1.0 - _ll_w
        global_drift_shift_ticks = _clamp(
            _base_w * drift_components_ticks + _ll_w * lead_lag_shift_ticks,
            -2.0,
            2.0,
        )
        inventory_hedge_multiplier = _clamp(
            1.0
            - (1.0 - inventory_hedge_overlay.hedge_multiplier) * inv_hedge_scale,
            0.0,
            1.0,
        )
        cj_inventory_skew_ticks_per_lot = _clamp(
            cj_glft_params.cj_inventory_skew_ticks_per_lot
            * inventory_hedge_multiplier,
            0.0,
            self.fallback.inventory_skew_cap_ticks,
        )
        if taker_overlay_mode:
            taker_overlay_edge_ticks = _clamp(
                taker_overlay_edge_ticks * taker_exit_scale,
                0.0,
                2.0,
            )
            # Scale size proportionally to sleeve weight and suppress only when
            # the sleeve is nearly fully de-weighted by the allocator.
            taker_overlay_size_lots = max(
                int(round(taker_overlay_size_lots * taker_exit_scale)),
                1,
            )
            if taker_exit_scale < 0.1:
                taker_overlay_mode = False
                taker_overlay_size_lots = 0
        noise_fade_shift_ticks = _clamp(
            noise_fade_shift_ticks * noise_fade_scale,
            -1.0,
            1.0,
        )
        if probe_sleeve == "NOISE_FADE" and abs(noise_fade_shift_ticks) > 0.0:
            noise_fade_shift_ticks = _signed_min_abs(
                noise_fade_shift_ticks,
                noise_fade_shift_ticks,
                0.50,
            )
        if noise_fade_taker_mode and noise_fade_scale < 0.5:
            noise_fade_taker_mode = False
            noise_fade_taker_size_lots = 0

        adaptive_state.last_sleeve_credit_by_name = _normalize_sleeve_credit(
            {
                "BASE_MM": (
                    sleeve_weights.base_mm
                    * max(
                        sleeve_signal_scores["BASE_MM"],
                        0.10 * float(not flatten_only_mode),
                    )
                ),
                "LEAD_LAG": (
                    sleeve_weights.lead_lag * abs(lead_lag_shift_ticks)
                ),
                "PAIR_SPREAD": (
                    sleeve_weights.pair_spread * abs(pair_spread_shift_ticks)
                ),
                "INV_HEDGE": (
                    sleeve_weights.inv_hedge
                    * abs(1.0 - inventory_hedge_multiplier)
                ),
                "TAKER_EXIT": (
                    sleeve_weights.taker_exit
                    * float(taker_overlay_mode and taker_overlay_size_lots > 0)
                    * max(taker_overlay_edge_ticks, 0.10)
                ),
                "NOISE_FADE": (
                    sleeve_weights.noise_fade
                    * (
                        abs(noise_fade_shift_ticks)
                        + 0.50
                        * float(
                            noise_fade_taker_mode
                            and noise_fade_taker_size_lots > 0
                        )
                    )
                ),
                "CASH": 0.0,  # cash sleeve never earns activity credit
            }
        )
        current_sleeve_credit = adaptive_state.last_sleeve_credit_by_name

        return SymbolStrategyParameters(
            gamma_inventory=gamma_inventory,
            quote_size_lots=quote_size_lots,
            passive_ladder_depth_levels=passive_ladder_depth_levels,
            bid_size_multiplier=bid_size_multiplier,
            ask_size_multiplier=ask_size_multiplier,
            bid_width_multiplier=bid_width_multiplier,
            ask_width_multiplier=ask_width_multiplier,
            spread_floor_ticks=spread_floor_ticks,
            spread_ceiling_ticks=spread_ceiling_ticks,
            max_live_spread_ticks=max_live_spread_ticks,
            quote_age_limit_ms=quote_age_limit_ms,
            microprice_weight=microprice_weight,
            imbalance_shift_ticks=imbalance_shift_ticks,
            inventory_skew_cap_ticks=self.fallback.inventory_skew_cap_ticks,
            inventory_limit_lots=self.fallback.inventory_limit_lots,
            inventory_target_lots=inventory_target_lots,
            symbol_enable_flag=session_open,
            bid_enable_flag=bid_enable_flag,
            ask_enable_flag=ask_enable_flag,
            allocation_weight=allocation_weight,
            pace_multiplier=pace_multiplier,
            sigma_realized=sigma_realized,
            passive_fill_probability=passive_fill_probability,
            passive_fill_arrival_ms=passive_fill_arrival_ms,
            fill_arrival_speed_score=fill_arrival_speed_score,
            live_quote_age_ms=live_quote_age_ms,
            recent_fill_rate=adaptive_state.ewma_fill_rate,
            recent_cancel_rate=adaptive_state.ewma_cancel_rate,
            bid_book_volume_lk=bid_book_volume,
            ask_book_volume_lk=ask_book_volume,
            depth_baseline_lk=depth_baseline_lk,
            depth_zscore=depth_zscore,
            bid_25pct_levels=bid_fraction_levels,
            ask_25pct_levels=ask_fraction_levels,
            depth_concentration_score=depth_concentration_score,
            front_shape_imbalance=front_shape_imbalance,
            front_shape_shift_ticks=front_shape_shift_ticks,
            side_size_tilt=side_size_tilt,
            side_width_tilt=side_width_tilt,
            session_elapsed_fraction=session_elapsed_fraction,
            session_minutes_to_close=session_minutes_to_close,
            expected_trade_count=expected_trade_count,
            observed_trade_count=observed_trade_count,
            trade_count_ratio=trade_count_ratio,
            close_urgency_multiplier=close_urgency_multiplier,
            flatten_only_mode=flatten_only_mode,
            safe_mode=safe_mode,
            spread_mean_ticks=smoothed_spread_ticks,
            spread_std_ticks=spread_std_ticks,
            spread_p90_ticks=spread_p90_ticks,
            spread_zscore=spread_zscore,
            local_imbalance_zscore=local_imbalance_zscore,
            one_sided_obi_zscore=one_sided_obi_zscore,
            global_drift_zscore=global_drift_zscore,
            spread_regime_signal=spread_regime_signal,
            global_drift_regime_signal=global_drift_regime_signal,
            quote_age_mean_ms=quote_age_mean_ms,
            quote_age_p90_ms=quote_age_p90_ms,
            estimated_rebate=estimated_rebate,
            estimated_fee=estimated_fee,
            estimated_net_fee=estimated_net_fee,
            passive_fill_ratio=passive_fill_ratio,
            avg_net_fee_per_fill=avg_net_fee_per_fill,
            execution_cost_score=execution_cost_score,
            expected_passive_slippage_ticks=expected_passive_slippage_ticks,
            expected_aggressive_slippage_ticks=expected_aggressive_slippage_ticks,
            maker_net_edge_ticks=cj_glft_params.maker_net_edge_ticks,
            maker_edge_intensity_factor=cj_glft_params.maker_edge_intensity_factor,
            slippage_quality_score=slippage_quality_score,
            queue_fill_support=queue_fill_support,
            queue_latency_haircut=queue_latency_haircut,
            bid_queue_ahead_lots=bid_queue_ahead_lots,
            ask_queue_ahead_lots=ask_queue_ahead_lots,
            bid_queue_share=bid_queue_share,
            ask_queue_share=ask_queue_share,
            local_microprice=state.local_microprice,
            local_depth_imbalance=imbalance_now,
            local_multi_level_voi=smoothed_local_voi,
            global_l1_imbalance=smoothed_global_imbalance,
            global_l1_voi=smoothed_global_voi,
            global_mid_drift=smoothed_global_drift,
            rls_drift_prediction_ticks=rls_drift_prediction_ticks,
            rls_confidence=rls_confidence,
            rls_cosine_similarity=rls_cosine_similarity,
            rls_prediction_variance=rls_prediction_variance,
            lead_lag_shift_ticks=lead_lag_shift_ticks,
            lead_lag_confidence=lead_lag_overlay.confidence,
            lead_lag_edge_score=lead_lag_overlay.edge_score,
            lead_lag_leader_symbol=lead_lag_overlay.leader_symbol,
            pair_spread_shift_ticks=pair_spread_shift_ticks,
            pair_spread_confidence=pair_spread_overlay.confidence,
            pair_spread_residual_ticks=pair_spread_overlay.residual_ticks,
            pair_spread_peer_symbol=pair_spread_overlay.peer_symbol,
            inventory_hedge_multiplier=inventory_hedge_multiplier,
            inventory_hedge_confidence=inventory_hedge_overlay.hedge_confidence,
            inventory_hedge_ratio=inventory_hedge_overlay.hedge_ratio,
            inventory_hedge_symbol=inventory_hedge_overlay.hedge_symbol,
            trade_signed_volume=smoothed_trade_signed_volume,
            trade_linked_voi_score=smoothed_trade_linked_voi,
            depth_entropy=depth_entropy,
            flow_entropy=flow_entropy,
            entropy_confidence_score=entropy_confidence_score,
            fast_maker_voi_pressure=fast_maker_voi_pressure,
            slow_trade_voi_pressure=slow_trade_voi_pressure,
            taker_overlay_mode=taker_overlay_mode,
            taker_overlay_size_lots=taker_overlay_size_lots,
            taker_overlay_edge_ticks=taker_overlay_edge_ticks,
            global_drift_shift_ticks=global_drift_shift_ticks,
            drift_agreement_multiplier=drift_agreement_multiplier,
            drift_inventory_bias_ticks=drift_inventory_bias_ticks,
            realized_symbol_pnl=realized_symbol_pnl,
            pnl_allocation_score=pnl_allocation_score,
            toxicity_score=toxicity_score,
            toxicity_markout_pct=toxicity_markout_pct,
            toxicity_width_multiplier=toxicity_width_multiplier,
            toxicity_size_multiplier=toxicity_size_multiplier,
            cj_inventory_skew_ticks_per_lot=cj_inventory_skew_ticks_per_lot,
            cj_horizon_fraction=cj_glft_params.cj_horizon_fraction,
            glft_half_width_ticks=cj_glft_params.glft_half_width_ticks,
            glft_arrival_intensity=cj_glft_params.glft_arrival_intensity,
            glft_depth_sensitivity=cj_glft_params.glft_depth_sensitivity,
            inventory_close_penalty=inventory_close_penalty,
            inventory_pressure_score=inventory_pressure_score,
            max_affordable_bid_flatten_lots=max_affordable_bid_flatten_lots,
            estimated_reserved_buying_power=estimated_reserved_buying_power,
            estimated_short_close_buying_power=estimated_short_close_buying_power,
            estimated_available_buying_power=estimated_available_buying_power,
            buying_power_usage_ratio=buying_power_usage_ratio,
            buying_power_scale=buying_power_scale,
            fast_velocity_ticks=fast_velocity_ticks,
            slow_velocity_ticks=slow_velocity_ticks,
            queue_velocity=queue_velocity,
            liquidity_mass_norm=liq_mass,
            impact_mass_norm=imp_mass,
            physics_momentum_shift_ticks=physics_momentum_shift_ticks,
            return_zscore=return_zscore,
            mean_reversion_score=mean_reversion_score,
            noise_fade_shift_ticks=noise_fade_shift_ticks,
            mr_error_fast_ticks=mr_error_fast_ticks,
            mr_error_medium_ticks=mr_error_medium_ticks,
            mr_error_slow_ticks=mr_error_slow_ticks,
            mr_reversion_quality_fast=mr_reversion_quality_fast,
            mr_reversion_quality_medium=mr_reversion_quality_medium,
            mr_reversion_quality_slow=mr_reversion_quality_slow,
            mr_crossed_fast=mr_crossed_fast,
            mr_crossed_medium=mr_crossed_medium,
            mr_crossed_slow=mr_crossed_slow,
            mr_cross_direction=mr_cross_direction,
            mr_cross_depth=mr_cross_depth,
            mr_cross_anchor_price=mr_cross_anchor_price,
            sweep_cross_direction=sweep_cross_direction,
            sweep_cross_depth=sweep_cross_depth,
            sweep_cross_anchor_tau_s=sweep_cross_anchor_tau_s,
            sweep_cross_anchor_price=sweep_cross_anchor_price,
            noise_fade_taker_mode=noise_fade_taker_mode,
            noise_fade_taker_size_lots=noise_fade_taker_size_lots,
            noise_fade_ma_price=noise_fade_ma_price,
            noise_fade_bid_px=noise_fade_bid_px,
            noise_fade_ask_px=noise_fade_ask_px,
            sleeve_weight_base_mm=sleeve_weights.base_mm,
            sleeve_weight_lead_lag=sleeve_weights.lead_lag,
            sleeve_weight_pair_spread=sleeve_weights.pair_spread,
            sleeve_weight_inv_hedge=sleeve_weights.inv_hedge,
            sleeve_weight_taker_exit=sleeve_weights.taker_exit,
            sleeve_weight_noise_fade=sleeve_weights.noise_fade,
            sleeve_weight_cash=sleeve_weights.cash,
            sleeve_credit_base_mm=current_sleeve_credit.get("BASE_MM", 0.0),
            sleeve_credit_lead_lag=current_sleeve_credit.get("LEAD_LAG", 0.0),
            sleeve_credit_pair_spread=current_sleeve_credit.get("PAIR_SPREAD", 0.0),
            sleeve_credit_inv_hedge=current_sleeve_credit.get("INV_HEDGE", 0.0),
            sleeve_credit_taker_exit=current_sleeve_credit.get("TAKER_EXIT", 0.0),
            sleeve_credit_noise_fade=current_sleeve_credit.get("NOISE_FADE", 0.0),
            sweep_deviation_ticks=sweep_deviation_ticks,
            sweep_short_ma_price=sweep_short_ma,
            sweep_long_ma_price=sweep_long_ma,
            sweep_detected=sweep_detected,
            sweep_reversion_bid_levels=tuple(sweep_reversion_bid_levels),
            sweep_reversion_ask_levels=tuple(sweep_reversion_ask_levels),
            sweep_reversion_mode=sweep_reversion_mode,
            sweep_reversion_size_lots=sweep_reversion_size_lots,
            sweep_exit_mode=sweep_exit_mode,
            sweep_exit_lots=sweep_exit_lots,
            sweep_exit_side_sign=sweep_exit_side_sign,
            sweep_exit_trigger=sweep_exit_trigger,
            sweep_fill_price=sweep_exit_fill_price,
        )

    def _tick_size_for(self, symbol: str) -> float:
        """Return the tick size for a specific symbol, falling back to global tick_size."""
        return self.tick_size_by_symbol.get(symbol, self.tick_size)

    def _session_progress(self) -> SessionProgress | None:
        if self.session_clock is None:
            return None
        return self.session_clock.snapshot()

    def _safe_mode(self) -> str:
        if self.kill_switch is None:
            return SafeMode.NORMAL.value
        return self.kill_switch.mode.value

    def _live_quote_age_ms(self, symbol: str, now_ns: int) -> float:
        if self.order_ledger is None:
            return 0.0
        ages_ms: list[float] = []
        for order in self.order_ledger.live_orders(symbol=symbol):
            reference_ts_ns = order.last_update_ts_ns or order.submitted_ts_ns
            ages_ms.append(max((now_ns - reference_ts_ns) / 1_000_000, 0.0))
        if not ages_ms:
            return 0.0
        return max(ages_ms)

    def _queue_fill_support(
        self,
        state: SymbolState,
        *,
        now_ns: int | None = None,
    ) -> tuple[float, float, int, int, float, float]:
        if self.order_ledger is None:
            return 0.0, 1.0, 0, 0, 0.0, 0.0

        live_orders = self.order_ledger.live_orders(symbol=state.symbol)
        queue_shares: list[float] = []
        bid_queue_ahead_lots = 0
        ask_queue_ahead_lots = 0
        bid_queue_share = 0.0
        ask_queue_share = 0.0
        queue_latency_haircut = 1.0
        apply_latency_haircut = now_ns is not None
        book_age_ms = (
            max((now_ns - state.last_book_update_ns) / 1_000_000, 0.0)
            if apply_latency_haircut
            else 0.0
        )
        queue_half_life_ms = max(
            self.history_config.queue_latency_half_life_ms,
            1.0,
        )
        for order in live_orders:
            if order.remaining_size <= 0:
                continue
            reference_ts_ns = order.last_update_ts_ns or order.submitted_ts_ns
            latency_age_ms = (
                max(
                    book_age_ms,
                    max((now_ns - reference_ts_ns) / 1_000_000, 0.0),
                )
                if apply_latency_haircut
                else 0.0
            )
            order_latency_haircut = _clamp(
                math.exp(
                    -math.log(2.0) * latency_age_ms / queue_half_life_ms
                ),
                0.05,
                1.0,
            )
            queue_latency_haircut = min(
                queue_latency_haircut,
                order_latency_haircut,
            )
            if order.side == OrderSide.BID:
                bid_queue_ahead_lots = max(
                    state.queue_ahead_lots("bid", order.price, local=True),
                    0,
                )
                bid_queue_share = _clamp(
                    state.queue_share(
                        "bid",
                        order.price,
                        order.remaining_size,
                        local=True,
                    ),
                    0.0,
                    1.0,
                )
                bid_queue_share *= order_latency_haircut
                queue_shares.append(bid_queue_share)
            else:
                ask_queue_ahead_lots = max(
                    state.queue_ahead_lots("ask", order.price, local=True),
                    0,
                )
                ask_queue_share = _clamp(
                    state.queue_share(
                        "ask",
                        order.price,
                        order.remaining_size,
                        local=True,
                    ),
                    0.0,
                    1.0,
                )
                ask_queue_share *= order_latency_haircut
                queue_shares.append(ask_queue_share)
        if not queue_shares:
            return (
                0.0,
                queue_latency_haircut,
                bid_queue_ahead_lots,
                ask_queue_ahead_lots,
                bid_queue_share,
                ask_queue_share,
            )
        return (
            sum(queue_shares) / len(queue_shares),
            queue_latency_haircut,
            bid_queue_ahead_lots,
            ask_queue_ahead_lots,
            bid_queue_share,
            ask_queue_share,
        )

    def _absorb_sweep_fills(
        self,
        symbol: str,
        adaptive_state: SymbolAdaptiveState,
    ) -> None:
        """Scan the audit trail for fills matching any pending sweep ladder level.

        For each (price, side_sign, seen_count) entry in sweep_pending_levels,
        find the matching audit record and absorb any new fills into sweep_fill_lots.
        VWAP-tracks sweep_fill_price across all levels so the exit logic knows the
        average entry price when unloading the position.
        """
        if self.order_ledger is None or not adaptive_state.sweep_pending_levels:
            return
        tick = max(self._tick_size_for(symbol), 1e-9)
        audits = self.order_ledger.audits_for_symbol(symbol)
        updated_pending: list[tuple[float, float, int]] = []
        for pending_px, pending_side_sign, seen_count in adaptive_state.sweep_pending_levels:
            match_side = OrderSide.BID if pending_side_sign > 0.0 else OrderSide.ASK
            matched = False
            for audit in audits:
                if audit.side != match_side:
                    continue
                if abs(audit.submit_price - pending_px) > tick * 1.5:
                    continue
                new_fills = audit.fills[seen_count:]
                new_lots = sum(f.executed_size for f in new_fills)
                if new_lots > 0:
                    existing_lots = abs(adaptive_state.sweep_fill_lots)
                    total_lots = existing_lots + new_lots
                    fill_vwap = sum(f.executed_price * f.executed_size for f in new_fills)
                    adaptive_state.sweep_fill_price = (
                        (adaptive_state.sweep_fill_price * existing_lots + fill_vwap)
                        / total_lots
                    )
                    adaptive_state.sweep_fill_lots += int(pending_side_sign * new_lots)
                    # Set side_sign once on the first fill; don't overwrite across levels
                    if adaptive_state.sweep_fill_side_sign == 0.0:
                        adaptive_state.sweep_fill_side_sign = pending_side_sign
                    seen_count = len(audit.fills)
                matched = True
                break
            # Keep tracking this level while the order may still partially fill
            if matched:
                updated_pending.append((pending_px, pending_side_sign, seen_count))
        adaptive_state.sweep_pending_levels = updated_pending

    def _queue_pending_markouts(
        self,
        symbol: str,
        adaptive_state: SymbolAdaptiveState,
    ) -> None:
        if self.order_ledger is None:
            return
        active_order_ids: set[str] = set()
        for audit in self.order_ledger.audits_for_symbol(symbol):
            active_order_ids.add(audit.order_id)
            side_sign = 1.0 if audit.side == OrderSide.BID else -1.0
            start_index = min(
                adaptive_state.toxicity_seen_fill_count_by_order_id.get(
                    audit.order_id,
                    0,
                ),
                len(audit.fills),
            )
            for fill in audit.fills[start_index:]:
                if (
                    audit.liquidity == OrderLiquidity.LIMIT
                    and audit.submitted_ts_ns > 0
                    and fill.event_ts_ns >= audit.submitted_ts_ns
                ):
                    fill_arrival_ms = max(
                        (fill.event_ts_ns - audit.submitted_ts_ns) / 1_000_000,
                        0.0,
                    )
                    adaptive_state.ewma_passive_fill_arrival_ms = self._ewma(
                        adaptive_state.ewma_passive_fill_arrival_ms,
                        fill_arrival_ms,
                        self.history_config.fill_arrival_age_alpha,
                        initialized=adaptive_state.initialized
                        or adaptive_state.ewma_passive_fill_arrival_ms > 0.0,
                    )
                adaptive_state.pending_fill_markouts.append(
                    PendingFillMarkout(
                        side_sign=side_sign,
                        fill_price=fill.executed_price,
                        event_ts_ns=fill.event_ts_ns,
                    )
                )
            adaptive_state.toxicity_seen_fill_count_by_order_id[audit.order_id] = len(
                audit.fills
            )
        stale_order_ids = (
            set(adaptive_state.toxicity_seen_fill_count_by_order_id) - active_order_ids
        )
        for order_id in stale_order_ids:
            adaptive_state.toxicity_seen_fill_count_by_order_id.pop(order_id, None)
        overflow = (
            len(adaptive_state.pending_fill_markouts)
            - self.history_config.toxicity_max_pending_fills
        )
        if overflow > 0:
            del adaptive_state.pending_fill_markouts[:overflow]

    def _consume_ready_markouts(
        self,
        adaptive_state: SymbolAdaptiveState,
        *,
        current_mid: float,
        now_ns: int,
    ) -> float:
        if current_mid <= 0.0 or not adaptive_state.pending_fill_markouts:
            return adaptive_state.ewma_toxicity_score

        ready_markout_pct: list[float] = []
        ready_scores: list[float] = []
        keep_pending: list[PendingFillMarkout] = []
        delay_ns = max(self.history_config.toxicity_markout_delay_ns, 0)
        for markout in adaptive_state.pending_fill_markouts:
            if now_ns - markout.event_ts_ns < delay_ns:
                keep_pending.append(markout)
                continue
            if markout.fill_price <= 0.0:
                continue
            adverse_move = markout.side_sign * (markout.fill_price - current_mid)
            adverse_return_pct = 100.0 * adverse_move / max(markout.fill_price, 1e-9)
            adverse_return_pct = _clamp(adverse_return_pct, 0.0, 10.0)
            ready_markout_pct.append(adverse_return_pct)
            ready_scores.append(_clamp(4.0 * adverse_return_pct, 0.0, 1.0))

        adaptive_state.pending_fill_markouts = keep_pending
        if not ready_scores:
            return adaptive_state.ewma_toxicity_score
        adaptive_state.ewma_toxicity_markout_pct = self._ewma(
            adaptive_state.ewma_toxicity_markout_pct,
            sum(ready_markout_pct) / len(ready_markout_pct),
            self.history_config.toxicity_alpha,
            initialized=adaptive_state.initialized,
        )
        return sum(ready_scores) / len(ready_scores)

    def _fill_count(self, symbol: str) -> int:
        if self.order_ledger is None:
            return 0
        return self.order_ledger.fill_count(symbol)

    def _cancel_count(self, symbol: str) -> int:
        if self.order_ledger is None:
            return 0
        return self.order_ledger.cancel_count(symbol)

    def _total_fill_count(self) -> int:
        if self.order_ledger is None:
            return 0
        return self.order_ledger.total_fill_count()

    def _symbol_fee_stats(self, symbol: str) -> tuple[float, float, float]:
        if self.order_ledger is None:
            return 0.0, 0.0, 0.0
        return self.order_ledger.symbol_fee_stats(symbol)

    def _reserved_buying_power(self) -> float:
        if self.order_ledger is None:
            return 0.0
        return self.order_ledger.estimated_reserved_buying_power

    def _short_close_buying_power(self) -> float:
        if self.portfolio_ledger is None:
            return 0.0
        return self.portfolio_ledger.estimated_short_close_buying_power

    def _gross_buying_power(self) -> float:
        if self.portfolio_ledger is None:
            return 0.0
        return self.portfolio_ledger.summary.total_bp

    def _inventory_lots_by_symbol(self) -> dict[str, int]:
        if self.portfolio_ledger is None:
            return {}
        return {
            symbol: position.net_lots
            for symbol, position in self.portfolio_ledger.positions.items()
        }

    def _max_affordable_bid_flatten_lots(self, *, mark_price: float) -> int:
        if self.portfolio_ledger is None or mark_price <= 0.0:
            return self.fallback.inventory_limit_lots
        available_bp = max(
            self.portfolio_ledger.summary.total_bp - self._reserved_buying_power(),
            0.0,
        )
        affordable_lots = int(
            available_bp / max(mark_price * 100.0, 1e-9)
        )
        return max(affordable_lots, 0)

    def _realized_symbol_pnl(self, symbol: str) -> float:
        if self.portfolio_ledger is None:
            return 0.0
        position = self.portfolio_ledger.positions.get(symbol)
        if position is None:
            return 0.0
        return position.realized_pl

    def _inventory_close_penalty(
        self,
        symbol: str,
        *,
        fallback_price: float,
        inventory_abs_lots: int,
    ) -> float:
        if self.portfolio_ledger is not None:
            penalty = self.portfolio_ledger.estimate_forced_close_penalty(
                symbol,
                mark_price=fallback_price,
            )
            if penalty > 0.0:
                return penalty
        return inventory_abs_lots * 100.0 * fallback_price * 0.01

    @staticmethod
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
