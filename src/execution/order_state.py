from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional

from src.core.units import FLOAT_EQ_TOLERANCE
from src.risk.inventory import Position

from src.execution.slippage import signed_shortfall_per_share, signed_shortfall_ticks


class OrderSide(str, Enum):
    BID = "bid"
    ASK = "ask"


class OrderIntentAction(str, Enum):
    KEEP = "keep"
    SUBMIT = "submit"
    CANCEL = "cancel"
    REPLACE = "replace"
    FLATTEN = "flatten"


class OrderLiquidity(str, Enum):
    LIMIT = "limit"
    MARKET = "market"
    UNKNOWN = "unknown"


LIMIT_REBATE_PER_SHARE = 0.002
MARKET_FEE_PER_SHARE = 0.003
LOT_SIZE_SHARES = 100


def _normalize_lots(raw_size: int, parent_size_lots: int = 0) -> int:
    if raw_size <= 0:
        return 0
    if parent_size_lots > 0:
        parent_size_shares = parent_size_lots * LOT_SIZE_SHARES
        if raw_size <= parent_size_lots:
            return raw_size
        if raw_size == parent_size_shares:
            return parent_size_lots
        if raw_size <= parent_size_shares:
            return max(1, (raw_size + LOT_SIZE_SHARES - 1) // LOT_SIZE_SHARES)
        if raw_size % LOT_SIZE_SHARES == 0:
            return raw_size // LOT_SIZE_SHARES
        return raw_size
    if raw_size >= LOT_SIZE_SHARES and raw_size % LOT_SIZE_SHARES == 0:
        return raw_size // LOT_SIZE_SHARES
    return raw_size


@dataclass(slots=True)
class QuoteLevelTarget:
    level_index: int
    price: Optional[float]
    size: int = 0
    queue_ahead_lots: int = 0
    queue_share: float = 0.0
    enabled: bool = True


@dataclass(slots=True)
class QuoteTarget:
    symbol: str
    bid_px: Optional[float]
    ask_px: Optional[float]
    bid_size: int = 0
    ask_size: int = 0
    bid_queue_ahead_lots: int = 0
    ask_queue_ahead_lots: int = 0
    bid_queue_share: float = 0.0
    ask_queue_share: float = 0.0
    enable_bid: bool = True
    enable_ask: bool = True
    flatten_mode: bool = False
    reason: str = ""
    bid_levels: tuple[QuoteLevelTarget, ...] = ()
    ask_levels: tuple[QuoteLevelTarget, ...] = ()

    def side_levels(self, side: OrderSide) -> tuple[QuoteLevelTarget, ...]:
        levels = self.bid_levels if side == OrderSide.BID else self.ask_levels
        if levels:
            return levels
        if side == OrderSide.BID:
            return (
                QuoteLevelTarget(
                    level_index=0,
                    price=self.bid_px,
                    size=self.bid_size,
                    queue_ahead_lots=self.bid_queue_ahead_lots,
                    queue_share=self.bid_queue_share,
                    enabled=self.enable_bid,
                ),
            )
        return (
            QuoteLevelTarget(
                level_index=0,
                price=self.ask_px,
                size=self.ask_size,
                queue_ahead_lots=self.ask_queue_ahead_lots,
                queue_share=self.ask_queue_share,
                enabled=self.enable_ask,
            ),
        )

    def level_target(
        self,
        side: OrderSide,
        level_index: int,
    ) -> QuoteLevelTarget:
        for level in self.side_levels(side):
            if level.level_index == level_index:
                return level
        return QuoteLevelTarget(
            level_index=level_index,
            price=None,
            size=0,
            queue_ahead_lots=0,
            queue_share=0.0,
            enabled=False,
        )


@dataclass(slots=True)
class WorkingOrder:
    symbol: str
    side: OrderSide
    order_id: str
    price: float
    size: int
    level_index: int = 0
    executed_size: int = 0
    status: str = "unknown"
    liquidity: OrderLiquidity = OrderLiquidity.UNKNOWN
    submitted_ts_ns: int = 0
    last_update_ts_ns: int = 0
    last_replace_request_ts_ns: int = 0
    inactive_ts_ns: int = 0
    pending_cancel: bool = False
    pending_replace_price: Optional[float] = None
    pending_replace_size: int = 0
    pending_replace_reason: str = ""

    @property
    def remaining_size(self) -> int:
        return max(self.size - self.executed_lots, 0)

    @property
    def executed_lots(self) -> int:
        return _normalize_lots(self.executed_size, self.size)

    @property
    def is_live(self) -> bool:
        return self.status not in {"canceled", "filled", "inactive", "rejected"}

    @property
    def estimated_reserved_buying_power(self) -> float:
        if (
            not self.is_live
            or self.liquidity != OrderLiquidity.LIMIT
            or self.status != "pending_new"
            or self.price <= 0.0
        ):
            return 0.0
        return self.remaining_size * LOT_SIZE_SHARES * self.price

    @property
    def has_pending_replace(self) -> bool:
        return self.pending_replace_price is not None and self.pending_replace_size > 0


@dataclass(slots=True)
class FillRecord:
    order_id: str
    symbol: str
    side: OrderSide
    executed_size: int
    executed_price: float
    status: str
    event_ts_ns: int
    broker_timestamp: str = ""
    execution_index: int = -1


@dataclass(slots=True)
class OrderAuditRecord:
    order_id: str
    symbol: str
    side: OrderSide
    level_index: int = 0
    submitted_ts_ns: int = 0
    last_update_ts_ns: int = 0
    submit_price: float = 0.0
    submit_size: int = 0
    decision_price: float = 0.0
    arrival_bid_px: float = 0.0
    arrival_ask_px: float = 0.0
    liquidity: OrderLiquidity = OrderLiquidity.UNKNOWN
    current_status: str = "unknown"
    fills: List[FillRecord] = field(default_factory=list)
    seen_execution_keys: set[str] = field(default_factory=set)
    seen_execution_lots_by_key: Dict[str, int] = field(default_factory=dict)
    seen_execution_count: int = 0
    cancel_requested_ts_ns: int = 0
    cancel_ack_ts_ns: int = 0
    replace_requested_ts_ns: int = 0
    cumulative_fill_lots: int = 0
    cumulative_fill_notional: float = 0.0
    cumulative_fill_count: int = 0
    cumulative_arrival_shortfall: float = 0.0
    cumulative_decision_shortfall: float = 0.0
    cumulative_arrival_slippage_ticks: float = 0.0
    cumulative_decision_slippage_ticks: float = 0.0

    @property
    def cumulative_executed_lots(self) -> int:
        return self.cumulative_fill_lots

    @property
    def cumulative_executed_shares(self) -> int:
        return self.cumulative_executed_lots * LOT_SIZE_SHARES

    @property
    def cumulative_executed_size(self) -> int:
        return self.cumulative_executed_lots

    @property
    def volume_weighted_fill_price(self) -> float:
        total_lots = self.cumulative_executed_lots
        if total_lots <= 0:
            return 0.0
        return self.cumulative_fill_notional / total_lots

    @property
    def estimated_rebate(self) -> float:
        if self.liquidity != OrderLiquidity.LIMIT:
            return 0.0
        return self.cumulative_executed_shares * LIMIT_REBATE_PER_SHARE

    @property
    def estimated_fee(self) -> float:
        if self.liquidity != OrderLiquidity.MARKET:
            return 0.0
        return self.cumulative_executed_shares * MARKET_FEE_PER_SHARE

    @property
    def estimated_net_fee(self) -> float:
        return self.estimated_fee - self.estimated_rebate

    @property
    def arrival_mid_price(self) -> float:
        if self.arrival_bid_px > 0.0 and self.arrival_ask_px > 0.0:
            return 0.5 * (self.arrival_bid_px + self.arrival_ask_px)
        return self.submit_price

    @property
    def arrival_spread(self) -> float:
        if self.arrival_bid_px > 0.0 and self.arrival_ask_px > 0.0:
            return max(self.arrival_ask_px - self.arrival_bid_px, 0.0)
        return 0.0

    @property
    def realized_arrival_slippage_ticks(self) -> float:
        if self.cumulative_fill_count <= 0:
            return 0.0
        return self.cumulative_arrival_slippage_ticks / self.cumulative_fill_count

    @property
    def realized_decision_slippage_ticks(self) -> float:
        if self.cumulative_fill_count <= 0:
            return 0.0
        return self.cumulative_decision_slippage_ticks / self.cumulative_fill_count

    @property
    def arrival_implementation_shortfall(self) -> float:
        return self.cumulative_arrival_shortfall

    @property
    def decision_implementation_shortfall(self) -> float:
        return self.cumulative_decision_shortfall

    def slippage_summary(self) -> dict[str, float]:
        return {
            "decision_price": self.decision_price,
            "arrival_bid_px": self.arrival_bid_px,
            "arrival_ask_px": self.arrival_ask_px,
            "arrival_mid_px": self.arrival_mid_price,
            "arrival_spread_px": self.arrival_spread,
            "fill_vwap_px": self.volume_weighted_fill_price,
            "realized_arrival_slippage_ticks": self.realized_arrival_slippage_ticks,
            "realized_decision_slippage_ticks": self.realized_decision_slippage_ticks,
            "arrival_implementation_shortfall": self.arrival_implementation_shortfall,
            "decision_implementation_shortfall": self.decision_implementation_shortfall,
        }

    def _normalized_fill_lots(self, raw_size: int) -> int:
        return _normalize_lots(raw_size, int(self.submit_size or 0))


@dataclass(slots=True)
class SymbolOrderLedger:
    symbol: str
    bid_order: Optional[WorkingOrder] = None
    ask_order: Optional[WorkingOrder] = None
    bid_level_orders: Dict[int, WorkingOrder] = field(default_factory=dict)
    ask_level_orders: Dict[int, WorkingOrder] = field(default_factory=dict)
    last_target: Optional[QuoteTarget] = None
    last_decision_ts_ns: int = 0

    def get_order(
        self,
        side: OrderSide,
        level_index: int = 0,
    ) -> Optional[WorkingOrder]:
        if side == OrderSide.BID:
            if level_index == 0:
                return self.bid_order
            return self.bid_level_orders.get(level_index)
        if level_index == 0:
            return self.ask_order
        return self.ask_level_orders.get(level_index)

    def set_order(
        self,
        side: OrderSide,
        order: Optional[WorkingOrder],
        level_index: int = 0,
    ) -> None:
        if side == OrderSide.BID:
            if level_index == 0:
                self.bid_order = order
            elif order is None:
                self.bid_level_orders.pop(level_index, None)
            else:
                self.bid_level_orders[level_index] = order
        else:
            if level_index == 0:
                self.ask_order = order
            elif order is None:
                self.ask_level_orders.pop(level_index, None)
            else:
                self.ask_level_orders[level_index] = order

    def side_level_indexes(self, side: OrderSide) -> tuple[int, ...]:
        if side == OrderSide.BID:
            indexes = set(self.bid_level_orders)
            if self.bid_order is not None:
                indexes.add(0)
            return tuple(sorted(indexes))
        indexes = set(self.ask_level_orders)
        if self.ask_order is not None:
            indexes.add(0)
        return tuple(sorted(indexes))

    def all_orders(self) -> tuple[WorkingOrder, ...]:
        orders: list[WorkingOrder] = []
        if self.bid_order is not None:
            orders.append(self.bid_order)
        orders.extend(
            order for _, order in sorted(self.bid_level_orders.items())
        )
        if self.ask_order is not None:
            orders.append(self.ask_order)
        orders.extend(
            order for _, order in sorted(self.ask_level_orders.items())
        )
        return tuple(orders)


@dataclass(slots=True)
class OrderCommand:
    action: OrderIntentAction
    symbol: str
    side: OrderSide
    level_index: int = 0
    price: Optional[float] = None
    size: int = 0
    order_id: str = ""
    reason: str = ""
    decision_price: float = 0.0
    arrival_bid_px: float = 0.0
    arrival_ask_px: float = 0.0


@dataclass(slots=True)
class OrderLedger:
    tick_size: float = 0.01
    by_symbol: Dict[str, SymbolOrderLedger] = field(default_factory=dict)
    orders_by_order_id: Dict[str, WorkingOrder] = field(default_factory=dict)
    audits_by_order_id: Dict[str, OrderAuditRecord] = field(default_factory=dict)
    audit_ids_by_symbol: Dict[str, set[str]] = field(default_factory=dict)
    max_completed_audits_retained: int = 2048
    min_completed_audit_retention_ns: int = 5_000_000_000
    archived_net_executed_shares_by_symbol: Dict[str, int] = field(default_factory=dict)
    archived_executed_shares_by_symbol: Dict[str, int] = field(default_factory=dict)
    archived_fill_count_by_symbol: Dict[str, int] = field(default_factory=dict)
    archived_cancel_count_by_symbol: Dict[str, int] = field(default_factory=dict)
    archived_passive_fill_count_by_symbol: Dict[str, int] = field(default_factory=dict)
    archived_estimated_rebate_by_symbol: Dict[str, float] = field(default_factory=dict)
    archived_estimated_fee_by_symbol: Dict[str, float] = field(default_factory=dict)
    session_executed_trades: int = 0
    session_executed_shares: int = 0
    session_fill_notional: float = 0.0
    session_fill_price_sum: float = 0.0
    session_arrival_shortfall: float = 0.0
    session_decision_shortfall: float = 0.0
    session_passive_fills: int = 0
    session_aggressive_fills: int = 0
    session_estimated_rebate: float = 0.0
    session_estimated_fee: float = 0.0
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )

    def ensure_symbol(self, symbol: str) -> SymbolOrderLedger:
        with self._lock:
            ledger = self.by_symbol.get(symbol)
            if ledger is None:
                ledger = SymbolOrderLedger(symbol=symbol)
                self.by_symbol[symbol] = ledger
            return ledger

    def find_by_order_id(self, order_id: str) -> Optional[WorkingOrder]:
        with self._lock:
            order = self.orders_by_order_id.get(order_id)
            if order is not None:
                return order
            for symbol_ledger in self.by_symbol.values():
                for order in symbol_ledger.all_orders():
                    if order and order.order_id == order_id:
                        self.orders_by_order_id[order_id] = order
                        return order
            return None

    def register_live_order(self, order: WorkingOrder) -> None:
        with self._lock:
            self.ensure_symbol(order.symbol).set_order(
                order.side,
                order,
                level_index=order.level_index,
            )
            self.orders_by_order_id[order.order_id] = order

    def reset_session_statistics(self) -> None:
        with self._lock:
            self.by_symbol.clear()
            self.orders_by_order_id.clear()
            self.audits_by_order_id.clear()
            self.audit_ids_by_symbol.clear()
            self.archived_net_executed_shares_by_symbol.clear()
            self.archived_executed_shares_by_symbol.clear()
            self.archived_fill_count_by_symbol.clear()
            self.archived_cancel_count_by_symbol.clear()
            self.archived_passive_fill_count_by_symbol.clear()
            self.archived_estimated_rebate_by_symbol.clear()
            self.archived_estimated_fee_by_symbol.clear()
            self.session_executed_trades = 0
            self.session_executed_shares = 0
            self.session_fill_notional = 0.0
            self.session_fill_price_sum = 0.0
            self.session_arrival_shortfall = 0.0
            self.session_decision_shortfall = 0.0
            self.session_passive_fills = 0
            self.session_aggressive_fills = 0
            self.session_estimated_rebate = 0.0
            self.session_estimated_fee = 0.0

    def live_orders(
        self,
        *,
        symbol: str | None = None,
        side: OrderSide | None = None,
    ) -> list[WorkingOrder]:
        with self._lock:
            live_orders = [
                order
                for order in self.orders_by_order_id.values()
                if order.is_live
                and (symbol is None or order.symbol == symbol)
                and (side is None or order.side == side)
            ]
            seen_object_ids = {id(order) for order in live_orders}
            seen_order_ids = {order.order_id for order in live_orders}
            for symbol_ledger in self.by_symbol.values():
                for order in symbol_ledger.all_orders():
                    if (
                        order is None
                        or not order.is_live
                        or id(order) in seen_object_ids
                        or order.order_id in seen_order_ids
                        or (symbol is not None and order.symbol != symbol)
                        or (side is not None and order.side != side)
                    ):
                        continue
                    live_orders.append(order)
                    seen_object_ids.add(id(order))
                    seen_order_ids.add(order.order_id)
            return live_orders

    def all_orders_snapshot(self) -> tuple[WorkingOrder, ...]:
        with self._lock:
            return tuple(self.orders_by_order_id.values())

    def audit_records_snapshot(self) -> tuple[OrderAuditRecord, ...]:
        with self._lock:
            return tuple(self.audits_by_order_id.values())

    def tracked_symbol_count(self) -> int:
        with self._lock:
            return len(self.by_symbol)

    def tracked_audit_count(self) -> int:
        with self._lock:
            return len(self.audits_by_order_id)

    def tracked_symbols_snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self.by_symbol)

    def get_net_executed_shares(self, symbol: str) -> int:
        with self._lock:
            net_shares = self.archived_net_executed_shares_by_symbol.get(symbol, 0)
            for audit in self.audits_for_symbol(symbol):
                if audit is None:
                    continue
                signed_multiplier = 1 if audit.side == OrderSide.BID else -1
                live_order = self.find_by_order_id(audit.order_id)
                visible_executed_shares = 0
                if live_order is not None and live_order.symbol == audit.symbol:
                    visible_executed_shares = (
                        live_order.executed_lots * LOT_SIZE_SHARES
                    )
                net_shares += signed_multiplier * max(
                    audit.cumulative_executed_shares,
                    visible_executed_shares,
                )
            return net_shares

    def audits_for_symbol(self, symbol: str) -> list[OrderAuditRecord]:
        with self._lock:
            return [
                audit
                for order_id in self.audit_ids_by_symbol.get(symbol, set())
                if (audit := self.audits_by_order_id.get(order_id)) is not None
            ]

    def get_net_executed_lots(self, symbol: str) -> int:
        return int(self.get_net_executed_shares(symbol) / LOT_SIZE_SHARES)

    def apply_position_baseline(
        self,
        broker_positions: Mapping[str, Position],
        *,
        symbols: Iterable[str] | None = None,
    ) -> dict[str, int]:
        with self._lock:
            baseline_offsets_shares: dict[str, int] = {}
            target_symbols = set(symbols) if symbols is not None else set(broker_positions)
            if symbols is None:
                target_symbols.update(self.by_symbol)
            for symbol in target_symbols:
                broker_position = broker_positions.get(symbol)
                broker_net_shares = (
                    broker_position.net_shares if broker_position is not None else 0
                )
                local_net_shares = self.get_net_executed_shares(symbol)
                offset_shares = broker_net_shares - local_net_shares
                if offset_shares == 0:
                    continue
                self.archived_net_executed_shares_by_symbol[symbol] = (
                    self.archived_net_executed_shares_by_symbol.get(symbol, 0)
                    + offset_shares
                )
                baseline_offsets_shares[symbol] = offset_shares
            return baseline_offsets_shares

    def ensure_audit(
        self,
        *,
        order_id: str,
        symbol: str,
        side: OrderSide,
        level_index: int = 0,
        submitted_ts_ns: int = 0,
        submit_price: float = 0.0,
        submit_size: int = 0,
        decision_price: float = 0.0,
        arrival_bid_px: float = 0.0,
        arrival_ask_px: float = 0.0,
        liquidity: OrderLiquidity = OrderLiquidity.UNKNOWN,
    ) -> OrderAuditRecord:
        with self._lock:
            audit = self.audits_by_order_id.get(order_id)
            if audit is None:
                audit = OrderAuditRecord(
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    level_index=level_index,
                    submitted_ts_ns=submitted_ts_ns,
                    last_update_ts_ns=submitted_ts_ns,
                    submit_price=submit_price,
                    submit_size=submit_size,
                    decision_price=decision_price,
                    arrival_bid_px=arrival_bid_px,
                    arrival_ask_px=arrival_ask_px,
                    liquidity=liquidity,
                )
                self.audits_by_order_id[order_id] = audit
                self.audit_ids_by_symbol.setdefault(symbol, set()).add(order_id)
            elif (
                audit.liquidity == OrderLiquidity.UNKNOWN
                and liquidity != OrderLiquidity.UNKNOWN
            ):
                audit.liquidity = liquidity
            if audit.level_index == 0 and level_index > 0:
                audit.level_index = level_index
            if audit.decision_price <= 0.0 and decision_price > 0.0:
                audit.decision_price = decision_price
            if audit.arrival_bid_px <= 0.0 and arrival_bid_px > 0.0:
                audit.arrival_bid_px = arrival_bid_px
            if audit.arrival_ask_px <= 0.0 and arrival_ask_px > 0.0:
                audit.arrival_ask_px = arrival_ask_px
            return audit

    def append_fill(self, fill: FillRecord) -> None:
        with self._lock:
            if fill.executed_size <= 0 or fill.executed_price <= 0.0:
                return
            audit = self.ensure_audit(
                order_id=fill.order_id,
                symbol=fill.symbol,
                side=fill.side,
            )
            fill_key = self._fill_key(fill)
            normalized_fill_lots = audit._normalized_fill_lots(fill.executed_size)
            previous_fill_lots = audit.seen_execution_lots_by_key.get(fill_key, 0)
            fill_lots = max(normalized_fill_lots - previous_fill_lots, 0)
            if fill_lots <= 0:
                audit.seen_execution_keys.add(fill_key)
                audit.seen_execution_lots_by_key[fill_key] = max(
                    previous_fill_lots,
                    normalized_fill_lots,
                )
                if fill.execution_index >= 0:
                    audit.seen_execution_count = max(
                        audit.seen_execution_count,
                        fill.execution_index + 1,
                    )
                return
            if fill_key in audit.seen_execution_keys:
                audit.seen_execution_lots_by_key[fill_key] = max(
                    previous_fill_lots,
                    normalized_fill_lots,
                )

            # Fallback guard for legacy fills that may not have broker timestamps or indexes.
            duplicate = fill_key not in audit.seen_execution_keys and any(
                (
                    existing.execution_index == fill.execution_index
                    and fill.execution_index >= 0
                )
                or (
                    existing.broker_timestamp == fill.broker_timestamp
                    and bool(fill.broker_timestamp)
                    and existing.executed_size == fill.executed_size
                    and abs(existing.executed_price - fill.executed_price) < FLOAT_EQ_TOLERANCE
                    and existing.status == fill.status
                )
                for existing in audit.fills
            )
            if duplicate:
                audit.seen_execution_keys.add(fill_key)
                audit.seen_execution_lots_by_key[fill_key] = max(
                    previous_fill_lots,
                    normalized_fill_lots,
                )
                return

            fill = FillRecord(
                order_id=fill.order_id,
                symbol=fill.symbol,
                side=fill.side,
                executed_size=fill_lots,
                executed_price=fill.executed_price,
                status=fill.status,
                event_ts_ns=fill.event_ts_ns,
                broker_timestamp=fill.broker_timestamp,
                execution_index=fill.execution_index,
            )
            audit.fills.append(fill)
            fill_shares = fill_lots * LOT_SIZE_SHARES
            arrival_shortfall = (
                signed_shortfall_per_share(
                    side=fill.side,
                    fill_price=fill.executed_price,
                    benchmark_price=audit.arrival_mid_price,
                )
                * fill_shares
            )
            decision_shortfall = (
                signed_shortfall_per_share(
                    side=fill.side,
                    fill_price=fill.executed_price,
                    benchmark_price=audit.decision_price,
                )
                * fill_shares
            )
            audit.cumulative_fill_lots += fill_lots
            audit.cumulative_fill_notional += fill_lots * fill.executed_price
            audit.cumulative_fill_count += 1
            audit.cumulative_arrival_shortfall += arrival_shortfall
            audit.cumulative_decision_shortfall += decision_shortfall
            audit.cumulative_arrival_slippage_ticks += signed_shortfall_ticks(
                side=fill.side,
                fill_price=fill.executed_price,
                benchmark_price=audit.arrival_mid_price,
                tick_size=self.tick_size,
            )
            audit.cumulative_decision_slippage_ticks += signed_shortfall_ticks(
                side=fill.side,
                fill_price=fill.executed_price,
                benchmark_price=audit.decision_price,
                tick_size=self.tick_size,
            )
            audit.seen_execution_keys.add(fill_key)
            audit.seen_execution_lots_by_key[fill_key] = normalized_fill_lots
            if fill.execution_index >= 0:
                audit.seen_execution_count = max(
                    audit.seen_execution_count,
                    fill.execution_index + 1,
                )
            audit.last_update_ts_ns = fill.event_ts_ns
            audit.current_status = fill.status

            self.session_executed_trades += 1
            self.session_executed_shares += fill_shares
            self.session_fill_notional += fill_shares * fill.executed_price
            self.session_fill_price_sum += fill.executed_price
            self.session_arrival_shortfall += arrival_shortfall
            self.session_decision_shortfall += decision_shortfall
            if audit.liquidity == OrderLiquidity.LIMIT:
                self.session_passive_fills += 1
                self.session_estimated_rebate += fill_shares * LIMIT_REBATE_PER_SHARE
            elif audit.liquidity == OrderLiquidity.MARKET:
                self.session_aggressive_fills += 1
                self.session_estimated_fee += fill_shares * MARKET_FEE_PER_SHARE

    def fill_count(self, symbol: str) -> int:
        with self._lock:
            total = self.archived_fill_count_by_symbol.get(symbol, 0)
            for audit in self.audits_for_symbol(symbol):
                total += audit.cumulative_fill_count
            return total

    def cancel_count(self, symbol: str) -> int:
        with self._lock:
            total = self.archived_cancel_count_by_symbol.get(symbol, 0)
            for audit in self.audits_for_symbol(symbol):
                if (
                    audit.cancel_requested_ts_ns > 0
                    and audit.replace_requested_ts_ns <= 0
                ):
                    total += 1
            return total

    def total_fill_count(self) -> int:
        with self._lock:
            return self.session_executed_trades

    def total_executed_shares(self) -> int:
        with self._lock:
            return self.session_executed_shares

    def passive_fill_count(self) -> int:
        with self._lock:
            return self.session_passive_fills

    def aggressive_fill_count(self) -> int:
        with self._lock:
            return self.session_aggressive_fills

    @property
    def session_fill_vwap(self) -> float:
        with self._lock:
            if self.session_executed_shares <= 0:
                return 0.0
            return self.session_fill_notional / self.session_executed_shares

    @property
    def session_fill_twap(self) -> float:
        with self._lock:
            if self.session_executed_trades <= 0:
                return 0.0
            return self.session_fill_price_sum / self.session_executed_trades

    @property
    def session_arrival_shortfall_total(self) -> float:
        with self._lock:
            return self.session_arrival_shortfall

    @property
    def session_decision_shortfall_total(self) -> float:
        with self._lock:
            return self.session_decision_shortfall

    def symbol_fee_stats(self, symbol: str) -> tuple[float, float, float]:
        with self._lock:
            estimated_rebate = self.archived_estimated_rebate_by_symbol.get(symbol, 0.0)
            estimated_fee = self.archived_estimated_fee_by_symbol.get(symbol, 0.0)
            passive_fills = self.archived_passive_fill_count_by_symbol.get(symbol, 0)
            total_fills = self.archived_fill_count_by_symbol.get(symbol, 0)
            for audit in self.audits_for_symbol(symbol):
                estimated_rebate += audit.estimated_rebate
                estimated_fee += audit.estimated_fee
                fill_count = audit.cumulative_fill_count
                total_fills += fill_count
                if audit.liquidity == OrderLiquidity.LIMIT:
                    passive_fills += fill_count
            passive_fill_ratio = passive_fills / total_fills if total_fills > 0 else 0.0
            return estimated_rebate, estimated_fee, passive_fill_ratio

    def archive_completed_audits(self, *, now_ns: int | None = None) -> None:
        with self._lock:
            if self.max_completed_audits_retained <= 0:
                return
            if len(self.audits_by_order_id) <= self.max_completed_audits_retained:
                return
            ts_ns = now_ns if now_ns is not None else 0

            active_slot_ids = {
                order.order_id
                for symbol_ledger in self.by_symbol.values()
                for order in symbol_ledger.all_orders()
                if order is not None
            }
            archive_candidates = [
                audit
                for order_id, audit in self.audits_by_order_id.items()
                if order_id not in active_slot_ids
                and audit.current_status in {"filled", "canceled", "inactive", "rejected"}
                and (
                    ts_ns <= 0
                    or audit.last_update_ts_ns <= 0
                    or ts_ns - audit.last_update_ts_ns >= self.min_completed_audit_retention_ns
                )
            ]
            if not archive_candidates:
                return

            archive_candidates.sort(key=lambda audit: audit.last_update_ts_ns)
            excess = len(self.audits_by_order_id) - self.max_completed_audits_retained
            for audit in archive_candidates[:excess]:
                signed_multiplier = 1 if audit.side == OrderSide.BID else -1
                self.archived_net_executed_shares_by_symbol[audit.symbol] = (
                    self.archived_net_executed_shares_by_symbol.get(audit.symbol, 0)
                    + signed_multiplier * audit.cumulative_executed_shares
                )
                self.archived_executed_shares_by_symbol[audit.symbol] = (
                    self.archived_executed_shares_by_symbol.get(audit.symbol, 0)
                    + audit.cumulative_executed_shares
                )
                self.archived_fill_count_by_symbol[audit.symbol] = (
                    self.archived_fill_count_by_symbol.get(audit.symbol, 0)
                    + audit.cumulative_fill_count
                )
                if audit.cancel_requested_ts_ns > 0 and audit.replace_requested_ts_ns <= 0:
                    self.archived_cancel_count_by_symbol[audit.symbol] = (
                        self.archived_cancel_count_by_symbol.get(audit.symbol, 0)
                        + 1
                    )
                if audit.liquidity == OrderLiquidity.LIMIT:
                    self.archived_passive_fill_count_by_symbol[audit.symbol] = (
                        self.archived_passive_fill_count_by_symbol.get(audit.symbol, 0)
                        + audit.cumulative_fill_count
                    )
                self.archived_estimated_rebate_by_symbol[audit.symbol] = (
                    self.archived_estimated_rebate_by_symbol.get(audit.symbol, 0.0)
                    + audit.estimated_rebate
                )
                self.archived_estimated_fee_by_symbol[audit.symbol] = (
                    self.archived_estimated_fee_by_symbol.get(audit.symbol, 0.0)
                    + audit.estimated_fee
                )
                self.audits_by_order_id.pop(audit.order_id, None)
                self.orders_by_order_id.pop(audit.order_id, None)
                symbol_audit_ids = self.audit_ids_by_symbol.get(audit.symbol)
                if symbol_audit_ids is not None:
                    symbol_audit_ids.discard(audit.order_id)
                    if not symbol_audit_ids:
                        self.audit_ids_by_symbol.pop(audit.symbol, None)

    @staticmethod
    def _fill_key(fill: FillRecord) -> str:
        if fill.execution_index >= 0:
            return f"idx:{fill.execution_index}"
        if fill.broker_timestamp:
            return (
                f"ts:{fill.broker_timestamp}|sz:{fill.executed_size}|px:{fill.executed_price:.12f}"
                f"|st:{fill.status}"
            )
        return f"fallback:{fill.executed_size}|{fill.executed_price:.12f}|{fill.status}"

    @property
    def estimated_total_rebate(self) -> float:
        with self._lock:
            return self.session_estimated_rebate

    @property
    def estimated_total_fee(self) -> float:
        with self._lock:
            return self.session_estimated_fee

    @property
    def estimated_total_net_fee(self) -> float:
        with self._lock:
            return self.session_estimated_fee - self.session_estimated_rebate

    @property
    def estimated_reserved_buying_power(self) -> float:
        return sum(
            order.estimated_reserved_buying_power
            for order in self.live_orders()
        )
