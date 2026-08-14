"""Toxicity-markout bookkeeping for the adaptive parameter pipeline.

Tracks fills that haven't reached their markout horizon yet, then scores how
adverse the mid-price move was once the delay has elapsed. This is the
toxicity signal that feeds `AdaptiveParameterProvider`'s width/size
multipliers.

Extracted from `AdaptiveParameterProvider` so this bookkeeping can be read
and tested in isolation, independent of quote-sizing math.
"""

from __future__ import annotations

from src.execution.order_state import OrderLedger, OrderLiquidity, OrderSide
from src.strategy.param_math import _clamp, _ewma
from src.strategy.param_types import AdaptiveHistoryConfig, PendingFillMarkout, SymbolAdaptiveState


class MarkoutTracker:
    """Stateless helper: all state lives on the `SymbolAdaptiveState` passed in."""

    @staticmethod
    def queue_pending(
        symbol: str,
        adaptive_state: SymbolAdaptiveState,
        *,
        order_ledger: OrderLedger | None,
        history_config: AdaptiveHistoryConfig,
    ) -> None:
        if order_ledger is None:
            return
        active_order_ids: set[str] = set()
        for audit in order_ledger.audits_for_symbol(symbol):
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
                    adaptive_state.ewma_passive_fill_arrival_ms = _ewma(
                        adaptive_state.ewma_passive_fill_arrival_ms,
                        fill_arrival_ms,
                        history_config.fill_arrival_age_alpha,
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
            - history_config.toxicity_max_pending_fills
        )
        if overflow > 0:
            del adaptive_state.pending_fill_markouts[:overflow]

    @staticmethod
    def consume_ready(
        adaptive_state: SymbolAdaptiveState,
        *,
        current_mid: float,
        now_ns: int,
        history_config: AdaptiveHistoryConfig,
    ) -> float:
        if current_mid <= 0.0 or not adaptive_state.pending_fill_markouts:
            return adaptive_state.ewma_toxicity_score

        ready_markout_pct: list[float] = []
        ready_scores: list[float] = []
        keep_pending: list[PendingFillMarkout] = []
        delay_ns = max(history_config.toxicity_markout_delay_ns, 0)
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
        adaptive_state.ewma_toxicity_markout_pct = _ewma(
            adaptive_state.ewma_toxicity_markout_pct,
            sum(ready_markout_pct) / len(ready_markout_pct),
            history_config.toxicity_alpha,
            initialized=adaptive_state.initialized,
        )
        return sum(ready_scores) / len(ready_scores)
