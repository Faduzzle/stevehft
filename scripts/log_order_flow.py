#!/usr/bin/env python3
"""Order lifecycle tracer — follow every order from birth to death.

For each order_id, prints a chronological timeline showing:
submit -> seen_live -> state_update -> fill -> cancel -> inactive

Usage:
    python scripts/log_order_flow.py runs/live_smoke/events.jsonl
    python scripts/log_order_flow.py runs/live_smoke/events.jsonl --symbol AAPL
    python scripts/log_order_flow.py runs/live_smoke/events.jsonl --order-id abc123
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from log_loader import EventIndex, Event, resolve_log_path

ORDER_LIFECYCLE_KINDS = {
    "order_command",
    "order_submitted",
    "order_submit_failed",
    "order_submit_skipped",
    "order_cancel_requested",
    "order_cancel_skipped",
    "order_cancel_failed",
    "order_replace_requested",
    "order_flatten_submitted",
    "order_flatten_failed",
    "order_flatten_skipped",
    "order_blocked_safe_mode",
    "order_blocked_risk",
    "order_seen_live",
    "order_state_update",
    "order_inactive",
    "order_fill",
    "reconciliation_action",
}


@dataclass(slots=True)
class OrderTimeline:
    order_id: str
    symbol: str = "?"
    side: str = "?"
    events: list[tuple[float, str, str]] = field(default_factory=list)

    def add(self, t: float, kind: str, detail: str) -> None:
        self.events.append((t, kind, detail))


def _extract_order_id(ev: Event) -> str | None:
    for key in ("order_id", "id"):
        val = ev.get(key)
        if val:
            return str(val)
    fill = ev.get("fill", {})
    if isinstance(fill, dict) and fill.get("order_id"):
        return str(fill["order_id"])
    command = ev.get("command", {})
    if isinstance(command, dict) and command.get("order_id"):
        return str(command["order_id"])
    return None


def _extract_symbol(ev: Event) -> str:
    for key in ("symbol",):
        val = ev.get(key)
        if val:
            return str(val)
    fill = ev.get("fill", {})
    if isinstance(fill, dict) and fill.get("symbol"):
        return str(fill["symbol"])
    command = ev.get("command", {})
    if isinstance(command, dict) and command.get("symbol"):
        return str(command["symbol"])
    return "?"


def _extract_side(ev: Event) -> str:
    for key in ("side",):
        val = ev.get(key)
        if val:
            return str(val)
    fill = ev.get("fill", {})
    if isinstance(fill, dict) and fill.get("side"):
        return str(fill["side"])
    command = ev.get("command", {})
    if isinstance(command, dict) and command.get("side"):
        return str(command["side"])
    return "?"


def _detail(ev: Event) -> str:
    parts: list[str] = []
    for key in ("price", "size", "status", "executed_size", "executed_price", "reason", "mode", "action"):
        val = ev.get(key)
        if val is not None:
            parts.append(f"{key}={val}")
    fill = ev.get("fill", {})
    if isinstance(fill, dict):
        for key in ("executed_price", "executed_size", "status"):
            val = fill.get(key)
            if val is not None:
                parts.append(f"fill.{key}={val}")
    slippage = ev.get("slippage", {})
    if isinstance(slippage, dict):
        for key in ("realized_arrival_slippage_ticks", "arrival_implementation_shortfall"):
            val = slippage.get(key)
            if val is not None and val != 0:
                parts.append(f"{key}={val:.4f}")
    command = ev.get("command", {})
    if isinstance(command, dict):
        for key in ("action", "price", "size", "reason"):
            val = command.get(key)
            if val is not None:
                parts.append(f"cmd.{key}={val}")
    return ", ".join(parts) if parts else ""


def build_timelines(idx: EventIndex, *, symbol_filter: str | None, order_id_filter: str | None) -> dict[str, OrderTimeline]:
    timelines: dict[str, OrderTimeline] = {}

    for ev in idx.events:
        if ev.kind not in ORDER_LIFECYCLE_KINDS:
            continue
        oid = _extract_order_id(ev)
        if not oid:
            continue
        if order_id_filter and oid != order_id_filter:
            continue

        sym = _extract_symbol(ev)
        if symbol_filter and sym != symbol_filter:
            continue

        tl = timelines.get(oid)
        if tl is None:
            tl = OrderTimeline(order_id=oid, symbol=sym, side=_extract_side(ev))
            timelines[oid] = tl
        if tl.symbol == "?":
            tl.symbol = sym

        t = idx.relative_s(ev)
        tl.add(t, ev.kind, _detail(ev))

    return timelines


def print_timeline(tl: OrderTimeline) -> None:
    print(f"\n--- Order {tl.order_id}  [{tl.symbol} {tl.side}] ---")
    for t, kind, detail in tl.events:
        short_kind = kind.replace("order_", "").replace("reconciliation_", "")
        line = f"  [{t:8.3f}s] {short_kind:30s}"
        if detail:
            line += f"  {detail}"
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="Order lifecycle tracer")
    parser.add_argument("logfile", nargs="?", default=None)
    parser.add_argument("--symbol", default=None, help="Filter by symbol")
    parser.add_argument("--order-id", default=None, help="Filter by order_id")
    args = parser.parse_args()

    path = resolve_log_path([args.logfile] if args.logfile else [])
    idx = EventIndex.build(path)

    timelines = build_timelines(idx, symbol_filter=args.symbol, order_id_filter=args.order_id)
    if not timelines:
        print("No order events found.")
        return

    sorted_tls = sorted(timelines.values(), key=lambda tl: tl.events[0][0] if tl.events else 0.0)
    print(f"Found {len(sorted_tls)} orders")
    for tl in sorted_tls:
        print_timeline(tl)
    print()


if __name__ == "__main__":
    main()