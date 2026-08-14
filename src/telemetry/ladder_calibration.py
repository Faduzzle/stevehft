from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict


def _finite_float(value: Any, default: float = 0.0) -> float:
    if not isinstance(value, (int, float)):
        return default
    numeric = float(value)
    return numeric if math.isfinite(numeric) else default


@dataclass(slots=True)
class LadderLevelStats:
    quote_samples: int = 0
    enabled_samples: int = 0
    size_total: float = 0.0
    queue_share_total: float = 0.0
    fill_count: int = 0
    fill_size_lots: int = 0
    arrival_slip_total: float = 0.0
    decision_slip_total: float = 0.0
    arrival_shortfall_total: float = 0.0
    decision_shortfall_total: float = 0.0

    @property
    def avg_size(self) -> float:
        if self.enabled_samples <= 0:
            return 0.0
        return self.size_total / self.enabled_samples

    @property
    def avg_queue_share(self) -> float:
        if self.enabled_samples <= 0:
            return 0.0
        return self.queue_share_total / self.enabled_samples

    @property
    def fill_rate(self) -> float:
        if self.enabled_samples <= 0:
            return 0.0
        return self.fill_count / self.enabled_samples

    @property
    def avg_arrival_slip_ticks(self) -> float:
        if self.fill_count <= 0:
            return 0.0
        return self.arrival_slip_total / self.fill_count

    @property
    def avg_decision_slip_ticks(self) -> float:
        if self.fill_count <= 0:
            return 0.0
        return self.decision_slip_total / self.fill_count

    def recommendation(self) -> str:
        if self.enabled_samples < 20:
            return "collect_more_data"
        if self.fill_count <= 0:
            return "no_fills_check_queue_or_tighten"
        if self.avg_arrival_slip_ticks > 0.30 or self.avg_decision_slip_ticks > 0.30:
            return "widen_or_reduce_size"
        if self.fill_rate < 0.01 and self.avg_queue_share < 0.20:
            return "shift_more_size_deeper_or_reduce_churn"
        if self.fill_rate > 0.05 and self.avg_arrival_slip_ticks < 0.0:
            return "can_add_size_here"
        return "keep_current"


@dataclass(slots=True)
class LadderCalibrationReport:
    event_path: Path
    stats_by_key: dict[tuple[str, str, int], LadderLevelStats]

    def as_text(self) -> str:
        lines = [
            f"event_path={self.event_path}",
            (
                "SYMBOL SIDE LVL QUOTES ENABLED AVG_SIZE AVG_QSH FILLS "
                "FILL_LOTS FILL_RATE ARR_SLIP DEC_SLIP ARR_SHORT DEC_SHORT "
                "RECOMMENDATION"
            ),
        ]
        for symbol, side, level_index in sorted(self.stats_by_key):
            stats = self.stats_by_key[(symbol, side, level_index)]
            lines.append(
                f"{symbol:<8} {side:<4} {level_index:>3d} "
                f"{stats.quote_samples:>6d} {stats.enabled_samples:>7d} "
                f"{stats.avg_size:>8.2f} {stats.avg_queue_share:>8.4f} "
                f"{stats.fill_count:>5d} {stats.fill_size_lots:>9d} "
                f"{stats.fill_rate:>9.4f} "
                f"{stats.avg_arrival_slip_ticks:>8.4f} "
                f"{stats.avg_decision_slip_ticks:>8.4f} "
                f"{stats.arrival_shortfall_total:>9.2f} "
                f"{stats.decision_shortfall_total:>9.2f} "
                f"{stats.recommendation()}"
            )
        return "\n".join(lines)


def calibrate_ladder_from_log(event_path: str | Path) -> LadderCalibrationReport:
    path = Path(event_path)
    if not path.exists():
        raise FileNotFoundError(f"missing event log: {path}")

    stats_by_key: DefaultDict[tuple[str, str, int], LadderLevelStats] = defaultdict(
        LadderLevelStats
    )
    for event in _load_jsonl(path):
        kind = str(event.get("kind", ""))
        payload = event.get("payload", {})
        if kind == "strategy_trace":
            quote_target = payload.get("quote_target", {})
            if not isinstance(quote_target, dict):
                quote_target = {}
            if not quote_target and isinstance(payload.get("trace"), dict):
                quote_target = payload["trace"].get("quote_target", {})
            _accumulate_quote_samples(stats_by_key, quote_target)
        elif kind == "order_fill":
            _accumulate_fill(stats_by_key, payload)
    return LadderCalibrationReport(
        event_path=path,
        stats_by_key=dict(stats_by_key),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize per-ladder-level quote/fill/slippage metrics from an event log.",
    )
    parser.add_argument("event_path", help="Path to events_XXXX.jsonl.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(calibrate_ladder_from_log(args.event_path).as_text())
    return 0


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        if not isinstance(event, dict):
            raise ValueError(f"line {line_no} is not a JSON object")
        events.append(event)
    return events


def _accumulate_quote_samples(
    stats_by_key: DefaultDict[tuple[str, str, int], LadderLevelStats],
    target: Any,
) -> None:
    if not isinstance(target, dict):
        return
    symbol = str(target.get("symbol", ""))
    for side, levels_key in (("bid", "bid_levels"), ("ask", "ask_levels")):
        levels = target.get(levels_key, ())
        if not isinstance(levels, list) or not levels:
            px_key = f"{side}_px"
            size_key = f"{side}_size"
            enabled_key = f"enable_{side}"
            queue_share_key = f"{side}_queue_share"
            if px_key in target or size_key in target or enabled_key in target:
                levels = [
                    {
                        "level_index": 0,
                        "price": target.get(px_key),
                        "size": target.get(size_key, 0),
                        "queue_share": target.get(queue_share_key, 0.0),
                        "enabled": target.get(enabled_key, False),
                    }
                ]
        if not isinstance(levels, list):
            continue
        for level in levels:
            if not isinstance(level, dict):
                continue
            key = (symbol, side, int(level.get("level_index", 0) or 0))
            stats = stats_by_key[key]
            stats.quote_samples += 1
            if bool(level.get("enabled", False)):
                stats.enabled_samples += 1
                stats.size_total += _finite_float(level.get("size"))
                stats.queue_share_total += _finite_float(level.get("queue_share"))


def _accumulate_fill(
    stats_by_key: DefaultDict[tuple[str, str, int], LadderLevelStats],
    payload: Any,
) -> None:
    if not isinstance(payload, dict):
        return
    if str(payload.get("liquidity", "")) not in {"", "limit"}:
        return
    fill = payload.get("fill", {})
    slippage = payload.get("slippage", {})
    if not isinstance(fill, dict) or not isinstance(slippage, dict):
        return
    key = (
        str(fill.get("symbol", "")),
        str(fill.get("side", "")),
        int(payload.get("level_index", 0) or 0),
    )
    stats = stats_by_key[key]
    stats.fill_count += 1
    stats.fill_size_lots += int(fill.get("executed_size", 0) or 0)
    stats.arrival_slip_total += _finite_float(
        slippage.get("realized_arrival_slippage_ticks")
    )
    stats.decision_slip_total += _finite_float(
        slippage.get("realized_decision_slippage_ticks")
    )
    stats.arrival_shortfall_total += _finite_float(
        slippage.get("arrival_implementation_shortfall")
    )
    stats.decision_shortfall_total += _finite_float(
        slippage.get("decision_implementation_shortfall")
    )


if __name__ == "__main__":
    raise SystemExit(main())
