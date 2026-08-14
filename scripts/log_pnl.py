#!/usr/bin/env python3
"""PnL and fee analysis — reconstruct fill-by-fill economics.

Tracks per-symbol and aggregate:
- realized PnL from portfolio snapshots
- estimated rebates and fees
- fill-level slippage (arrival and decision shortfall)
- passive vs aggressive fill breakdown
- running net fee score

Usage:
    python scripts/log_pnl.py runs/live_smoke/events.jsonl
    python scripts/log_pnl.py runs/live_smoke/events.jsonl --symbol AAPL
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field

from log_loader import EventIndex, Event, resolve_log_path


LOT_SIZE_SHARES = 100
LIMIT_REBATE_PER_SHARE = 0.002
MARKET_FEE_PER_SHARE = 0.003


@dataclass(slots=True)
class SymbolPnL:
    symbol: str
    fills: int = 0
    passive_fills: int = 0
    aggressive_fills: int = 0
    total_shares: int = 0
    total_notional: float = 0.0
    estimated_rebate: float = 0.0
    estimated_fee: float = 0.0
    arrival_shortfall: float = 0.0
    decision_shortfall: float = 0.0
    last_realized_pl: float = 0.0
    fill_prices: list[float] = field(default_factory=list)


def analyze(idx: EventIndex, symbol_filter: str | None) -> dict[str, SymbolPnL]:
    by_symbol: dict[str, SymbolPnL] = {}

    for ev in idx.of("order_fill"):
        fill = ev.get("fill", {})
        symbol = fill.get("symbol", "?")
        if symbol_filter and symbol != symbol_filter:
            continue

        pnl = by_symbol.get(symbol)
        if pnl is None:
            pnl = SymbolPnL(symbol=symbol)
            by_symbol[symbol] = pnl

        pnl.fills += 1
        executed_size = fill.get("executed_size", 0)
        executed_price = fill.get("executed_price", 0.0)
        pnl.fill_prices.append(executed_price)

        slippage = ev.get("slippage", {})
        arrival_slip = slippage.get("arrival_implementation_shortfall", 0.0)
        decision_slip = slippage.get("decision_implementation_shortfall", 0.0)
        pnl.arrival_shortfall += arrival_slip
        pnl.decision_shortfall += decision_slip

    # Use session metrics for aggregate stats
    for ev in idx.of("session_metrics"):
        m = ev.get("metrics", {})
        # These are session-wide, not per-symbol; we'll use them for totals
        pass

    # Use portfolio snapshots for realized PnL
    for ev in idx.of("portfolio_snapshot"):
        positions = ev.get("positions", {})
        for symbol, pos in positions.items():
            if symbol_filter and symbol != symbol_filter:
                continue
            if not isinstance(pos, dict):
                continue
            pnl = by_symbol.get(symbol)
            if pnl is None:
                pnl = SymbolPnL(symbol=symbol)
                by_symbol[symbol] = pnl
            pnl.last_realized_pl = pos.get("realized_pl", 0.0)

    return by_symbol


def print_fill_timeline(idx: EventIndex, symbol_filter: str | None) -> None:
    fills = idx.of("order_fill")
    if not fills:
        print("\n  (no fills to show timeline)")
        return

    print(f"\n{'FILL TIMELINE':=^60}")
    print(f"  {'t_s':>8s}  {'symbol':>8s}  {'side':>4s}  {'price':>8s}  {'size':>5s}  {'arrival_slip':>12s}")
    print(f"  {'-'*8}  {'-'*8}  {'-'*4}  {'-'*8}  {'-'*5}  {'-'*12}")

    for ev in fills:
        fill = ev.get("fill", {})
        symbol = fill.get("symbol", "?")
        if symbol_filter and symbol != symbol_filter:
            continue
        t = idx.relative_s(ev)
        side = fill.get("side", "?")
        price = fill.get("executed_price", 0.0)
        size = fill.get("executed_size", 0)
        slippage = ev.get("slippage", {})
        arrival_slip = slippage.get("realized_arrival_slippage_ticks", 0.0)
        print(f"  {t:8.3f}  {symbol:>8s}  {side:>4s}  {price:8.4f}  {size:5d}  {arrival_slip:12.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PnL and fee analysis")
    parser.add_argument("logfile", nargs="?", default=None)
    parser.add_argument("--symbol", default=None, help="Filter by symbol")
    args = parser.parse_args()

    path = resolve_log_path([args.logfile] if args.logfile else [])
    idx = EventIndex.build(path)

    by_symbol = analyze(idx, args.symbol)

    print(f"\n{'PNL SUMMARY':=^60}")
    if not by_symbol:
        print("  (no fill or position data)")
    else:
        for sym in sorted(by_symbol):
            pnl = by_symbol[sym]
            vwap = sum(pnl.fill_prices) / len(pnl.fill_prices) if pnl.fill_prices else 0.0
            print(f"\n  Symbol: {sym}")
            print(f"    Fills              : {pnl.fills}")
            print(f"    Fill VWAP          : ${vwap:.4f}")
            print(f"    Arrival shortfall  : ${pnl.arrival_shortfall:.4f}")
            print(f"    Decision shortfall : ${pnl.decision_shortfall:.4f}")
            print(f"    Realized PnL       : ${pnl.last_realized_pl:.4f}")

    # Session-wide summary from metrics
    metrics_list = idx.of("session_metrics") or idx.of("strategy_cycle_complete")
    if metrics_list:
        last = metrics_list[-1]
        m = last.get("metrics", {})
        print(f"\n{'SESSION TOTALS':=^60}")
        print(f"  Total fills          : {m.get('executed_trades', 0)}")
        print(f"  Total shares         : {m.get('executed_shares', 0)}")
        print(f"  Passive fills        : {m.get('passive_fills', 0)}")
        print(f"  Aggressive fills     : {m.get('aggressive_fills', 0)}")
        print(f"  Est. rebates         : ${m.get('estimated_rebates', 0):.4f}")
        print(f"  Est. fees            : ${m.get('estimated_fees', 0):.4f}")
        print(f"  Est. net fees        : ${m.get('estimated_net_fees', 0):.4f}")
        print(f"  Arrival shortfall    : ${m.get('arrival_shortfall', 0):.4f}")
        print(f"  Decision shortfall   : ${m.get('decision_shortfall', 0):.4f}")

    print_fill_timeline(idx, args.symbol)
    print()


if __name__ == "__main__":
    main()
