from __future__ import annotations

from dataclasses import dataclass

from src.data.state import SymbolState
from src.execution.order_state import QuoteLevelTarget, QuoteTarget
from src.strategy.params import SymbolStrategyParameters
from src.strategy.signals import (
    QuoteGateDecision,
    compute_fair_value,
    compute_half_width,
    compute_inventory_skew,
    round_to_tick,
)


@dataclass(slots=True)
class MarketMakingQuotePlan:
    bid_px: float
    ask_px: float
    bid_size: int
    ask_size: int
    enable_bid: bool
    enable_ask: bool
    flatten_mode: bool
    taker_overlay_mode: bool
    reason: str
    fair_value: float
    center: float
    inventory_skew: float
    imbalance_shift: float
    front_shape_shift: float
    half_width: float
    bid_half_width: float
    ask_half_width: float
    raw_bid_size: float
    raw_ask_size: float
    flatten_target_lots: int
    center_shift_applied: float = 0.0

    @property
    def participation_mode(self) -> str:
        if self.enable_bid and self.enable_ask:
            return "passive_two_sided"
        if self.enable_bid or self.enable_ask:
            return "passive_one_sided"
        return "suppressed"

    @property
    def quote_width(self) -> float:
        return max(self.ask_px - self.bid_px, 0.0)

    def to_quote_target(
        self,
        *,
        symbol: str,
        params: SymbolStrategyParameters,
        tick_size: float,
    ) -> QuoteTarget:
        if self.reason == "sweep_exit_buy" and self.enable_bid:
            # Exit: spread lots across levels at and below the exit price.
            bid_levels = _build_sweep_exit_levels(
                side="bid",
                anchor_px=self.bid_px,
                total_size=self.bid_size,
                max_levels=params.passive_ladder_depth_levels,
                queue_ahead_lots=params.bid_queue_ahead_lots,
                queue_share=params.bid_queue_share,
                tick_size=tick_size,
            )
        else:
            # Normal quote level 0, plus sweep resting levels appended after.
            normal_bid = self._build_side_levels(
                side="bid",
                best_px=self.bid_px,
                best_size=self.bid_size,
                queue_ahead_lots=params.bid_queue_ahead_lots,
                queue_share=params.bid_queue_share,
                enabled=self.enable_bid,
                max_levels=params.passive_ladder_depth_levels,
                side_front_edge=_side_front_edge("bid", params),
                tick_size=tick_size,
            )
            if self.taker_overlay_mode and params.sweep_reversion_bid_levels:
                # Append sweep resting levels with index offset so router treats
                # them as independent orders distinct from the normal maker level.
                offset = len(normal_bid)
                sweep_bid = _sweep_ladder_to_levels(
                    params.sweep_reversion_bid_levels,
                    queue_ahead_lots=params.bid_queue_ahead_lots,
                    queue_share=params.bid_queue_share,
                    index_offset=offset,
                )
                bid_levels = normal_bid + sweep_bid
            else:
                bid_levels = normal_bid

        if self.reason == "sweep_exit_sell" and self.enable_ask:
            # Exit: spread lots across levels at and above the exit price.
            ask_levels = _build_sweep_exit_levels(
                side="ask",
                anchor_px=self.ask_px,
                total_size=self.ask_size,
                max_levels=params.passive_ladder_depth_levels,
                queue_ahead_lots=params.ask_queue_ahead_lots,
                queue_share=params.ask_queue_share,
                tick_size=tick_size,
            )
        else:
            normal_ask = self._build_side_levels(
                side="ask",
                best_px=self.ask_px,
                best_size=self.ask_size,
                queue_ahead_lots=params.ask_queue_ahead_lots,
                queue_share=params.ask_queue_share,
                enabled=self.enable_ask,
                max_levels=params.passive_ladder_depth_levels,
                side_front_edge=_side_front_edge("ask", params),
                tick_size=tick_size,
            )
            if self.taker_overlay_mode and params.sweep_reversion_ask_levels:
                offset = len(normal_ask)
                sweep_ask = _sweep_ladder_to_levels(
                    params.sweep_reversion_ask_levels,
                    queue_ahead_lots=params.ask_queue_ahead_lots,
                    queue_share=params.ask_queue_share,
                    index_offset=offset,
                )
                ask_levels = normal_ask + sweep_ask
            else:
                ask_levels = normal_ask
        return QuoteTarget(
            symbol=symbol,
            bid_px=self.bid_px,
            ask_px=self.ask_px,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
            bid_queue_ahead_lots=params.bid_queue_ahead_lots,
            ask_queue_ahead_lots=params.ask_queue_ahead_lots,
            bid_queue_share=params.bid_queue_share,
            ask_queue_share=params.ask_queue_share,
            enable_bid=self.enable_bid,
            enable_ask=self.enable_ask,
            flatten_mode=self.flatten_mode,
            reason=self.reason,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
        )

    def _build_side_levels(
        self,
        *,
        side: str,
        best_px: float,
        best_size: int,
        queue_ahead_lots: int,
        queue_share: float,
        enabled: bool,
        max_levels: int,
        side_front_edge: float,
        tick_size: float,
    ) -> tuple[QuoteLevelTarget, ...]:
        if (
            self.flatten_mode
            or not enabled
            or best_size <= 0
            or best_px <= 0.0
            or max_levels <= 1
        ):
            return (
                QuoteLevelTarget(
                    level_index=0,
                    price=best_px if enabled else None,
                    size=best_size if enabled else 0,
                    queue_ahead_lots=queue_ahead_lots,
                    queue_share=queue_share,
                    enabled=enabled,
                ),
            )

        levels = [
            QuoteLevelTarget(
                level_index=0,
                price=best_px,
                size=best_size,
                queue_ahead_lots=queue_ahead_lots,
                queue_share=queue_share,
                enabled=True,
            ),
        ]
        for level_index in range(1, max_levels):
            level_offset_ticks = level_index * tick_size
            level_px = (
                max(best_px - level_offset_ticks, tick_size)
                if side == "bid"
                else best_px + level_offset_ticks
            )
            depth_size_multiplier = max(
                min(
                    1.0
                    + 0.35 * level_index
                    - 0.65 * side_front_edge,
                    2.5,
                ),
                0.5,
            )
            level_size = max(int(round(best_size * depth_size_multiplier)), 1)
            levels.append(
                QuoteLevelTarget(
                    level_index=level_index,
                    price=round_to_tick(level_px, tick_size),
                    size=level_size,
                    queue_ahead_lots=queue_ahead_lots + level_index * best_size,
                    queue_share=max(queue_share / (level_index + 1), 0.0),
                    enabled=True,
                )
            )
        return tuple(levels)


