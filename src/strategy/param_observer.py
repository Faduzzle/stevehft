from __future__ import annotations

import math
from typing import Protocol

from src.data.state import SymbolState
from src.strategy.param_types import AdaptiveHistoryConfig, SymbolAdaptiveState


class AdaptiveParameterObserverContext(Protocol):
    tick_size: float
    history_config: AdaptiveHistoryConfig
    state_by_symbol: dict[str, SymbolAdaptiveState]

    def _live_quote_age_ms(self, symbol: str, now_ns: int) -> float: ...

    def _fill_count(self, symbol: str) -> int: ...

    def _cancel_count(self, symbol: str) -> int: ...

    def _queue_pending_markouts(
        self,
        symbol: str,
        adaptive_state: SymbolAdaptiveState,
    ) -> None: ...

    def _consume_ready_markouts(
        self,
        adaptive_state: SymbolAdaptiveState,
        *,
        current_mid: float,
        now_ns: int,
    ) -> float: ...

    def _ewma(
        self,
        previous: float,
        current: float,
        alpha: float,
        *,
        initialized: bool,
    ) -> float: ...


def observe_symbol_state(
    context: AdaptiveParameterObserverContext,
    symbol: str,
    state: SymbolState,
    *,
    now_ns: int,
) -> SymbolAdaptiveState:
    adaptive_state = context.state_by_symbol.get(symbol)
    if adaptive_state is None:
        adaptive_state = SymbolAdaptiveState(first_update_ns=now_ns)
        context.state_by_symbol[symbol] = adaptive_state

    update_ns = state.last_book_update_ns or state.best_price.update_ts_ns
    if update_ns <= 0 or update_ns == adaptive_state.last_update_ns:
        return adaptive_state
    dt_s = (
        max((update_ns - adaptive_state.last_update_ns) / 1_000_000_000.0, 0.0)
        if adaptive_state.last_update_ns > 0
        else 0.0
    )

    spread_ticks = state.spread / context.tick_size if context.tick_size > 0.0 else 0.0
    depth_levels = context.history_config.depth_levels
    depth_fraction = context.history_config.depth_fraction
    book_depth = float(
        state.cumulative_bid_depth(depth_levels, local=True)
        + state.cumulative_ask_depth(depth_levels, local=True)
    )
    signed_imbalance = state.depth_imbalance(depth_levels, local=True)
    abs_imbalance = abs(signed_imbalance)
    local_voi = state.local_multi_level_voi
    depth_entropy = state.depth_entropy(depth_levels, local=True)
    bid_fraction_levels = float(
        state.levels_to_cover_side_volume_fraction(
            "bid",
            depth_fraction,
            local=True,
        )
    )
    ask_fraction_levels = float(
        state.levels_to_cover_side_volume_fraction(
            "ask",
            depth_fraction,
            local=True,
        )
    )
    global_imbalance = state.global_l1_imbalance
    global_voi = state.global_l1_voi
    global_mid = state.global_l1_mid
    local_micro = state.local_microprice
    global_micro = state.global_microprice
    local_global_micro_divergence_bps = (
        10000.0 * (local_micro - global_micro) / global_micro
        if global_micro > 1e-9
        else 0.0
    )
    local_global_imbalance_divergence = signed_imbalance - global_imbalance
    global_mid_drift = (
        global_mid - adaptive_state.prev_global_mid
        if adaptive_state.prev_global_mid > 0.0 and global_mid > 0.0
        else 0.0
    )
    quote_age_ms = context._live_quote_age_ms(symbol, now_ns)
    fill_count = context._fill_count(symbol)
    cancel_count = context._cancel_count(symbol)
    fill_delta = max(fill_count - adaptive_state.last_fill_count, 0)
    cancel_delta = max(cancel_count - adaptive_state.last_cancel_count, 0)
    trade_signed_volume = state.trade_signed_volume
    local_trade_linked_voi = _clamp(local_voi, -1.0, 1.0) * _clamp(
        trade_signed_volume / max(float(book_depth), 1.0),
        -1.0,
        1.0,
    )
    rls_features = [
        1.0,
        _clamp(signed_imbalance, -1.0, 1.0),
        _clamp(local_voi, -1.0, 1.0),
        _clamp(global_imbalance, -1.0, 1.0),
        _clamp(global_voi, -1.0, 1.0),
        _clamp(local_trade_linked_voi, -1.0, 1.0),
        # local-global interaction terms
        _clamp(local_global_micro_divergence_bps / 10.0, -1.0, 1.0),
        _clamp(local_global_imbalance_divergence, -1.0, 1.0),
    ]
    context._queue_pending_markouts(symbol, adaptive_state)
    adaptive_state.spread_welford.decay = 1.0 - context.history_config.spread_alpha
    adaptive_state.imbalance_welford.decay = (
        1.0 - context.history_config.imbalance_alpha
    )
    adaptive_state.depth_welford.decay = 1.0 - context.history_config.depth_alpha
    adaptive_state.global_drift_welford.decay = (
        1.0 - context.history_config.global_drift_alpha
    )
    adaptive_state.return_welford.decay = (
        1.0 - context.history_config.return_var_alpha
    )
    adaptive_state.quote_age_welford.decay = (
        1.0 - context.history_config.quote_age_alpha
    )

    if adaptive_state.last_mid_price > 0.0 and state.mid_price > 0.0:
        mid_return = (
            state.mid_price - adaptive_state.last_mid_price
        ) / adaptive_state.last_mid_price
        mid_move_ticks = (
            state.mid_price - adaptive_state.last_mid_price
        ) / max(context.tick_size, 1e-9)
        return_sq = mid_return * mid_return
    else:
        mid_return = 0.0
        mid_move_ticks = 0.0
        return_sq = 0.0

    adaptive_state.ewma_spread_ticks = context._ewma(
        adaptive_state.ewma_spread_ticks,
        spread_ticks,
        context.history_config.spread_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_signed_imbalance = context._ewma(
        adaptive_state.ewma_signed_imbalance,
        signed_imbalance,
        context.history_config.imbalance_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_abs_imbalance = context._ewma(
        adaptive_state.ewma_abs_imbalance,
        abs_imbalance,
        context.history_config.imbalance_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_local_voi = context._ewma(
        adaptive_state.ewma_local_voi,
        local_voi,
        context.history_config.local_voi_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_depth_lk = context._ewma(
        adaptive_state.ewma_depth_lk,
        book_depth,
        context.history_config.depth_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_bid_depth_fraction_levels = context._ewma(
        adaptive_state.ewma_bid_depth_fraction_levels,
        bid_fraction_levels,
        context.history_config.depth_shape_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_ask_depth_fraction_levels = context._ewma(
        adaptive_state.ewma_ask_depth_fraction_levels,
        ask_fraction_levels,
        context.history_config.depth_shape_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_mid_return_var = context._ewma(
        adaptive_state.ewma_mid_return_var,
        return_sq,
        context.history_config.return_var_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_quote_age_ms = context._ewma(
        adaptive_state.ewma_quote_age_ms,
        quote_age_ms,
        context.history_config.quote_age_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_fill_rate = context._ewma(
        adaptive_state.ewma_fill_rate,
        float(fill_delta),
        context.history_config.fill_rate_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_cancel_rate = context._ewma(
        adaptive_state.ewma_cancel_rate,
        float(cancel_delta),
        context.history_config.cancel_rate_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_toxicity_score = context._ewma(
        adaptive_state.ewma_toxicity_score,
        context._consume_ready_markouts(
            adaptive_state,
            current_mid=state.mid_price,
            now_ns=update_ns,
        ),
        context.history_config.toxicity_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_global_imbalance = context._ewma(
        adaptive_state.ewma_global_imbalance,
        global_imbalance,
        context.history_config.global_imbalance_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_global_voi = context._ewma(
        adaptive_state.ewma_global_voi,
        global_voi,
        context.history_config.global_voi_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_global_mid_drift = context._ewma(
        adaptive_state.ewma_global_mid_drift,
        global_mid_drift,
        context.history_config.global_drift_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_trade_signed_volume = context._ewma(
        adaptive_state.ewma_trade_signed_volume,
        trade_signed_volume,
        context.history_config.trade_linked_voi_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_trade_linked_voi = context._ewma(
        adaptive_state.ewma_trade_linked_voi,
        local_trade_linked_voi,
        context.history_config.trade_linked_voi_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_local_global_micro_divergence_bps = context._ewma(
        adaptive_state.ewma_local_global_micro_divergence_bps,
        local_global_micro_divergence_bps,
        context.history_config.local_global_micro_divergence_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_imbalance_divergence = context._ewma(
        adaptive_state.ewma_imbalance_divergence,
        local_global_imbalance_divergence,
        context.history_config.local_global_imbalance_divergence_alpha,
        initialized=adaptive_state.initialized,
    )
    if trade_signed_volume > 0.0:
        trade_positive_probability = 1.0
    elif trade_signed_volume < 0.0:
        trade_positive_probability = 0.0
    else:
        trade_positive_probability = adaptive_state.ewma_trade_positive_probability
    adaptive_state.ewma_trade_positive_probability = context._ewma(
        adaptive_state.ewma_trade_positive_probability,
        trade_positive_probability,
        context.history_config.flow_entropy_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_flow_entropy = context._ewma(
        adaptive_state.ewma_flow_entropy,
        _binary_entropy(adaptive_state.ewma_trade_positive_probability),
        context.history_config.flow_entropy_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.ewma_depth_entropy = context._ewma(
        adaptive_state.ewma_depth_entropy,
        depth_entropy,
        context.history_config.depth_shape_alpha,
        initialized=adaptive_state.initialized,
    )
    adaptive_state.spread_welford.update(spread_ticks)
    adaptive_state.imbalance_welford.update(signed_imbalance)
    adaptive_state.depth_welford.update(book_depth)
    adaptive_state.global_drift_welford.update(
        global_mid_drift / max(context.tick_size, 1e-9)
    )
    spread_zscore = adaptive_state.spread_welford.zscore(spread_ticks)
    global_drift_zscore = adaptive_state.global_drift_welford.zscore(
        global_mid_drift / max(context.tick_size, 1e-9)
    )
    adaptive_state.spread_cusum.update(spread_zscore)
    adaptive_state.global_drift_page_hinkley.update(global_drift_zscore)
    adaptive_state.spread_p90.update(spread_ticks)
    adaptive_state.depth_p50.update(book_depth)
    adaptive_state.return_welford.update(mid_return)
    adaptive_state.quote_age_welford.update(quote_age_ms)
    adaptive_state.quote_age_p90.update(quote_age_ms)
    if adaptive_state.prev_rls_features:
        adaptive_state.drift_rls.update(
            adaptive_state.prev_rls_features,
            _clamp(mid_move_ticks, -2.0, 2.0),
        )
    adaptive_state.rls_drift_prediction_ticks = _clamp(
        adaptive_state.drift_rls.predict(rls_features),
        -1.0,
        1.0,
    )
    # RLS confidence: how much to trust the current prediction.
    # cosine_similarity measures regime alignment (are we in a market the model
    # was trained on?).  prediction_variance measures model uncertainty at this
    # specific input (shrinks as data accumulates).
    # confidence = max(cosine_similarity, 0) * (1 - prediction_variance)
    # so it requires BOTH regime alignment AND model convergence.
    cos_sim = adaptive_state.drift_rls.cosine_similarity_to_weights(rls_features)
    pred_var = adaptive_state.drift_rls.prediction_variance(rls_features)
    adaptive_state.rls_cosine_similarity = cos_sim
    adaptive_state.rls_prediction_variance = pred_var
    adaptive_state.rls_confidence = _clamp(
        max(cos_sim, 0.0) * (1.0 - pred_var),
        0.0,
        1.0,
    )
    adaptive_state.prev_rls_features = rls_features

    # --- Physics-informed momentum ---
    # fast velocity: clamp to ±2 ticks before EWMA to match RLS training range
    # and prevent a single large print from dominating
    fast_velocity_ticks = _clamp(mid_move_ticks, -2.0, 2.0)

    # slow velocity: use the confidence-gated RLS prediction directly — it is
    # already a slowly-adapting signal via the RLS forgetting factor, so adding
    # another EWMA layer would create 1-2s of additional lag
    slow_velocity_ticks = (
        adaptive_state.rls_drift_prediction_ticks * adaptive_state.rls_confidence
    )

    # queue velocity: flow-weighted directionality signal (-1..+1)
    queue_velocity = _clamp(
        0.55 * _clamp(local_voi, -1.0, 1.0)
        + 0.45 * _clamp(local_trade_linked_voi, -1.0, 1.0),
        -1.0,
        1.0,
    )

    adaptive_state.ewma_fast_velocity_ticks = context._ewma(
        adaptive_state.ewma_fast_velocity_ticks,
        fast_velocity_ticks,
        context.history_config.momentum_fast_alpha,
        initialized=adaptive_state.initialized,
    )
    # No extra EWMA on slow_velocity — RLS forgetting factor is sufficient
    adaptive_state.ewma_slow_velocity_ticks = slow_velocity_ticks
    adaptive_state.ewma_queue_velocity = context._ewma(
        adaptive_state.ewma_queue_velocity,
        queue_velocity,
        context.history_config.momentum_queue_alpha,
        initialized=adaptive_state.initialized,
    )

    # liquidity mass: smoothed depth relative to its own median
    # values in (0, 2]; >1 = deep book, <1 = thin book
    depth_median = max(adaptive_state.depth_p50.estimate, 1.0)
    raw_liq_mass = float(book_depth) / depth_median
    adaptive_state.liquidity_mass_norm = _clamp(raw_liq_mass / 2.0, 0.1, 1.0)

    # impact mass: inverse depth pressure — thin book amplifies signal
    adaptive_state.impact_mass_norm = _clamp(1.0 - adaptive_state.liquidity_mass_norm, 0.0, 0.9)

    # Sweep-reversion MAs.
    # Short MA (α≈0.35): fast attractor — this is the snap-back TARGET.
    # Half-life ~2 updates → reacts to current sweep within 2-4 bars.
    # Long MA (α≈0.08): slow drift baseline — biases which side to front-load
    # and tells us whether a sweep down is against or with the drift.
    if state.mid_price > 0.0:
        prev_fast_error_ticks = adaptive_state.mr_error_fast_ticks
        prev_medium_error_ticks = adaptive_state.mr_error_medium_ticks
        prev_slow_error_ticks = adaptive_state.mr_error_slow_ticks
        adaptive_state.mr_ema_fast_price = _update_time_based_ema(
            context,
            previous=adaptive_state.mr_ema_fast_price,
            current=state.mid_price,
            tau_s=context.history_config.mean_reversion_tau_fast_s,
            dt_s=dt_s,
        )
        adaptive_state.mr_ema_medium_price = _update_time_based_ema(
            context,
            previous=adaptive_state.mr_ema_medium_price,
            current=state.mid_price,
            tau_s=context.history_config.mean_reversion_tau_medium_s,
            dt_s=dt_s,
        )
        adaptive_state.mr_ema_slow_price = _update_time_based_ema(
            context,
            previous=adaptive_state.mr_ema_slow_price,
            current=state.mid_price,
            tau_s=context.history_config.mean_reversion_tau_slow_s,
            dt_s=dt_s,
        )
        adaptive_state.mr_error_fast_ticks = (
            state.mid_price - adaptive_state.mr_ema_fast_price
        ) / max(context.tick_size, 1e-9)
        adaptive_state.mr_error_medium_ticks = (
            state.mid_price - adaptive_state.mr_ema_medium_price
        ) / max(context.tick_size, 1e-9)
        adaptive_state.mr_error_slow_ticks = (
            state.mid_price - adaptive_state.mr_ema_slow_price
        ) / max(context.tick_size, 1e-9)
        adaptive_state.mr_crossed_fast = _crossed_zero(
            prev_fast_error_ticks,
            adaptive_state.mr_error_fast_ticks,
            initialized=adaptive_state.initialized,
        )
        adaptive_state.mr_crossed_medium = _crossed_zero(
            prev_medium_error_ticks,
            adaptive_state.mr_error_medium_ticks,
            initialized=adaptive_state.initialized,
        )
        adaptive_state.mr_crossed_slow = _crossed_zero(
            prev_slow_error_ticks,
            adaptive_state.mr_error_slow_ticks,
            initialized=adaptive_state.initialized,
        )
        adaptive_state.mr_cross_depth = (
            int(adaptive_state.mr_crossed_fast)
            + int(adaptive_state.mr_crossed_medium)
            + int(adaptive_state.mr_crossed_slow)
        )
        adaptive_state.mr_cross_direction = 0
        adaptive_state.mr_cross_anchor_price = 0.0
        if adaptive_state.mr_cross_depth > 0:
            if mid_move_ticks > 0.0:
                adaptive_state.mr_cross_direction = 1
            elif mid_move_ticks < 0.0:
                adaptive_state.mr_cross_direction = -1
            if adaptive_state.mr_crossed_slow and adaptive_state.mr_ema_slow_price > 0.0:
                adaptive_state.mr_cross_anchor_price = adaptive_state.mr_ema_slow_price
            elif (
                adaptive_state.mr_crossed_medium
                and adaptive_state.mr_ema_medium_price > 0.0
            ):
                adaptive_state.mr_cross_anchor_price = (
                    adaptive_state.mr_ema_medium_price
                )
            elif adaptive_state.mr_crossed_fast and adaptive_state.mr_ema_fast_price > 0.0:
                adaptive_state.mr_cross_anchor_price = adaptive_state.mr_ema_fast_price
        adaptive_state.mr_reversion_quality_fast = _update_reversion_quality(
            context,
            previous_quality=adaptive_state.mr_reversion_quality_fast,
            previous_error_ticks=prev_fast_error_ticks,
            mid_move_ticks=mid_move_ticks,
        )
        adaptive_state.mr_reversion_quality_medium = _update_reversion_quality(
            context,
            previous_quality=adaptive_state.mr_reversion_quality_medium,
            previous_error_ticks=prev_medium_error_ticks,
            mid_move_ticks=mid_move_ticks,
        )
        adaptive_state.mr_reversion_quality_slow = _update_reversion_quality(
            context,
            previous_quality=adaptive_state.mr_reversion_quality_slow,
            previous_error_ticks=prev_slow_error_ticks,
            mid_move_ticks=mid_move_ticks,
        )

        short_init = adaptive_state.ewma_sweep_short_ma > 0.0
        adaptive_state.ewma_sweep_short_ma = context._ewma(
            adaptive_state.ewma_sweep_short_ma if short_init else state.mid_price,
            state.mid_price,
            context.history_config.sweep_short_ma_alpha,
            initialized=short_init,
        )
        long_init = adaptive_state.ewma_sweep_long_ma > 0.0
        adaptive_state.ewma_sweep_long_ma = context._ewma(
            adaptive_state.ewma_sweep_long_ma if long_init else state.mid_price,
            state.mid_price,
            context.history_config.sweep_long_ma_alpha,
            initialized=long_init,
        )
        adaptive_state.ewma_noise_fade_ma = adaptive_state.ewma_sweep_short_ma

        previous_signature_emas = list(adaptive_state.sweep_signature_ema_prices)
        tau_values = tuple(context.history_config.sweep_signature_tau_s)
        if not previous_signature_emas or len(previous_signature_emas) != len(tau_values):
            previous_signature_emas = [state.mid_price for _ in tau_values]
        updated_signature_emas: list[float] = []
        crossed_indices: list[int] = []
        for idx, tau_s in enumerate(tau_values):
            prev_ema = previous_signature_emas[idx]
            next_ema = _update_time_based_ema(
                context,
                previous=prev_ema,
                current=state.mid_price,
                tau_s=tau_s,
                dt_s=dt_s,
            )
            updated_signature_emas.append(next_ema)
            if _crossed_zero(
                adaptive_state.last_mid_price - prev_ema,
                state.mid_price - next_ema,
                initialized=adaptive_state.initialized,
            ):
                crossed_indices.append(idx)
        adaptive_state.sweep_signature_ema_prices = updated_signature_emas
        adaptive_state.sweep_cross_direction = 0
        adaptive_state.sweep_cross_depth = 0
        adaptive_state.sweep_cross_anchor_tau_s = 0.0
        adaptive_state.sweep_cross_anchor_price = 0.0
        if crossed_indices:
            deepest_idx = crossed_indices[-1]
            adaptive_state.sweep_cross_depth = len(crossed_indices)
            adaptive_state.sweep_cross_anchor_tau_s = tau_values[deepest_idx]
            adaptive_state.sweep_cross_anchor_price = updated_signature_emas[deepest_idx]
            if mid_move_ticks > 0.0:
                adaptive_state.sweep_cross_direction = 1
            elif mid_move_ticks < 0.0:
                adaptive_state.sweep_cross_direction = -1
    # Return z-score: how extreme is the current per-update return vs its own
    # recent distribution?  Used for momentum suppression gating only.
    if adaptive_state.return_welford.initialized:
        current_return_zscore = adaptive_state.return_welford.zscore(mid_return)
    else:
        current_return_zscore = 0.0
    adaptive_state.ewma_return_zscore = context._ewma(
        adaptive_state.ewma_return_zscore,
        current_return_zscore,
        0.45,  # fast — picks up current sweep in 2-3 updates
        initialized=adaptive_state.initialized,
    )

    adaptive_state.initialized = True
    adaptive_state.updates_seen += 1
    adaptive_state.last_mid_price = state.mid_price
    adaptive_state.prev_global_mid = global_mid
    adaptive_state.last_update_ns = update_ns
    adaptive_state.last_fill_count = fill_count
    adaptive_state.last_cancel_count = cancel_count
    return adaptive_state


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _binary_entropy(probability: float) -> float:
    probability = _clamp(probability, 1e-9, 1.0 - 1e-9)
    return (
        -probability * _log2(probability)
        - (1.0 - probability) * _log2(1.0 - probability)
    )


def _log2(value: float) -> float:
    return math.log(value, 2)


def _time_based_alpha(*, tau_s: float, dt_s: float) -> float:
    if tau_s <= 0.0:
        return 1.0
    if dt_s <= 0.0:
        return 0.0
    return _clamp(1.0 - math.exp(-dt_s / tau_s), 0.0, 1.0)


def _update_time_based_ema(
    context: AdaptiveParameterObserverContext,
    *,
    previous: float,
    current: float,
    tau_s: float,
    dt_s: float,
) -> float:
    initialized = previous > 0.0
    if not initialized:
        return current
    return context._ewma(
        previous,
        current,
        _time_based_alpha(tau_s=tau_s, dt_s=dt_s),
        initialized=True,
    )


def _update_reversion_quality(
    context: AdaptiveParameterObserverContext,
    *,
    previous_quality: float,
    previous_error_ticks: float,
    mid_move_ticks: float,
) -> float:
    if abs(previous_error_ticks) <= 0.25 or abs(mid_move_ticks) <= 1e-9:
        return previous_quality
    reverted = previous_error_ticks * mid_move_ticks < 0.0
    current_quality = 1.0 if reverted else 0.0
    return context._ewma(
        previous_quality,
        current_quality,
        context.history_config.mean_reversion_quality_alpha,
        initialized=True,
    )


def _crossed_zero(
    previous_error_ticks: float,
    current_error_ticks: float,
    *,
    initialized: bool,
) -> bool:
    if not initialized:
        return False
    if abs(previous_error_ticks) <= 0.05 or abs(current_error_ticks) <= 0.05:
        return False
    return previous_error_ticks * current_error_ticks < 0.0
