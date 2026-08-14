#!/usr/bin/env python3
"""Market data quality inspector — check book updates, spreads, and staleness.

Tracks per-symbol:
- Book update frequency and gaps
- Spread distribution (min, mean, p50, p90, max)
- Microprice vs mid divergence
- Local vs global book imbalance
- Any zero-depth or missing-touch events

Usage:
    python scripts/log_market_data.py runs/live_smoke/events.jsonl
    python scripts/log_market_data.py runs/live_smoke/events.jsonl --symbol AAPL
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field

from log_loader import EventIndex, Event, resolve_log_path


@dataclass(slots=True)
class SymbolBookStats:
    symbol: str
    updates: int = 0
    spreads: list[float] = field(default_factory=list)
    microprices: list[float] = field(default_factory=list)
    mids: list[float] = field(default_factory=list)
    imbalances: list[float] = field(default_factory=list)
    global_l1_vois: list[float] = field(default_factory=list)
    local_ml_vois: list[float] = field(default_factory=list)
    last_trade_prices: list[float] = field(default_factory=list)
    last_trade_sizes: list[int] = field(default_factory=list)
    zero_bid_count: int = 0
    zero_ask_count: int = 0
    update_gaps_ms: list[float] = field(default_factory=list)
    last_update_ts_ns: int = 0


def analyze(idx: EventIndex, symbol_filter: str | None) -> dict[str, SymbolBookStats]:
    by_symbol: dict[str, SymbolBookStats] = {}

    for ev in idx.of("book_cache_update"):
        symbol = ev.get("symbol", "?")
        if symbol_filter and symbol != symbol_filter:
            continue

        stats = by_symbol.get(symbol)
        if stats is None:
            stats = SymbolBookStats(symbol=symbol)
            by_symbol[symbol] = stats

        stats.updates += 1
        spread = ev.get("spread", 0.0)
        if spread is not None:
            stats.spreads.append(float(spread))
        microprice = ev.get("microprice", 0.0)
        if microprice:
            stats.microprices.append(float(microprice))
        bid = ev.get("best_bid_px", 0.0)
        ask = ev.get("best_ask_px", 0.0)
        if bid and ask:
            stats.mids.append(0.5 * (float(bid) + float(ask)))
        imbalance = ev.get("book_imbalance", 0.0)
        if imbalance is not None:
            stats.imbalances.append(float(imbalance))
        global_l1_voi = ev.get("global_l1_voi")
        if global_l1_voi is not None:
            stats.global_l1_vois.append(float(global_l1_voi))
        local_ml_voi = ev.get("local_multi_level_voi")
        if local_ml_voi is not None:
            stats.local_ml_vois.append(float(local_ml_voi))
        ltp = ev.get("last_trade_price", 0.0)
        if ltp and float(ltp) > 0:
            stats.last_trade_prices.append(float(ltp))
        lts = ev.get("last_trade_size", 0)
        if lts and int(lts) > 0:
            stats.last_trade_sizes.append(int(lts))

        if not ev.get("best_bid_px") or float(ev.get("best_bid_px", 0)) <= 0:
            stats.zero_bid_count += 1
        if not ev.get("best_ask_px") or float(ev.get("best_ask_px", 0)) <= 0:
            stats.zero_ask_count += 1

        if stats.last_update_ts_ns > 0:
            gap_ms = (ev.ts_ns - stats.last_update_ts_ns) / 1_000_000.0
            stats.update_gaps_ms.append(gap_ms)
        stats.last_update_ts_ns = ev.ts_ns

    return by_symbol


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(len(sorted_vals) * p)
    idx = min(idx, len(sorted_vals) - 1)
    return sorted_vals[idx]


def print_stats(by_symbol: dict[str, SymbolBookStats]) -> None:
    for sym in sorted(by_symbol):
        stats = by_symbol[sym]
        print(f"\n  Symbol: {sym}")
        print(f"    Book updates       : {stats.updates}")

        if stats.spreads:
            s = sorted(stats.spreads)
            print(f"    Spread min         : ${min(s):.4f}")
            print(f"    Spread mean        : ${sum(s)/len(s):.4f}")
            print(f"    Spread P50         : ${_percentile(s, 0.5):.4f}")
            print(f"    Spread P90         : ${_percentile(s, 0.9):.4f}")
            print(f"    Spread max         : ${max(s):.4f}")

        if stats.microprices and stats.mids and len(stats.microprices) == len(stats.mids):
            divergences = [abs(mp - mid) for mp, mid in zip(stats.microprices, stats.mids)]
            avg_div = sum(divergences) / len(divergences)
            max_div = max(divergences)
            print(f"    Microprice-mid avg : ${avg_div:.6f}")
            print(f"    Microprice-mid max : ${max_div:.6f}")

        if stats.imbalances:
            avg_imb = sum(stats.imbalances) / len(stats.imbalances)
            abs_imbs = sorted(abs(x) for x in stats.imbalances)
            print(f"    Imbalance mean     : {avg_imb:+.4f}")
            print(f"    |Imbalance| P50    : {_percentile(abs_imbs, 0.5):.4f}")
            print(f"    |Imbalance| P90    : {_percentile(abs_imbs, 0.9):.4f}")

        if stats.global_l1_vois:
            vois = sorted(stats.global_l1_vois)
            mean_voi = sum(vois) / len(vois)
            abs_vois = sorted(abs(x) for x in stats.global_l1_vois)
            print(f"    Global L1 VOI mean : {mean_voi:+.4f}")
            print(f"    |Global L1 VOI| P50: {_percentile(abs_vois, 0.5):.4f}")
            print(f"    |Global L1 VOI| P90: {_percentile(abs_vois, 0.9):.4f}")

        if stats.local_ml_vois:
            vois = sorted(stats.local_ml_vois)
            mean_voi = sum(vois) / len(vois)
            abs_vois = sorted(abs(x) for x in stats.local_ml_vois)
            print(f"    Local ML VOI mean  : {mean_voi:+.4f}")
            print(f"    |Local ML VOI| P50 : {_percentile(abs_vois, 0.5):.4f}")
            print(f"    |Local ML VOI| P90 : {_percentile(abs_vois, 0.9):.4f}")

        if stats.last_trade_prices:
            print(f"    Last trade updates : {len(stats.last_trade_prices)}")
            print(f"    Last trade px range: ${min(stats.last_trade_prices):.4f} - ${max(stats.last_trade_prices):.4f}")

        if stats.zero_bid_count or stats.zero_ask_count:
            print(f"    [!] Zero bid events: {stats.zero_bid_count}")
            print(f"    [!] Zero ask events: {stats.zero_ask_count}")

        if stats.update_gaps_ms:
            gaps = sorted(stats.update_gaps_ms)
            print(f"    Update gap mean    : {sum(gaps)/len(gaps):.1f} ms")
            print(f"    Update gap P95     : {_percentile(gaps, 0.95):.1f} ms")
            print(f"    Update gap max     : {max(gaps):.1f} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="Market data quality inspector")
    parser.add_argument("logfile", nargs="?", default=None)
    parser.add_argument("--symbol", default=None, help="Filter by symbol")
    args = parser.parse_args()

    path = resolve_log_path([args.logfile] if args.logfile else [])
    idx = EventIndex.build(path)

    by_symbol = analyze(idx, args.symbol)

    print(f"\n{'MARKET DATA QUALITY':=^60}")
    if not by_symbol:
        print("  (no book_cache_update events found)")
    else:
        print_stats(by_symbol)

    # Check for stale-book gate suppressions in strategy
    traces = idx.of("strategy_trace")
    stale_count = sum(
        1 for ev in traces
        if ev.get("trace", ev.payload).get("decision_reason", "") == "stale_book"
    )
    wide_count = sum(
        1 for ev in traces
        if ev.get("trace", ev.payload).get("decision_reason", "") == "spread_too_wide"
    )
    missing_count = sum(
        1 for ev in traces
        if ev.get("trace", ev.payload).get("decision_reason", "") == "missing_touch"
    )
    if stale_count or wide_count or missing_count:
        print(f"\n{'QUOTE SUPPRESSION':=^60}")
        if stale_count:
            print(f"  Stale book suppression : {stale_count} cycles")
        if wide_count:
            print(f"  Wide spread suppression: {wide_count} cycles")
        if missing_count:
            print(f"  Missing touch suppress : {missing_count} cycles")

    print()


if __name__ == "__main__":
    main()