def build_quote_plan(
    *,
    symbol_state: SymbolState,
    params: SymbolStrategyParameters,
    inventory_lots: int,
    gate: QuoteGateDecision,
    tick_size: float,
) -> MarketMakingQuotePlan:
    fair_value, imbalance_shift = compute_fair_value(
        symbol_state,
        microprice_weight=params.microprice_weight,
        imbalance_shift_ticks=params.imbalance_shift_ticks,
        tick_size=tick_size,
        microprice_value=params.local_microprice,
        imbalance_value=params.local_depth_imbalance,
        drift_shift_ticks=params.global_drift_shift_ticks,
    )
    fair_value += params.pair_spread_shift_ticks * tick_size
    front_shape_shift = params.front_shape_shift_ticks * tick_size
    fair_value += front_shape_shift
    inventory_skew = compute_inventory_skew(
        inventory_lots,
        tick_size=tick_size,
        skew_ticks_per_lot=params.cj_inventory_skew_ticks_per_lot,
        max_skew_ticks=params.inventory_skew_cap_ticks,
        inventory_limit_lots=params.inventory_limit_lots,
    )
    half_width = compute_half_width(
        symbol_state,
        tick_size=tick_size,
        min_half_spread_ticks=params.spread_floor_ticks,
        max_half_spread_ticks=params.spread_ceiling_ticks,
        model_half_width_ticks=params.glft_half_width_ticks,
    )
    # Per-side widths must be known before capping center shift so the cap is
    # sized to the narrower side — prevents the shift from exceeding whichever
    # half-width is smaller when adverse pressure makes the two sides asymmetric.
    bid_half_width = half_width * params.bid_width_multiplier
    ask_half_width = half_width * params.ask_width_multiplier
    # Cap total center shift to the narrower of the two half-widths.
    _max_center_shift = min(bid_half_width, ask_half_width)
    _raw_center_shift = (
        params.drift_inventory_bias_ticks * tick_size
        + params.physics_momentum_shift_ticks * tick_size
        + params.noise_fade_shift_ticks * tick_size
    )
    _center_shift = max(-_max_center_shift, min(_raw_center_shift, _max_center_shift))
    # `center` is the symmetric fair-value reference (used for sweep exit, traces).
    # Inventory skew is applied asymmetrically: rather than shifting both sides
    # together, we only move the side we want to trade toward.
    # Long inventory (skew > 0): lower asks only — we want to sell, not buy more.
    # Short inventory (skew < 0): raise bids only — we want to buy, not sell more.
    center = fair_value + _center_shift
    # Compute raw quotes, then clamp to ensure we never cross the spread.
    # A bid above the best ask (or ask below the best bid) would fill immediately
    # as a taker order, picking up adverse selection and driving steady bleed.
    nbbo_bid = symbol_state.best_price.best_bid_px
    nbbo_ask = symbol_state.best_price.best_ask_px
    bid_px = round_to_tick(
        max(center - bid_half_width + min(inventory_skew, 0.0), tick_size),
        tick_size,
    )
    if nbbo_ask > 0.0:
        bid_px = min(bid_px, round_to_tick(nbbo_ask - tick_size, tick_size))
    ask_px = round_to_tick(
        max(center + ask_half_width - max(inventory_skew, 0.0), bid_px + tick_size),
        tick_size,
    )
    if nbbo_bid > 0.0:
        ask_px = max(ask_px, round_to_tick(nbbo_bid + tick_size, tick_size))
    raw_bid_size = (
        params.quote_size_lots
        * params.allocation_weight
        * params.pace_multiplier
        * params.bid_size_multiplier
    )
    raw_ask_size = (
        params.quote_size_lots
        * params.allocation_weight
        * params.pace_multiplier
        * params.ask_size_multiplier
    )
    bid_size = max(int(round(raw_bid_size)), 1)
    ask_size = max(int(round(raw_ask_size)), 1)
    # Asymmetric inventory limits from long-MA drift bias.
    # inventory_target_lots > 0 → prefer longs: relax bid limit, tighten ask limit.
    # inventory_target_lots < 0 → prefer shorts: relax ask limit, tighten bid limit.
    # Zero out in flatten_only_mode — we're trying to reduce exposure, not add bias.
    target = 0 if params.flatten_only_mode else params.inventory_target_lots
    effective_bid_limit = params.inventory_limit_lots + max(target, 0)
    effective_ask_limit = params.inventory_limit_lots + max(-target, 0)
    enable_bid = (
        gate.allow_quotes
        and params.symbol_enable_flag
        and params.bid_enable_flag
        and inventory_lots < effective_bid_limit
        and bid_size > 0
    )
    enable_ask = (
        gate.allow_quotes
        and params.symbol_enable_flag
        and params.ask_enable_flag
        and inventory_lots > -effective_ask_limit
        and ask_size > 0
    )
    plan = MarketMakingQuotePlan(
        bid_px=bid_px,
        ask_px=ask_px,
        bid_size=bid_size,
        ask_size=ask_size,
        enable_bid=enable_bid,
        enable_ask=enable_ask,
        flatten_mode=False,
        taker_overlay_mode=False,
        reason=_base_reason(gate=gate, params=params),
        fair_value=fair_value,
        center=center,
        inventory_skew=inventory_skew,
        imbalance_shift=imbalance_shift,
        front_shape_shift=front_shape_shift,
        half_width=half_width,
        bid_half_width=bid_half_width,
        ask_half_width=ask_half_width,
        raw_bid_size=raw_bid_size,
        raw_ask_size=raw_ask_size,
        flatten_target_lots=abs(inventory_lots),
        center_shift_applied=_center_shift,
    )
    _apply_execution_overlay(plan, params=params, inventory_lots=inventory_lots, gate=gate, tick_size=tick_size)
    _zero_disabled_sizes(plan)
    return plan


