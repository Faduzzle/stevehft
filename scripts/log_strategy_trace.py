#!/usr/bin/env python3
"""Strategy parameter evolution — track how the adaptive params change over time.

For each symbol, prints time series of key parameters so you can spot:
- fair value drift vs mid
- inventory skew direction and magnitude
- width expansion/contraction
- toxicity score changes
- allocation and pace multiplier changes
- queue position evolution

Usage:
    python scripts/log_strategy_trace.py runs/live_smoke/events.jsonl
    python scripts/log_strategy_trace.py runs/live_smoke/events.jsonl --symbol AAPL
    python scripts/log_strategy_trace.py runs/live_smoke/events.jsonl --csv
"""
from __future__ import annotations

import argparse
import csv
import io
import sys

from log_loader import EventIndex, resolve_log_path


KEY_FIELDS = [
    "fair_value",
    "spread_ticks",
    "half_width",
    "inventory_lots",
    "inventory_pressure_score",
    "cj_inventory_skew_ticks_per_lot",
    "glft_half_width_ticks",
    "glft_arrival_intensity",
    "toxicity_score",
    "toxicity_width_multiplier",
    "toxicity_size_multiplier",
    "allocation_weight",
    "pace_multiplier",
    "passive_fill_probability",
    "queue_fill_support",
    "bid_queue_share",
    "ask_queue_share",
    "spread_zscore",
    "local_imbalance_zscore",
    "global_drift_zscore",
    "spread_regime_signal",
    "global_drift_regime_signal",
    "execution_cost_score",
    "slippage_quality_score",
    "buying_power_usage_ratio",
    "buying_power_scale",
    "close_urgency_multiplier",
    "flatten_only_mode",
    "safe_mode",
    "sigma_realized",
    "local_microprice",
    "local_depth_imbalance",
    "global_l1_imbalance",
    "global_mid_drift",
    "depth_concentration_score",
    "front_shape_imbalance",
    "global_l1_voi",
    "local_multi_level_voi",
    "last_trade_price",
    "last_trade_size",
    "trade_signed_volume",
    "bid_size_multiplier",
    "ask_size_multiplier",
    "bid_width_multiplier",
    "ask_width_multiplier",
    "pnl_allocation_score",
]

TARGET_FIELDS = [
    "bid_px",
    "ask_px",
    "bid_size",
    "ask_size",
    "enable_bid",
    "enable_ask",
    "flatten_mode",
    "reason",
]


def extract_trace_row(ev_payload: dict, relative_s: float) -> dict:
    # Support both old nested format (trace→diagnostics→extra) and new flat format
    trace = ev_payload.get("trace", ev_payload)
    if "diagnostics" in trace:
        # Legacy nested format
        diag = trace.get("diagnostics", {})
        extra = diag.get("extra", {})
        target = trace.get("quote_target", {})
        symbol = trace.get("symbol", "?")
        decision_reason = trace.get("decision_reason", "")
        participation_mode = diag.get("participation_mode", "")
        fair_value_anchor = diag.get("fair_value_anchor", 0)
        fair_value_center = diag.get("fair_value_center", 0)
        quote_width = diag.get("quote_width", 0)
        inventory_skew = diag.get("inventory_skew", 0)
        alpha_bias = diag.get("alpha_bias", 0)
    else:
        # New flat format — diagnostics fields are top-level in payload
        extra = trace.get("extra", {})
        target = trace.get("quote_target", {})
        symbol = trace.get("symbol", "?")
        decision_reason = trace.get("decision_reason", "")
        participation_mode = trace.get("participation_mode", "")
        fair_value_anchor = trace.get("fair_value_anchor", 0)
        fair_value_center = trace.get("fair_value_center", 0)
        quote_width = trace.get("quote_width", 0)
        inventory_skew = trace.get("inventory_skew", 0)
        alpha_bias = trace.get("alpha_bias", 0)

    row: dict = {
        "t_s": round(relative_s, 3),
        "symbol": symbol,
        "decision_reason": decision_reason,
        "participation_mode": participation_mode,
        "fair_value_anchor": fair_value_anchor,
        "fair_value_center": fair_value_center,
        "quote_width": quote_width,
        "inventory_skew": inventory_skew,
        "alpha_bias": alpha_bias,
    }

    for f in KEY_FIELDS:
        row[f] = extra.get(f, "")

    for f in TARGET_FIELDS:
        row[f] = target.get(f, "") if target else ""

    return row


def print_text(rows: list[dict], symbol_filter: str | None) -> None:
    header_fields = ["t_s", "symbol", "fair_value_center", "quote_width",
                     "inventory_skew", "inventory_lots", "half_width",
                     "toxicity_score", "allocation_weight", "pace_multiplier",
                     "bid_px", "ask_px", "bid_size", "ask_size",
                     "enable_bid", "enable_ask", "decision_reason"]
    header = "  ".join(f"{f:>18s}" for f in header_fields)
    print(header)
    print("-" * len(header))

    for row in rows:
        if symbol_filter and row["symbol"] != symbol_filter:
            continue
        vals = []
        for f in header_fields:
            v = row.get(f, "")
            if isinstance(v, float):
                vals.append(f"{v:>18.5f}")
            elif isinstance(v, bool):
                vals.append(f"{'Y':>18s}" if v else f"{'N':>18s}")
            else:
                vals.append(f"{str(v):>18s}")
        print("  ".join(vals))


def print_csv(rows: list[dict]) -> None:
    if not rows:
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy parameter trace viewer")
    parser.add_argument("logfile", nargs="?", default=None)
    parser.add_argument("--symbol", default=None, help="Filter by symbol")
    parser.add_argument("--csv", action="store_true", help="Output as CSV")
    args = parser.parse_args()

    path = resolve_log_path([args.logfile] if args.logfile else [])
    idx = EventIndex.build(path)

    traces = idx.of("strategy_trace")
    if not traces:
        print("No strategy traces found.")
        return

    rows = [extract_trace_row(ev.payload, idx.relative_s(ev)) for ev in traces]
    if args.symbol:
        rows = [r for r in rows if r["symbol"] == args.symbol]

    if args.csv:
        print_csv(rows)
    else:
        print(f"Strategy traces: {len(rows)}")
        print_text(rows, args.symbol)


if __name__ == "__main__":
    main()
