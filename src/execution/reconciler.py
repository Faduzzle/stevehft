from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Protocol

from src.core.units import FLOAT_EQ_TOLERANCE, NS_PER_MS
from .order_state import (
    FillRecord,
    OrderCommand,
    OrderIntentAction,
    OrderLedger,
    OrderLiquidity,
    OrderSide,
    QuoteTarget,
    WorkingOrder,
)
from src.risk.inventory import PortfolioLedger
from src.risk.kill_switch import KillSwitchController, ReconciliationHealth
from src.telemetry.logger import EventLoggerLike


class TraderLike(Protocol):
    def is_connected(self) -> bool: ...

    def get_waiting_list(self) -> list: ...

    def get_executed_orders(self, order_id: str) -> list: ...

    def get_portfolio_items(self) -> dict: ...

    def get_portfolio_summary(self): ...


@dataclass(slots=True)
class ReconciliationConfig:
    stale_order_after_ms: int = 750
    min_replace_interval_ms: int = 150
    new_order_grace_ms: int = 250
    inactive_order_hold_ms: int = 100
    max_waiting_list_sync_failures: int = 1
    max_execution_sync_failures: int = 3
    max_portfolio_sync_failures: int = 1
    max_parse_failures: int = 1
    max_price_drift_ticks: int = 1
    stale_keep_queue_share: float = 0.65
    tick_size: float = 0.01


@dataclass(slots=True)
class ReconciliationResult:
    commands: List[OrderCommand] = field(default_factory=list)
    dirty_symbols: List[str] = field(default_factory=list)