def _apply_execution_overlay(
    plan: MarketMakingQuotePlan,
    *,
    params: SymbolStrategyParameters,
    inventory_lots: int,
    gate: QuoteGateDecision,
    tick_size: float,
) -> None:
    if params.flatten_only_mode:
        plan.flatten_mode = True
        plan.reason = "close_flatten_only"
        if inventory_lots > 0:
            plan.ask_size = max(plan.flatten_target_lots, 1)
            plan.enable_bid = False
            plan.enable_ask = plan.ask_size > 0
        elif inventory_lots < 0:
            plan.bid_size = min(
                max(plan.flatten_target_lots, 1),
                max(params.max_affordable_bid_flatten_lots, 0),
            )
            plan.enable_bid = plan.bid_size > 0
            plan.enable_ask = False
        else:
            plan.enable_bid = False
            plan.enable_ask = False
        return

    # Standing sweep-reversion ladder: resting passive orders placed BELOW (bids)
    # and ABOVE (asks) the short MA, always active based on long-MA drift direction.
    # These run alongside normal two-sided quoting — they are supplementary deep
    # orders that fill when price spikes through them, not a quote replacement.
    # Setting taker_overlay_mode=True signals to_quote_target() to include sweep
    # ladder levels in addition to the normal level 0 quote.
    if (
        params.noise_fade_taker_mode
        and params.symbol_enable_flag
        and params.noise_fade_taker_size_lots > 0
        and (params.sweep_reversion_bid_levels or params.sweep_reversion_ask_levels)
    ):
        plan.taker_overlay_mode = True
        # Normal bid/ask quoting continues — sweep levels are additive via to_quote_target().

    # Sweep exit: unload accumulated sweep position at the short MA (the mass).
    # Price has returned to the mass (or the mass has tracked to the fill price),
    # so we place a passive limit at center to exit at a good price.
    # sweep_exit_side_sign: +1 = need to sell (exit long), -1 = need to buy (exit short).
    if (
        params.sweep_exit_mode
        and params.symbol_enable_flag
        # Sweep exit also bypasses the wide-spread gate — we need to unload
        # even when the book is temporarily thin during the reversion.
        # gate.allow_quotes is NOT checked here.
        and params.sweep_exit_lots > 0
    ):
        if params.sweep_exit_side_sign > 0.0:
            # Exit long: sell at fill_price + 1 tick (guarantee profit over entry).
            # Clamp to at least center so we never sell below fair value.
            # If fill_price is 0 (shouldn't happen but guard anyway), fall back to center.
            if params.sweep_fill_price > 0.0:
                exit_ask = round_to_tick(
                    max(params.sweep_fill_price + tick_size, plan.center),
                    tick_size,
                )
            else:
                exit_ask = round_to_tick(plan.center + tick_size, tick_size)
            plan.ask_px = exit_ask
            plan.ask_size = params.sweep_exit_lots
            plan.bid_size = 0
            plan.enable_bid = False
            plan.enable_ask = True
            plan.flatten_mode = False
            plan.taker_overlay_mode = True
            plan.reason = "sweep_exit_sell"
        else:
            # Exit short: buy at fill_price - 1 tick (guarantee profit over entry).
            # Clamp to at most center so we never buy above fair value.
            # If fill_price is 0 (shouldn't happen but guard anyway), fall back to center.
            if params.sweep_fill_price > 0.0:
                exit_bid = round_to_tick(
                    min(params.sweep_fill_price - tick_size, plan.center),
                    tick_size,
                )
            else:
                exit_bid = round_to_tick(plan.center - tick_size, tick_size)
            plan.bid_px = max(exit_bid, tick_size)
            plan.bid_size = params.sweep_exit_lots
            plan.ask_size = 0
            plan.enable_bid = True
            plan.enable_ask = False
            plan.flatten_mode = False
            plan.taker_overlay_mode = True
            plan.reason = "sweep_exit_buy"
        return

    if (
        not params.taker_overlay_mode
        or not params.symbol_enable_flag
        or not gate.allow_quotes
        or params.taker_overlay_size_lots <= 0
    ):
        return

    if inventory_lots > 0:
        plan.ask_size = min(
            max(params.taker_overlay_size_lots, 1),
            plan.flatten_target_lots,
        )
        plan.enable_bid = False
        plan.enable_ask = plan.ask_size > 0
    elif inventory_lots < 0:
        plan.bid_size = min(
            max(params.taker_overlay_size_lots, 1),
            plan.flatten_target_lots,
            max(params.max_affordable_bid_flatten_lots, 0),
        )
        plan.enable_bid = plan.bid_size > 0
        plan.enable_ask = False
    else:
        return

    plan.flatten_mode = True
    plan.taker_overlay_mode = True
    plan.reason = "inventory_taker_overlay"


