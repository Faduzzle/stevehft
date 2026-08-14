#!/usr/bin/env python3
"""Risk and safety audit — find dangerous patterns in the session log.

Checks for:
- Kill switch escalations and time spent in non-NORMAL modes
- Position mismatch events
- Order failures (submit, cancel, flatten)
- Sync failures (waiting list, execution, portfolio)
- Blocked orders and their reasons
- Queue position degradation from replaces
- Flatten behavior near session close
- Stale data warnings
- Any gaps in expected event sequences

Usage:
    python scripts/log_risk_audit.py runs/live_smoke/events.jsonl
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass

from log_loader import EventIndex, Event, resolve_log_path


@dataclass(slots=True)
class AuditFinding:
    severity: str  # CRITICAL, WARNING, INFO
    category: str
    message: str
    t_s: float = 0.0


def audit_safe_mode(idx: EventIndex) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    transitions = idx.of("safe_mode_transition")
    if not transitions:
        findings.append(AuditFinding("INFO", "safe_mode", "No transitions — stayed NORMAL entire session"))
        return findings

    time_in_mode: dict[str, float] = {}
    last_transition_ts = idx.first_ts_ns
    last_mode = "normal"

    for ev in transitions:
        duration_s = (ev.ts_ns - last_transition_ts) / 1_000_000_000.0
        time_in_mode[last_mode] = time_in_mode.get(last_mode, 0.0) + duration_s

        prev = ev.get("previous_mode", "?")
        nxt = ev.get("next_mode", "?")
        mismatch = ev.get("position_mismatch_lots", 0)
        t = idx.relative_s(ev)

        if nxt in ("kill_switch", "flatten_only"):
            findings.append(AuditFinding(
                "CRITICAL", "safe_mode",
                f"Escalated to {nxt} from {prev} (mismatch={mismatch} lots)", t
            ))
        elif nxt == "position_mismatch":
            findings.append(AuditFinding(
                "WARNING", "safe_mode",
                f"Position mismatch detected ({mismatch} lots)", t
            ))
        elif nxt == "degraded_reconcile":
            findings.append(AuditFinding(
                "WARNING", "safe_mode",
                f"Entered degraded reconciliation from {prev}", t
            ))

        last_transition_ts = ev.ts_ns
        last_mode = nxt

    # Final segment
    duration_s = (idx.last_ts_ns - last_transition_ts) / 1_000_000_000.0
    time_in_mode[last_mode] = time_in_mode.get(last_mode, 0.0) + duration_s

    for mode, secs in sorted(time_in_mode.items()):
        pct = 100.0 * secs / max(idx.duration_s, 0.001)
        if mode != "normal" and pct > 5.0:
            findings.append(AuditFinding(
                "WARNING", "safe_mode",
                f"Spent {secs:.1f}s ({pct:.1f}%) in {mode}"
            ))

    return findings


def audit_order_failures(idx: EventIndex) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    failure_kinds = {
        "order_submit_failed": "CRITICAL",
        "order_cancel_failed": "CRITICAL",
        "order_flatten_failed": "CRITICAL",
        "order_submit_skipped": "WARNING",
        "order_cancel_skipped": "INFO",
        "order_flatten_skipped": "WARNING",
    }
    for kind, severity in failure_kinds.items():
        events = idx.of(kind)
        if events:
            reasons = Counter(ev.get("reason", "unknown") for ev in events)
            for reason, cnt in reasons.most_common():
                findings.append(AuditFinding(
                    severity, "order_failure",
                    f"{kind}: {cnt}x reason={reason}"
                ))
    return findings


def audit_blocked_orders(idx: EventIndex) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for kind in ("order_blocked_safe_mode", "order_blocked_risk"):
        events = idx.of(kind)
        if not events:
            continue
        reasons = Counter()
        for ev in events:
            if kind == "order_blocked_safe_mode":
                reasons[ev.get("mode", "?")] += 1
            else:
                reasons[ev.get("reason", "?")] += 1
        for reason, cnt in reasons.most_common():
            findings.append(AuditFinding(
                "WARNING", "blocked_orders",
                f"{kind}: {cnt}x ({reason})"
            ))
    return findings


def audit_sync_failures(idx: EventIndex) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    sync_kinds = [
        "waiting_list_sync_failed",
        "portfolio_sync_failed",
        "execution_sync_failed",
        "waiting_order_parse_failed",
        "executed_order_parse_failed",
    ]
    for kind in sync_kinds:
        events = idx.of(kind)
        if events:
            findings.append(AuditFinding(
                "CRITICAL" if len(events) > 3 else "WARNING",
                "sync_failure",
                f"{kind}: {len(events)} occurrences"
            ))
            # Show first error
            first_err = events[0].get("error", "")
            if first_err:
                findings.append(AuditFinding(
                    "INFO", "sync_failure",
                    f"  First error: {first_err[:120]}"
                ))
    return findings


def audit_replace_churn(idx: EventIndex) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    replaces = idx.of("order_replace_requested")
    submits = idx.of("order_submitted")
    fills = idx.of("order_fill")

    if submits:
        replace_ratio = len(replaces) / max(len(submits), 1)
        if replace_ratio > 2.0:
            findings.append(AuditFinding(
                "WARNING", "churn",
                f"Replace/submit ratio is {replace_ratio:.1f}x — excessive quote churn, destroying queue position"
            ))
        elif replace_ratio > 1.0:
            findings.append(AuditFinding(
                "INFO", "churn",
                f"Replace/submit ratio: {replace_ratio:.1f}x"
            ))

    if fills and submits:
        fill_ratio = len(fills) / max(len(submits), 1)
        if fill_ratio < 0.05:
            findings.append(AuditFinding(
                "WARNING", "churn",
                f"Fill/submit ratio is {fill_ratio:.1%} — very few fills per order submitted"
            ))

    return findings


def audit_flatten_behavior(idx: EventIndex) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    flatten_events = idx.of("order_flatten_submitted")
    if not flatten_events:
        findings.append(AuditFinding("INFO", "flatten", "No flatten orders submitted"))
        return findings

    findings.append(AuditFinding(
        "INFO", "flatten",
        f"Flatten orders submitted: {len(flatten_events)}"
    ))

    # Check if flatten happened near session end
    traces = idx.of("strategy_trace")
    flatten_traces = [
        ev for ev in traces
        if (ev.get("trace", ev.payload).get("decision_reason", "") == "close_flatten_only")
    ]
    if flatten_traces:
        first_flatten_t = idx.relative_s(flatten_traces[0])
        findings.append(AuditFinding(
            "INFO", "flatten",
            f"First close-flatten trace at {first_flatten_t:.1f}s into session"
        ))

    return findings


def audit_session_connectivity(idx: EventIndex) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    reconnect_attempts = idx.of("session_reconnect_attempt")
    reconnect_failures = idx.of("session_reconnect_failed")
    health_failures = idx.of("session_health_check_failed")
    resub_failures = idx.of("session_resubscribe_failed")
    trade_time_failures = idx.of("session_last_trade_time_failed")
    data_refresh_failures = idx.of("market_data_pre_refresh_failed")

    if reconnect_attempts:
        findings.append(AuditFinding(
            "WARNING", "session",
            f"Reconnect attempts: {len(reconnect_attempts)} ({len(reconnect_failures)} failed)"
        ))
    if health_failures:
        findings.append(AuditFinding(
            "WARNING" if len(health_failures) < 5 else "CRITICAL", "session",
            f"Health check failures: {len(health_failures)}"
        ))
    if resub_failures:
        symbols = set()
        for ev in resub_failures:
            symbols.add(ev.get("symbol", "?"))
        findings.append(AuditFinding(
            "WARNING", "session",
            f"Resubscribe failures: {len(resub_failures)} for symbols {sorted(symbols)}"
        ))
    if trade_time_failures:
        findings.append(AuditFinding(
            "WARNING", "session",
            f"Broker clock failures (get_last_trade_time): {len(trade_time_failures)}"
        ))
    if data_refresh_failures:
        findings.append(AuditFinding(
            "WARNING", "session",
            f"Market data pre-refresh failures: {len(data_refresh_failures)}"
        ))

    if not any([reconnect_attempts, health_failures, resub_failures,
                trade_time_failures, data_refresh_failures]):
        findings.append(AuditFinding("INFO", "session", "No connectivity issues detected"))

    return findings


def audit_event_gaps(idx: EventIndex) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    # Check for expected event families
    required_families = {
        "strategy_trace": "No strategy traces — strategy never ran?",
        "server_poll_complete": "No server polls — reconciler never ran?",
        "book_cache_update": "No book cache updates — market data never refreshed?",
    }
    for kind, msg in required_families.items():
        if idx.count(kind) == 0:
            findings.append(AuditFinding("CRITICAL", "coverage", msg))

    # Check for large time gaps between poll cycles
    polls = idx.of("app_poll_cycle")
    if len(polls) >= 2:
        max_gap_ms = 0.0
        max_gap_t = 0.0
        for i in range(1, len(polls)):
            gap_ms = (polls[i].ts_ns - polls[i - 1].ts_ns) / 1_000_000.0
            if gap_ms > max_gap_ms:
                max_gap_ms = gap_ms
                max_gap_t = idx.relative_s(polls[i])
        if max_gap_ms > 1000:
            findings.append(AuditFinding(
                "WARNING", "timing",
                f"Largest poll gap: {max_gap_ms:.0f}ms at t={max_gap_t:.1f}s"
            ))
        elif max_gap_ms > 500:
            findings.append(AuditFinding(
                "INFO", "timing",
                f"Largest poll gap: {max_gap_ms:.0f}ms at t={max_gap_t:.1f}s"
            ))

    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Risk and safety audit")
    parser.add_argument("logfile", nargs="?", default=None)
    args = parser.parse_args()

    path = resolve_log_path([args.logfile] if args.logfile else [])
    idx = EventIndex.build(path)

    all_findings: list[AuditFinding] = []
    all_findings.extend(audit_safe_mode(idx))
    all_findings.extend(audit_order_failures(idx))
    all_findings.extend(audit_blocked_orders(idx))
    all_findings.extend(audit_sync_failures(idx))
    all_findings.extend(audit_session_connectivity(idx))
    all_findings.extend(audit_replace_churn(idx))
    all_findings.extend(audit_flatten_behavior(idx))
    all_findings.extend(audit_event_gaps(idx))

    criticals = [f for f in all_findings if f.severity == "CRITICAL"]
    warnings = [f for f in all_findings if f.severity == "WARNING"]
    infos = [f for f in all_findings if f.severity == "INFO"]

    print(f"\n{'RISK AUDIT':=^60}")
    print(f"  Session duration: {idx.duration_s:.1f}s")
    print(f"  Total events: {len(idx.events)}")
    print(f"  Findings: {len(criticals)} critical, {len(warnings)} warnings, {len(infos)} info")

    if criticals:
        print(f"\n{'CRITICAL':=^60}")
        for f in criticals:
            t_str = f"[{f.t_s:.1f}s] " if f.t_s > 0 else ""
            print(f"  [!!] {t_str}{f.category}: {f.message}")

    if warnings:
        print(f"\n{'WARNINGS':=^60}")
        for f in warnings:
            t_str = f"[{f.t_s:.1f}s] " if f.t_s > 0 else ""
            print(f"  [!]  {t_str}{f.category}: {f.message}")

    if infos:
        print(f"\n{'INFO':=^60}")
        for f in infos:
            t_str = f"[{f.t_s:.1f}s] " if f.t_s > 0 else ""
            print(f"  [i]  {t_str}{f.category}: {f.message}")

    # Overall verdict
    print(f"\n{'VERDICT':=^60}")
    if criticals:
        print("  FAIL — critical issues found, investigate before next run")
    elif warnings:
        print("  CAUTION — warnings found, review before scaling up")
    else:
        print("  PASS — no issues detected")
    print()


if __name__ == "__main__":
    main()