class Reconciler:
    def __init__(
        self,
        trader: TraderLike,
        order_ledger: OrderLedger,
        portfolio_ledger: PortfolioLedger,
        config: ReconciliationConfig,
        kill_switch: KillSwitchController | None = None,
        event_logger: Optional[EventLoggerLike] = None,
    ) -> None:
        self._trader = trader
        self._order_ledger = order_ledger
        self._portfolio_ledger = portfolio_ledger
        self._config = config
        self._kill_switch = kill_switch
        self._event_logger = event_logger
        self._waiting_list_sync_failures = 0
        self._execution_sync_failures = 0
        self._portfolio_sync_failures = 0
        self._parse_failures = 0

    def poll_server_state(self) -> None:
        now_ns = time.monotonic_ns()
        try:
            self._sync_waiting_orders(now_ns)
        except Exception as exc:
            self._waiting_list_sync_failures += 1
            if self._event_logger is not None:
                self._event_logger.log(
                    "waiting_list_sync_failed",
                    failures=self._waiting_list_sync_failures,
                    error=repr(exc),
                )
        else:
            self._waiting_list_sync_failures = 0
        self._sync_executions(now_ns)
        try:
            self._sync_portfolio(now_ns)
        except Exception as exc:
            self._portfolio_sync_failures += 1
            if self._event_logger is not None:
                self._event_logger.log(
                    "portfolio_sync_failed",
                    failures=self._portfolio_sync_failures,
                    error=repr(exc),
                )
        else:
            self._portfolio_sync_failures = 0
        self._order_ledger.archive_completed_audits(now_ns=now_ns)
        self._reconcile_position_mismatch_from_portfolio(now_ns=now_ns)
        if self._kill_switch is not None:
            health = self.reconciliation_health(now_ns=now_ns)
            self._kill_switch.update(health)
        if self._event_logger is not None:
            self._event_logger.log(
                "server_poll_complete",
                tracked_orders=self._order_ledger.tracked_audit_count(),
                tracked_symbols=self._order_ledger.tracked_symbol_count(),
                tracked_positions=len(self._portfolio_ledger.positions),
            )

    def reconciliation_health(self, *, now_ns: int | None = None) -> ReconciliationHealth:
        ts_ns = now_ns or time.monotonic_ns()
        waiting_stale_ms = 0
        for order in self._order_ledger.live_orders():
            if order.last_update_ts_ns > 0:
                waiting_stale_ms = max(
                    waiting_stale_ms,
                    int((ts_ns - order.last_update_ts_ns) / NS_PER_MS),
                )

        portfolio_stale_ms = 0
        if self._portfolio_ledger.summary.last_update_ts_ns > 0:
            portfolio_stale_ms = int(
                (ts_ns - self._portfolio_ledger.summary.last_update_ts_ns) / NS_PER_MS
            )

        position_mismatch_lots = 0
        symbols = set(self._order_ledger.tracked_symbols_snapshot())
        symbols.update(self._portfolio_ledger.positions)
        for symbol in symbols:
            broker_lots = self._portfolio_ledger.get_net_lots(symbol)
            local_executed_lots = self._order_ledger.get_net_executed_lots(symbol)
            position_mismatch_lots = max(
                position_mismatch_lots,
                abs(broker_lots - local_executed_lots),
            )

        try:
            broker_connected = bool(self._trader.is_connected())
        except Exception:
            broker_connected = False
        if (
            self._waiting_list_sync_failures
            >= self._config.max_waiting_list_sync_failures
        ):
            broker_connected = False
        if self._execution_sync_failures >= self._config.max_execution_sync_failures:
            broker_connected = False
        if self._portfolio_sync_failures >= self._config.max_portfolio_sync_failures:
            broker_connected = False
        if self._parse_failures >= self._config.max_parse_failures:
            broker_connected = False

        return ReconciliationHealth(
            waiting_list_stale_ms=waiting_stale_ms,
            portfolio_stale_ms=portfolio_stale_ms,
            position_mismatch_lots=position_mismatch_lots,
            broker_connected=broker_connected,
        )

    def _reconcile_position_mismatch_from_portfolio(
        self,
        *,
        now_ns: int,
    ) -> None:
        if self._kill_switch is None:
            return
        if (
            self._waiting_list_sync_failures > 0
            or self._execution_sync_failures > 0
            or self._portfolio_sync_failures > 0
            or self._parse_failures > 0
        ):
            return

        mismatched_symbols: dict[str, int] = {}
        rebaselined_symbols: set[str] = set()
        symbols = set(self._order_ledger.tracked_symbols_snapshot())
        symbols.update(self._portfolio_ledger.positions)
        for symbol in symbols:
            broker_lots = self._portfolio_ledger.get_net_lots(symbol)
            local_lots = self._order_ledger.get_net_executed_lots(symbol)
            mismatch_lots = broker_lots - local_lots
            if mismatch_lots == 0:
                continue
            mismatched_symbols[symbol] = mismatch_lots
            if not self._order_ledger.live_orders(symbol=symbol):
                rebaselined_symbols.add(symbol)
        if not mismatched_symbols:
            return
        if not rebaselined_symbols:
            return

        baseline_offsets_shares = self._order_ledger.apply_position_baseline(
            self._portfolio_ledger.positions,
            symbols=rebaselined_symbols,
        )
        if self._event_logger is not None and baseline_offsets_shares:
            self._event_logger.log(
                "position_mismatch_rebaseline",
                mismatched_symbols=mismatched_symbols,
                baseline_offsets_shares=baseline_offsets_shares,
                rebaselined_symbols=sorted(rebaselined_symbols),
                ts_ns=now_ns,
            )

    def build_reconciliation_actions(
        self,
        targets: List[QuoteTarget],
        now_ns: Optional[int] = None,
    ) -> ReconciliationResult:
        ts_ns = now_ns or time.monotonic_ns()
        result = ReconciliationResult()

        for target in targets:
            ledger = self._order_ledger.ensure_symbol(target.symbol)
            ledger.last_target = target
            ledger.last_decision_ts_ns = ts_ns
            if self._event_logger is not None:
                self._event_logger.log("strategy_target", target=target)

            bid_commands = self._reconcile_side_levels(
                target=target,
                side=OrderSide.BID,
                ledger=ledger,
                now_ns=ts_ns,
            )
            ask_commands = self._reconcile_side_levels(
                target=target,
                side=OrderSide.ASK,
                ledger=ledger,
                now_ns=ts_ns,
            )

            for command in (*bid_commands, *ask_commands):
                if command is not None:
                    result.commands.append(command)
                    if target.symbol not in result.dirty_symbols:
                        result.dirty_symbols.append(target.symbol)
                    if self._event_logger is not None:
                        self._event_logger.log("reconciliation_action", command=command)

        return result

    def _reconcile_side_levels(
        self,
        *,
        target: QuoteTarget,
        side: OrderSide,
        ledger,
        now_ns: int,
    ) -> list[OrderCommand]:
        target_level_indexes = {
            level.level_index
            for level in target.side_levels(side)
        }
        live_level_indexes = set(ledger.side_level_indexes(side))
        if target.flatten_mode:
            target_level_indexes.add(0)
            live_level_indexes.add(0)
        commands: list[OrderCommand] = []
        for level_index in sorted(target_level_indexes | live_level_indexes):
            command = self._reconcile_side(
                target=target,
                side=side,
                level_index=level_index,
                live_order=ledger.get_order(side, level_index=level_index),
                now_ns=now_ns,
            )
            if command is not None:
                commands.append(command)
        return commands

    def _reconcile_side(
        self,
        *,
        target: QuoteTarget,
        side: OrderSide,
        level_index: int,
        live_order: Optional[WorkingOrder],
        now_ns: int,
    ) -> Optional[OrderCommand]:
        level_target = target.level_target(side, level_index)
        enabled = level_target.enabled
        target_price = level_target.price
        target_size = level_target.size
        target_queue_share = level_target.queue_share

        if target.flatten_mode and target_size > 0:
            if level_index != 0 and live_order is None:
                return None
            if live_order is None or not live_order.is_live:
                if self._hold_after_inactive(
                    live_order,
                    now_ns,
                    flatten_mode=target.flatten_mode,
                ):
                    return None
                return OrderCommand(
                    action=OrderIntentAction.FLATTEN,
                    symbol=target.symbol,
                    side=side,
                    level_index=level_index,
                    size=target_size,
                    reason=target.reason or "flatten_only",
                    decision_price=target_price or 0.0,
                    arrival_bid_px=target.bid_px or 0.0,
                    arrival_ask_px=target.ask_px or 0.0,
                )
            if not live_order.pending_cancel:
                return OrderCommand(
                    action=OrderIntentAction.CANCEL,
                    symbol=target.symbol,
                    side=side,
                    level_index=level_index,
                    order_id=live_order.order_id,
                    reason="flatten_cancel_passive_before_market",
                    decision_price=target_price or 0.0,
                    arrival_bid_px=target.bid_px or 0.0,
                    arrival_ask_px=target.ask_px or 0.0,
                )
            return None

        if not enabled or target_price is None or target_size <= 0:
            self._clear_pending_replace(live_order)
            if live_order and live_order.is_live and not live_order.pending_cancel:
                return OrderCommand(
                    action=OrderIntentAction.CANCEL,
                    symbol=target.symbol,
                    side=side,
                    level_index=level_index,
                    order_id=live_order.order_id,
                    reason="target_disabled",
                    decision_price=target_price or 0.0,
                    arrival_bid_px=target.bid_px or 0.0,
                    arrival_ask_px=target.ask_px or 0.0,
                )
            return None

        if live_order is None or not live_order.is_live:
            if self._hold_after_inactive(
                live_order,
                now_ns,
                flatten_mode=target.flatten_mode,
            ):
                return None
            if live_order is not None and live_order.has_pending_replace:
                submit_price = live_order.pending_replace_price
                submit_size = live_order.pending_replace_size
                submit_reason = live_order.pending_replace_reason or target.reason or "replace_after_cancel_ack"
                live_order.pending_replace_price = None
                live_order.pending_replace_size = 0
                live_order.pending_replace_reason = ""
                return OrderCommand(
                    action=OrderIntentAction.SUBMIT,
                    symbol=target.symbol,
                    side=side,
                    level_index=level_index,
                    price=submit_price,
                    size=submit_size,
                    reason=submit_reason,
                    decision_price=submit_price or 0.0,
                    arrival_bid_px=target.bid_px or 0.0,
                    arrival_ask_px=target.ask_px or 0.0,
                )
            return OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol=target.symbol,
                side=side,
                level_index=level_index,
                price=target_price,
                size=target_size,
                reason=target.reason or "no_live_order",
                decision_price=target_price or 0.0,
                arrival_bid_px=target.bid_px or 0.0,
                arrival_ask_px=target.ask_px or 0.0,
            )

        if live_order.pending_cancel:
            if live_order.has_pending_replace:
                live_order.pending_replace_price = target_price
                live_order.pending_replace_size = target_size
                live_order.pending_replace_reason = target.reason or live_order.pending_replace_reason
            return None

        if (
            live_order.last_replace_request_ts_ns > 0
            and (now_ns - live_order.last_replace_request_ts_ns) / NS_PER_MS
            < self._config.min_replace_interval_ms
        ):
            return None

        freshness_ts_ns = live_order.last_update_ts_ns or live_order.submitted_ts_ns
        stale = (
            (now_ns - freshness_ts_ns) / NS_PER_MS
            >= self._config.stale_order_after_ms
        )
        price_drift_ticks = self._less_competitive_price_drift_ticks(
            side=side,
            live_price=live_order.price,
            target_price=target_price,
        )
        over_aggressive_drift_ticks = self._over_aggressive_price_drift_ticks(
            side=side,
            live_price=live_order.price,
            target_price=target_price,
        )
        size_mismatch = (
            target_size < live_order.remaining_size
            or target_size > live_order.remaining_size + 1
        )
        queue_position_good = (
            target_queue_share >= self._config.stale_keep_queue_share
            and price_drift_ticks <= 0.0
            and not size_mismatch
        )

        if (
            (stale and not queue_position_good)
            or price_drift_ticks > self._config.max_price_drift_ticks
            or over_aggressive_drift_ticks > self._config.max_price_drift_ticks
            or size_mismatch
        ):
            return OrderCommand(
                action=OrderIntentAction.REPLACE,
                symbol=target.symbol,
                side=side,
                level_index=level_index,
                price=target_price,
                size=target_size,
                order_id=live_order.order_id,
                reason="stale_or_off_target",
                decision_price=target_price or 0.0,
                arrival_bid_px=target.bid_px or 0.0,
                arrival_ask_px=target.ask_px or 0.0,
            )

        return None

    def _less_competitive_price_drift_ticks(
        self,
        *,
        side: OrderSide,
        live_price: float,
        target_price: float,
    ) -> float:
        if self._config.tick_size <= 0.0:
            return 0.0
        if side == OrderSide.BID:
            drift = target_price - live_price
        else:
            drift = live_price - target_price
        return max(drift, 0.0) / self._config.tick_size

    def _over_aggressive_price_drift_ticks(
        self,
        *,
        side: OrderSide,
        live_price: float,
        target_price: float,
    ) -> float:
        if self._config.tick_size <= 0.0:
            return 0.0
        if side == OrderSide.BID:
            drift = live_price - target_price
        else:
            drift = target_price - live_price
        return max(drift, 0.0) / self._config.tick_size

    def _hold_after_inactive(
        self,
        live_order: Optional[WorkingOrder],
        now_ns: int,
        *,
        flatten_mode: bool = False,
    ) -> bool:
        if flatten_mode:
            return False
        if live_order is None or live_order.is_live or live_order.inactive_ts_ns <= 0:
            return False
        if live_order.has_pending_replace:
            return False
        inactive_age_ms = (now_ns - live_order.inactive_ts_ns) / NS_PER_MS
        return inactive_age_ms < self._config.inactive_order_hold_ms

    @staticmethod
    def _clear_pending_replace(live_order: Optional[WorkingOrder]) -> None:
        if live_order is None or not live_order.has_pending_replace:
            return
        live_order.pending_replace_price = None
        live_order.pending_replace_size = 0
        live_order.pending_replace_reason = ""

    def _sync_waiting_orders(self, now_ns: int) -> None:
        waiting = self._trader.get_waiting_list()
        live_ids = set()
        parse_failures_this_poll = 0
        for raw in waiting:
            try:
                order_id = raw.id
                symbol = raw.symbol
                side = self._infer_side(raw.type)
                liquidity = self._infer_liquidity(raw.type)
                price = raw.price
                size = raw.size
                executed_size = raw.executed_size
                status = str(raw.status).lower()
            except (AttributeError, ValueError, TypeError) as exc:
                parse_failures_this_poll += 1
                if self._event_logger is not None:
                    self._event_logger.log(
                        "waiting_order_parse_failed",
                        raw_order_type=str(getattr(raw, "type", "")),
                        raw_order_id=str(getattr(raw, "id", "")),
                        error=repr(exc),
                    )
                continue

            live_ids.add(order_id)
            order = self._order_ledger.find_by_order_id(raw.id)
            if order is None:
                symbol_ledger = self._order_ledger.ensure_symbol(symbol)
                order = WorkingOrder(
                    symbol=symbol,
                    side=side,
                    order_id=order_id,
                    price=price,
                    size=size,
                    executed_size=executed_size,
                    status=status,
                    liquidity=liquidity,
                    submitted_ts_ns=now_ns,
                    last_update_ts_ns=now_ns,
                )
                self._order_ledger.register_live_order(order)
                audit = self._order_ledger.ensure_audit(
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    level_index=order.level_index,
                    submitted_ts_ns=now_ns,
                    submit_price=price,
                    submit_size=size,
                    liquidity=order.liquidity,
                )
                audit.current_status = status
                audit.last_update_ts_ns = now_ns
                if self._event_logger is not None:
                    self._event_logger.log(
                        "order_seen_live",
                        symbol=symbol,
                        side=side,
                        order_id=order_id,
                        price=price,
                        size=size,
                        executed_size=executed_size,
                        status=audit.current_status,
                    )
            else:
                previous_status = order.status
                previous_price = order.price
                previous_size = order.size
                previous_executed = order.executed_size
                order.price = price
                order.size = size
                order.executed_size = executed_size
                order.status = status
                order.liquidity = liquidity
                order.last_update_ts_ns = now_ns
                audit = self._order_ledger.ensure_audit(
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    level_index=order.level_index,
                    liquidity=order.liquidity,
                )
                audit.current_status = order.status
                audit.last_update_ts_ns = now_ns
                if self._event_logger is not None and (
                    previous_status != order.status
                    or abs(previous_price - order.price) > FLOAT_EQ_TOLERANCE
                    or previous_size != order.size
                    or previous_executed != order.executed_size
                ):
                    self._event_logger.log(
                        "order_state_update",
                        symbol=order.symbol,
                        side=order.side,
                        order_id=order.order_id,
                        price=order.price,
                        size=order.size,
                        executed_size=order.executed_size,
                        status=order.status,
                    )

        if parse_failures_this_poll > 0:
            self._parse_failures += parse_failures_this_poll
        else:
            self._parse_failures = 0

        for order in self._order_ledger.all_orders_snapshot():
            if order.order_id in live_ids or not order.is_live:
                continue
            if (
                order.status in {"pending_new", "pending_flatten"}
                and not order.pending_cancel
                and not order.has_pending_replace
                and order.submitted_ts_ns > 0
                and (now_ns - order.submitted_ts_ns) / NS_PER_MS
                < self._config.new_order_grace_ms
            ):
                continue
            order.status = "inactive"
            order.pending_cancel = False
            order.inactive_ts_ns = now_ns
            order.last_update_ts_ns = now_ns
            audit = self._order_ledger.ensure_audit(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                level_index=order.level_index,
                liquidity=order.liquidity,
            )
            audit.current_status = "inactive"
            audit.last_update_ts_ns = now_ns
            if self._event_logger is not None:
                self._event_logger.log(
                    "order_inactive",
                    symbol=order.symbol,
                    side=order.side,
                    order_id=order.order_id,
                )

    def _sync_executions(self, now_ns: int) -> None:
        sync_failures_this_poll = 0
        for audit in self._order_ledger.audit_records_snapshot():
            live_order = self._order_ledger.find_by_order_id(audit.order_id)
            if (
                audit.current_status in {"filled", "canceled", "inactive", "rejected"}
                and (
                    audit.seen_execution_count > 0
                    or (
                        audit.last_update_ts_ns > 0
                        and audit.last_update_ts_ns < now_ns
                    )
                )
                and (live_order is None or not live_order.is_live)
            ):
                continue
            try:
                executions = self._trader.get_executed_orders(audit.order_id)
            except Exception as exc:
                sync_failures_this_poll += 1
                if self._event_logger is not None:
                    self._event_logger.log(
                        "execution_sync_failed",
                        order_id=audit.order_id,
                        failures=self._execution_sync_failures + sync_failures_this_poll,
                        error=repr(exc),
                    )
                continue

            execution_count = len(executions)
            if execution_count > audit.seen_execution_count:
                start_index = audit.seen_execution_count
                execution_slice = list(
                    enumerate(executions[start_index:], start=start_index)
                )
            elif execution_count == audit.seen_execution_count:
                if execution_count <= 0:
                    continue
                execution_slice = [(execution_count - 1, executions[-1])]
            else:
                execution_slice = [(-1, raw_fill) for raw_fill in executions]

            for execution_index, raw_fill in execution_slice:
                try:
                    fill = FillRecord(
                        order_id=raw_fill.id,
                        symbol=raw_fill.symbol,
                        side=self._infer_side(raw_fill.type),
                        executed_size=raw_fill.executed_size,
                        executed_price=raw_fill.executed_price,
                        status=str(raw_fill.status).lower(),
                        event_ts_ns=now_ns,
                        broker_timestamp=str(getattr(raw_fill, "timestamp", "") or ""),
                        execution_index=execution_index,
                    )
                    liquidity = self._infer_liquidity(raw_fill.type)
                except (AttributeError, ValueError, TypeError) as exc:
                    sync_failures_this_poll += 1
                    if self._event_logger is not None:
                        self._event_logger.log(
                            "executed_order_parse_failed",
                            order_id=audit.order_id,
                            raw_order_type=str(getattr(raw_fill, "type", "")),
                            error=repr(exc),
                        )
                    continue

                before = len(audit.fills)
                self._order_ledger.ensure_audit(
                    order_id=fill.order_id,
                    symbol=fill.symbol,
                    side=fill.side,
                    liquidity=liquidity,
                )
                self._order_ledger.append_fill(fill)
                updated_audit = self._order_ledger.ensure_audit(
                    order_id=fill.order_id,
                    symbol=fill.symbol,
                    side=fill.side,
                    liquidity=liquidity,
                )
                after = len(updated_audit.fills) if updated_audit is not None else before
                if self._event_logger is not None and after > before:
                    self._event_logger.log(
                        "order_fill",
                        fill=fill,
                        level_index=(
                            updated_audit.level_index
                            if updated_audit is not None
                            else 0
                        ),
                        liquidity=(
                            updated_audit.liquidity.value
                            if updated_audit is not None
                            else liquidity.value
                        ),
                        slippage=(
                            updated_audit.slippage_summary()
                            if updated_audit is not None
                            else {}
                        ),
                    )
            audit.seen_execution_count = max(
                audit.seen_execution_count,
                execution_count,
            )

        if sync_failures_this_poll > 0:
            self._execution_sync_failures += sync_failures_this_poll
        else:
            self._execution_sync_failures = 0

    def _sync_portfolio(self, now_ns: int) -> None:
        broker_items = self._trader.get_portfolio_items()
        for symbol, item in broker_items.items():
            previous = self._portfolio_ledger.positions.get(symbol)
            self._portfolio_ledger.update_position(
                symbol,
                long_shares=item.get_long_shares(),
                short_shares=item.get_short_shares(),
                long_price=item.get_long_price(),
                short_price=item.get_short_price(),
                realized_pl=item.get_realized_pl(),
                ts_ns=now_ns,
            )
            updated = self._portfolio_ledger.positions[symbol]
            if self._event_logger is not None and (
                previous is None
                or previous.long_shares != updated.long_shares
                or previous.short_shares != updated.short_shares
                or abs(previous.realized_pl - updated.realized_pl) > FLOAT_EQ_TOLERANCE
            ):
                self._event_logger.log("position_update", position=updated)

        cleared_positions = self._portfolio_ledger.clear_missing_positions(
            broker_items.keys(),
            ts_ns=now_ns,
        )
        if self._event_logger is not None:
            for position in cleared_positions:
                self._event_logger.log("position_update", position=position)

        summary = self._trader.get_portfolio_summary()
        self._portfolio_ledger.summary.total_bp = summary.get_total_bp()
        self._portfolio_ledger.summary.total_shares = summary.get_total_shares()
        self._portfolio_ledger.summary.total_realized_pl = summary.get_total_realized_pl()
        self._portfolio_ledger.summary.last_update_ts_ns = now_ns
        if self._event_logger is not None:
            self._event_logger.log(
                "portfolio_snapshot",
                total_bp=self._portfolio_ledger.summary.total_bp,
                total_shares=self._portfolio_ledger.summary.total_shares,
                total_realized_pl=self._portfolio_ledger.summary.total_realized_pl,
                positions=self._portfolio_ledger.positions,
            )

    @staticmethod
    def _infer_side(raw_type: object) -> OrderSide:
        type_name = str(raw_type).upper()
        if "BUY" in type_name or "BID" in type_name:
            return OrderSide.BID
        if "SELL" in type_name or "ASK" in type_name:
            return OrderSide.ASK
        raise ValueError(f"unknown order side type: {raw_type!r}")

    @staticmethod
    def _infer_liquidity(raw_type: object) -> OrderLiquidity:
        type_name = str(raw_type).upper()
        if "MARKET" in type_name:
            return OrderLiquidity.MARKET
        if "LIMIT" in type_name:
            return OrderLiquidity.LIMIT
        return OrderLiquidity.UNKNOWN