def _zero_disabled_sizes(plan: MarketMakingQuotePlan) -> None:
    if not plan.enable_bid:
        plan.bid_size = 0
    if not plan.enable_ask:
        plan.ask_size = 0


def _base_reason(
    *,
    gate: QuoteGateDecision,
    params: SymbolStrategyParameters,
) -> str:
    if not gate.allow_quotes:
        return gate.reason
    if not params.symbol_enable_flag:
        return "symbol_disabled"
    return "inventory_aware_quote"


def _sweep_ladder_to_levels(
    ladder: tuple[tuple[float, int], ...],
    *,
    queue_ahead_lots: int,
    queue_share: float,
    index_offset: int = 0,
) -> tuple[QuoteLevelTarget, ...]:
    """Convert a sweep-reversion price/size ladder into QuoteLevelTarget tuples.

    Each level gets its own explicit price so the order router can place
    independent limit orders at each depth.  Queue-share is divided by level
    index+1 so deeper levels compete less aggressively for queue position.
    """
    return tuple(
        QuoteLevelTarget(
            level_index=index_offset + i,
            price=px,
            size=sz,
            queue_ahead_lots=queue_ahead_lots,
            queue_share=max(queue_share / (i + 1), 0.0),
            enabled=True,
        )
        for i, (px, sz) in enumerate(ladder)
    )


