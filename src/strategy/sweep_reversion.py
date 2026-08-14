"""Sweep-reversion overlay for the adaptive parameter pipeline.

Strategy: price sweeps away from a short moving average (~alpha=0.35,
half-life ~2 updates) then snaps back to it. On sweep detection this engine
front-loads a standing passive ladder AT the short MA, so the strategy has
queue priority and can fill on the snap-back rather than reacting after the
fact.

The long MA (~alpha=0.08) is the slow drift baseline. It biases which side
is preferred: if the long MA is rising, down-sweeps are more likely to
revert (drift supports them), so bids are preferentially front-loaded on
dips (and the mirror image for asks).

This also owns momentum suppression: when the mid price is mid-sweep (away
from the short MA), the physics-momentum shift is chasing noise, so it gets
faded proportionally to how confident the sweep detector is.

Extracted from `AdaptiveParameterProvider._derive_from_state` so this ~300
line subsystem can be read, and eventually tested, without the rest of the
quote-sizing pipeline around it. The output surface (`SweepReversionOutputs`)
matches exactly what the caller consumed from local variables before the
extraction — see the `SymbolStrategyParameters(...)` construction in
params.py for how each field is used downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.data.state import SymbolState
from src.execution.order_state import OrderLedger, OrderSide
from src.strategy.param_math import _clamp, _merge_sweep_pending_levels, _ramp_unit, _round_to_tick
from src.strategy.param_types import AdaptiveHistoryConfig, SymbolAdaptiveState


@dataclass(slots=True)
class SweepReversionOutputs:
    physics_momentum_shift_ticks: float
    return_zscore: float
    mean_reversion_score: float
    noise_fade_shift_ticks: float
    mr_error_fast_ticks: float
    mr_error_medium_ticks: float
    mr_error_slow_ticks: float
    mr_reversion_quality_fast: float
    mr_reversion_quality_medium: float
    mr_reversion_quality_slow: float
    mr_crossed_fast: bool
    mr_crossed_medium: bool
    mr_crossed_slow: bool
    mr_cross_direction: int
    mr_cross_depth: int
    mr_cross_anchor_price: float
    sweep_cross_direction: int
    sweep_cross_depth: int
    sweep_cross_anchor_tau_s: float
    sweep_cross_anchor_price: float
    noise_fade_taker_mode: bool
    noise_fade_taker_size_lots: int
    noise_fade_ma_price: float
    noise_fade_bid_px: float
    noise_fade_ask_px: float
    sweep_deviation_ticks: float
    sweep_short_ma_price: float
    sweep_long_ma_price: float
    sweep_detected: bool
    sweep_reversion_bid_levels: list[tuple[float, int]] = field(default_factory=list)
    sweep_reversion_ask_levels: list[tuple[float, int]] = field(default_factory=list)
    sweep_reversion_mode: bool = False
    sweep_reversion_size_lots: int = 0
    sweep_exit_mode: bool = False
    sweep_exit_lots: int = 0
    sweep_exit_side_sign: float = 0.0
    sweep_exit_trigger: str = ""
    sweep_exit_fill_price: float = 0.0
    inventory_target_lots: int = 0


class SweepReversionEngine:
    """Stateless: all state lives on the `SymbolAdaptiveState` passed in."""

    def compute(
        self,
        *,
        state: SymbolState,
        adaptive_state: SymbolAdaptiveState,
        tick_size: float,
        now_ns: int,
        inventory_lots: int,
        inventory_limit_lots: int,
        flatten_only_mode: bool,
        session_open: bool,
        toxicity_score: float,
        rls_confidence: float,
        physics_momentum_shift_ticks: float,
        momentum_max: float,
        order_ledger: OrderLedger | None,
        history_config: AdaptiveHistoryConfig,
    ) -> SweepReversionOutputs:
        # Absorb any fills that came back for the pending sweep order.
        # Must happen before the sweep overlay logic so sweep_fill_lots is current.
        self._absorb_sweep_fills(
            state.symbol,
            adaptive_state,
            order_ledger=order_ledger,
            tick_size=tick_size,
        )

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
        sweep_thresh = history_config.sweep_threshold_ticks
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
            fade = history_config.mean_reversion_momentum_fade
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
            and toxicity_score <= history_config.sweep_max_toxicity
            and current_book_age_ms <= history_config.sweep_max_book_age_ms
            and adaptive_state.updates_seen >= 6
            and sweep_short_ma > 0.0
        ):
            min_edge = history_config.sweep_min_edge_ticks
            max_levels = max(history_config.sweep_ladder_depth_levels, 1)
            sweep_inv_cap = inventory_limit_lots

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
            exit_thresh = history_config.sweep_exit_threshold_ticks * tick_size
            fill_px = adaptive_state.sweep_fill_price
            # Only exit if the fill was genuinely far from the mass — we need at
            # least min_edge ticks of expected reversion P&L to have been captured.
            # A fill at the mass itself means the overlay misfired; skip.
            fill_was_in_sweep = (
                fill_px > 0.0
                and abs(fill_px - sweep_short_ma) >= history_config.sweep_min_edge_ticks * tick_size
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

        return SweepReversionOutputs(
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
            mr_cross_anchor_price=adaptive_state.mr_cross_anchor_price,
            sweep_cross_direction=sweep_cross_direction,
            sweep_cross_depth=sweep_cross_depth,
            sweep_cross_anchor_tau_s=sweep_cross_anchor_tau_s,
            sweep_cross_anchor_price=sweep_cross_anchor_price,
            noise_fade_taker_mode=noise_fade_taker_mode,
            noise_fade_taker_size_lots=noise_fade_taker_size_lots,
            noise_fade_ma_price=noise_fade_ma_price,
            noise_fade_bid_px=noise_fade_bid_px,
            noise_fade_ask_px=noise_fade_ask_px,
            sweep_deviation_ticks=sweep_deviation_ticks,
            sweep_short_ma_price=sweep_short_ma,
            sweep_long_ma_price=sweep_long_ma,
            sweep_detected=sweep_detected,
            sweep_reversion_bid_levels=sweep_reversion_bid_levels,
            sweep_reversion_ask_levels=sweep_reversion_ask_levels,
            sweep_reversion_mode=sweep_reversion_mode,
            sweep_reversion_size_lots=sweep_reversion_size_lots,
            sweep_exit_mode=sweep_exit_mode,
            sweep_exit_lots=sweep_exit_lots,
            sweep_exit_side_sign=sweep_exit_side_sign,
            sweep_exit_trigger=sweep_exit_trigger,
            sweep_exit_fill_price=sweep_exit_fill_price,
            inventory_target_lots=inventory_target_lots,
        )

    def _absorb_sweep_fills(
        self,
        symbol: str,
        adaptive_state: SymbolAdaptiveState,
        *,
        order_ledger: OrderLedger | None,
        tick_size: float,
    ) -> None:
        """Scan the audit trail for fills matching any pending sweep ladder level.

        For each (price, side_sign, seen_count) entry in sweep_pending_levels,
        find the matching audit record and absorb any new fills into sweep_fill_lots.
        VWAP-tracks sweep_fill_price across all levels so the exit logic knows the
        average entry price when unloading the position.
        """
        if order_ledger is None or not adaptive_state.sweep_pending_levels:
            return
        tick = max(tick_size, 1e-9)
        audits = order_ledger.audits_for_symbol(symbol)
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
