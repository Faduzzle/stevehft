#!/usr/bin/env python3
"""Session summary — the first thing to run after any session.

Prints a one-page overview: session duration, event counts, fill stats,
risk transitions, errors, and warnings.

Usage:
    python scripts/log_summary.py runs/live_smoke/events.jsonl
"""
from __future__ import annotations

import sys
from collections import Counter

from log_loader import EventIndex, resolve_log_path


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"{minutes}m {secs:.1f}s"


def print_header(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def section_overview(idx: EventIndex) -> None:
    print_header("SESSION OVERVIEW")
    print(f"  Total events     : {len(idx.events)}")
    print(f"  Duration         : {_fmt_duration(idx.duration_s)}")
    print(f"  Distinct kinds   : {len(idx.kinds())}")

    started = idx.of("app_started")
    if started:
        cfg = started[0].get("runtime_config", {})
        md = cfg.get("market_data", {})
        risk = cfg.get("risk", {})
        print(f"  Symbols          : {md.get('symbols', '?')}")
        print(f"  Update interval  : {md.get('update_interval_ms', '?')} ms")
        print(f"  Max pos/symbol   : {risk.get('max_position_lots_per_symbol', '?')} lots")
        print(f"  Max gross pos    : {risk.get('max_gross_position_lots', '?')} lots")


def section_event_counts(idx: EventIndex) -> None:
    print_header("EVENT COUNTS")
    for kind in idx.kinds():
        print(f"  {kind:45s} {idx.count(kind):>6d}")


def section_fill_economics(idx: EventIndex) -> None:
    print_header("FILL ECONOMICS")
    fills = idx.of("order_fill")
    if not fills:
        print("  (no fills)")
        return

    total_fills = len(fills)
    symbols: Counter[str] = Counter()
    for ev in fills:
        fill = ev.get("fill", {})
        symbols[fill.get("symbol", "?")] += 1

    metrics_list = idx.of("session_metrics") or idx.of("strategy_cycle_complete")
    if metrics_list:
        last = metrics_list[-1]
        m = last.get("metrics", {})
        passive = m.get("passive_fills", 0)
        aggressive = m.get("aggressive_fills", 0)
        rebates = m.get("estimated_rebates", 0.0)
        fees = m.get("estimated_fees", 0.0)
        net = m.get("estimated_net_fees", 0.0)
        arrival_short = m.get("arrival_shortfall", 0.0)
        decision_short = m.get("decision_shortfall", 0.0)
        vwap = m.get("fill_vwap", 0.0)

        print(f"  Total fills      : {total_fills}")
        print(f"  Passive fills    : {passive}")
        print(f"  Aggressive fills : {aggressive}")
        ratio = passive / max(passive + aggressive, 1)
        print(f"  Passive ratio    : {ratio:.1%}")
        print(f"  Est. rebates     : ${rebates:,.4f}")
        print(f"  Est. fees        : ${fees:,.4f}")
        print(f"  Est. net fees    : ${net:,.4f}")
        print(f"  Arrival shortfall: ${arrival_short:,.4f}")
        print(f"  Decision shortfll: ${decision_short:,.4f}")
        print(f"  Session VWAP     : ${vwap:,.4f}")
    else:
        print(f"  Total fills      : {total_fills}")

    print(f"\n  Fills by symbol:")
    for sym, cnt in symbols.most_common():
        print(f"    {sym:10s} {cnt:>5d}")


def section_order_lifecycle(idx: EventIndex) -> None:
    print_header("ORDER LIFECYCLE")
    submitted = idx.count("order_submitted")
    cancelled = idx.count("order_cancel_requested")
    replaced = idx.count("order_replace_requested")
    flattened = idx.count("order_flatten_submitted")
    blocked_safe = idx.count("order_blocked_safe_mode")
    blocked_risk = idx.count("order_blocked_risk")
    skipped = idx.count("order_submit_skipped") + idx.count("order_cancel_skipped")

    print(f"  Submitted        : {submitted}")
    print(f"  Cancel requested : {cancelled}")
    print(f"  Replace requested: {replaced}")
    print(f"  Flatten submitted: {flattened}")
    print(f"  Blocked (safe)   : {blocked_safe}")
    print(f"  Blocked (risk)   : {blocked_risk}")
    print(f"  Skipped          : {skipped}")

    fail_submit = idx.count("order_submit_failed")
    fail_cancel = idx.count("order_cancel_failed")
    fail_flatten = idx.count("order_flatten_failed")
    total_fail = fail_submit + fail_cancel + fail_flatten
    if total_fail > 0:
        print(f"\n  [!] Order failures: submit={fail_submit}, cancel={fail_cancel}, flatten={fail_flatten}")


def section_risk_transitions(idx: EventIndex) -> None:
    print_header("RISK / SAFE MODE")
    transitions = idx.of("safe_mode_transition")
    if not transitions:
        print("  No safe mode transitions (stayed NORMAL)")
        return

    for ev in transitions:
        t = idx.relative_s(ev)
        prev = ev.get("previous_mode", "?")
        nxt = ev.get("next_mode", "?")
        mismatch = ev.get("position_mismatch_lots", 0)
        print(f"  [{t:8.2f}s] {prev} -> {nxt}  (mismatch={mismatch} lots)")


def section_session_health(idx: EventIndex) -> None:
    print_header("SESSION HEALTH")
    connects = idx.of("session_connect")
    disconnects = idx.of("session_disconnect")
    reconnect_attempts = idx.of("session_reconnect_attempt")
    reconnect_failures = idx.of("session_reconnect_failed")
    resubscribes = idx.of("session_resubscribe_books")
    resub_failures = idx.of("session_resubscribe_failed")
    discovers = idx.of("session_discover_symbols")

    print(f"  Connects         : {len(connects)}")
    print(f"  Disconnects      : {len(disconnects)}")
    if reconnect_attempts:
        print(f"  Reconnect tries  : {len(reconnect_attempts)}")
        print(f"  Reconnect fails  : {len(reconnect_failures)}")
    if resubscribes:
        print(f"  Resubscriptions  : {len(resubscribes)}")
    if resub_failures:
        print(f"  Resub failures   : {len(resub_failures)}")
    if discovers:
        last = discovers[-1]
        syms = last.get("symbols", ())
        print(f"  Discovered syms  : {len(syms)}")

    subscribe_events = idx.of("session_subscribe_books")
    if subscribe_events:
        last_sub = subscribe_events[-1]
        syms = last_sub.get("symbols", ())
        all_books = last_sub.get("subscribe_all_books", False)
        print(f"  Subscribed syms  : {len(syms)}{' (all_books)' if all_books else ''}")


def section_errors_warnings(idx: EventIndex) -> None:
    print_header("ERRORS AND WARNINGS")
    error_kinds = [
        "waiting_list_sync_failed",
        "portfolio_sync_failed",
        "execution_sync_failed",
        "waiting_order_parse_failed",
        "executed_order_parse_failed",
        "order_submit_failed",
        "order_cancel_failed",
        "order_flatten_failed",
        "bootstrap_failed",
        "session_unsubscribe_failed",
        "session_health_check_failed",
        "session_list_subscribed_books_failed",
        "session_reconnect_failed",
        "session_resubscribe_failed",
        "session_last_trade_time_failed",
        "market_data_pre_refresh_failed",
    ]
    found_any = False
    for kind in error_kinds:
        count = idx.count(kind)
        if count > 0:
            found_any = True
            print(f"  [!] {kind}: {count}")
    if not found_any:
        print("  No errors or sync failures detected.")


def section_strategy_decisions(idx: EventIndex) -> None:
    print_header("STRATEGY DECISIONS")
    traces = idx.of("strategy_trace")
    if not traces:
        print("  (no strategy traces)")
        return

    reason_counts: Counter[str] = Counter()
    mode_counts: Counter[str] = Counter()
    for ev in traces:
        trace = ev.get("trace", ev.payload)
        diag = trace.get("diagnostics", {})
        reason_counts[trace.get("decision_reason", "?")] += 1
        mode_counts[diag.get("participation_mode", "?")] += 1

    print(f"  Total traces     : {len(traces)}")
    print(f"\n  Decision reasons:")
    for reason, cnt in reason_counts.most_common():
        print(f"    {reason:35s} {cnt:>5d}")
    print(f"\n  Participation modes:")
    for mode, cnt in mode_counts.most_common():
        print(f"    {mode:35s} {cnt:>5d}")


def section_timing(idx: EventIndex) -> None:
    print_header("CYCLE TIMING")
    polls = idx.of("app_poll_cycle")
    if len(polls) < 2:
        print("  (not enough poll cycles)")
        return

    deltas_ms = []
    for i in range(1, len(polls)):
        delta = (polls[i].ts_ns - polls[i - 1].ts_ns) / 1_000_000.0
        deltas_ms.append(delta)

    deltas_ms.sort()
    n = len(deltas_ms)
    mean = sum(deltas_ms) / n
    p50 = deltas_ms[n // 2]
    p95 = deltas_ms[int(n * 0.95)]
    p99 = deltas_ms[int(n * 0.99)]
    worst = deltas_ms[-1]

    print(f"  Cycles           : {len(polls)}")
    print(f"  Mean interval    : {mean:.1f} ms")
    print(f"  P50              : {p50:.1f} ms")
    print(f"  P95              : {p95:.1f} ms")
    print(f"  P99              : {p99:.1f} ms")
    print(f"  Worst            : {worst:.1f} ms")


def main() -> None:
    path = resolve_log_path()
    print(f"Loading: {path}")
    idx = EventIndex.build(path)

    section_overview(idx)
    section_event_counts(idx)
    section_session_health(idx)
    section_fill_economics(idx)
    section_order_lifecycle(idx)
    section_risk_transitions(idx)
    section_strategy_decisions(idx)
    section_timing(idx)
    section_errors_warnings(idx)
    print()


if __name__ == "__main__":
    main()