def _build_sweep_exit_levels(
    *,
    side: str,
    anchor_px: float,
    total_size: int,
    max_levels: int,
    queue_ahead_lots: int,
    queue_share: float,
    tick_size: float,
) -> tuple[QuoteLevelTarget, ...]:
    if anchor_px <= 0.0 or total_size <= 0:
        return (
            QuoteLevelTarget(
                level_index=0,
                price=anchor_px if anchor_px > 0.0 else None,
                size=0,
                queue_ahead_lots=queue_ahead_lots,
                queue_share=queue_share,
                enabled=False,
            ),
        )

    level_count = max(1, min(max_levels, total_size))
    base_size = total_size // level_count
    extra_size = total_size % level_count
    levels: list[QuoteLevelTarget] = []
    for level_index in range(level_count):
        level_size = base_size + (1 if level_index < extra_size else 0)
        if side == "bid":
            level_px = max(anchor_px - level_index * tick_size, tick_size)
        else:
            level_px = anchor_px + level_index * tick_size
        levels.append(
            QuoteLevelTarget(
                level_index=level_index,
                price=round_to_tick(level_px, tick_size),
                size=level_size,
                queue_ahead_lots=queue_ahead_lots + level_index * base_size,
                queue_share=max(queue_share / (level_index + 1), 0.0),
                enabled=level_size > 0,
            )
        )
    return tuple(levels)


def _side_front_edge(
    side: str,
    params: SymbolStrategyParameters,
) -> float:
    side_alpha_tilt = (
        -params.side_size_tilt
        if side == "bid"
        else params.side_size_tilt
    )
    queue_share = (
        params.bid_queue_share
        if side == "bid"
        else params.ask_queue_share
    )
    return max(
        min(
            0.55 * side_alpha_tilt
            + 0.30 * (2.0 * queue_share - 1.0)
            + 0.15 * (2.0 * params.entropy_confidence_score - 1.0),
            1.0,
        ),
        -1.0,
    )
