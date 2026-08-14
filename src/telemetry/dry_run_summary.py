from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RunningStats:
    count: int = 0
    minimum: float = math.inf
    maximum: float = -math.inf
    total: float = 0.0

    def update(self, value: Any) -> None:
        if not isinstance(value, (int, float)):
            return
        numeric = float(value)
        if not math.isfinite(numeric):
            return
        self.count += 1
        self.minimum = min(self.minimum, numeric)
        self.maximum = max(self.maximum, numeric)
        self.total += numeric

    @property
    def mean(self) -> float:
        if self.count <= 0:
            return 0.0
        return self.total / self.count

    def as_text(self) -> str:
        if self.count <= 0:
            return "count=0 mean=0.000000 min=0.000000 max=0.000000"
        return (
            f"count={self.count} mean={self.mean:.6f} "
            f"min={self.minimum:.6f} max={self.maximum:.6f}"
        )


@dataclass(slots=True)
class DryRunSummaryReport:
    event_path: Path
    event_counts: dict[str, int] = field(default_factory=dict)
    target_count: int = 0
    flatten_target_count: int = 0
    two_sided_target_count: int = 0
    one_sided_target_count: int = 0
    suppressed_target_count: int = 0
    alpha_bias_local_agreement_count: int = 0
    alpha_bias_local_disagreement_count: int = 0
    drift_bias_global_agreement_count: int = 0
    drift_bias_global_disagreement_count: int = 0
    feature_stats: dict[str, RunningStats] = field(default_factory=dict)

    def as_text(self) -> str:
        lines = [
            f"event_path={self.event_path}",
            "event_counts="
            + ", ".join(
                f"{kind}:{count}" for kind, count in sorted(self.event_counts.items())
            ),
            (
                "targets="
                f"total:{self.target_count} "
                f"flatten:{self.flatten_target_count} "
                f"two_sided:{self.two_sided_target_count} "
                f"one_sided:{self.one_sided_target_count} "
                f"suppressed:{self.suppressed_target_count}"
            ),
            (
                "sign_agreement="
                f"alpha_local:{self.alpha_bias_local_agreement_count}/"
                f"{self.alpha_bias_local_agreement_count + self.alpha_bias_local_disagreement_count} "
                f"drift_global:{self.drift_bias_global_agreement_count}/"
                f"{self.drift_bias_global_agreement_count + self.drift_bias_global_disagreement_count}"
            ),
        ]
        for key in sorted(self.feature_stats):
            lines.append(f"{key}: {self.feature_stats[key].as_text()}")
        return "\n".join(lines)


def summarize_dry_run_log(event_path: str | Path) -> DryRunSummaryReport:
    path = Path(event_path)
    report = DryRunSummaryReport(event_path=path)
    if not path.exists():
        raise FileNotFoundError(f"missing event log: {path}")
    for event in _load_jsonl(path):
        kind = str(event.get("kind", ""))
        report.event_counts[kind] = report.event_counts.get(kind, 0) + 1
        payload = event.get("payload", {})
        if kind == "strategy_target":
            _summarize_target(report, payload.get("target", {}))
        elif kind == "strategy_trace":
            _summarize_trace(report, payload.get("trace", {}))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print a compact summary of a dry-run or smoke-run events.jsonl file.",
    )
    parser.add_argument("event_path", help="Path to events.jsonl.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(summarize_dry_run_log(args.event_path).as_text())
    return 0


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        event = json.loads(raw_line)
        if not isinstance(event, dict):
            raise ValueError(f"line {line_no} is not a JSON object")
        events.append(event)
    return events


def _summarize_target(report: DryRunSummaryReport, target: dict[str, Any]) -> None:
    report.target_count += 1
    enable_bid = bool(target.get("enable_bid", False))
    enable_ask = bool(target.get("enable_ask", False))
    if bool(target.get("flatten_mode", False)):
        report.flatten_target_count += 1
    if enable_bid and enable_ask:
        report.two_sided_target_count += 1
    elif enable_bid or enable_ask:
        report.one_sided_target_count += 1
    else:
        report.suppressed_target_count += 1
    _stats(report, "target_bid_px").update(target.get("bid_px"))
    _stats(report, "target_ask_px").update(target.get("ask_px"))
    _stats(report, "target_bid_size").update(target.get("bid_size"))
    _stats(report, "target_ask_size").update(target.get("ask_size"))


def _summarize_trace(report: DryRunSummaryReport, trace: dict[str, Any]) -> None:
    diagnostics = trace.get("diagnostics", {})
    extra = diagnostics.get("extra", {})
    alpha_bias = diagnostics.get("alpha_bias")
    local_imbalance = extra.get("local_depth_imbalance")
    drift_bias_ticks = extra.get("drift_inventory_bias_ticks")
    global_drift_ticks = extra.get("global_drift_shift_ticks")

    _stats(report, "fair_value_center").update(diagnostics.get("fair_value_center"))
    _stats(report, "quote_width").update(diagnostics.get("quote_width"))
    _stats(report, "allocation_weight").update(diagnostics.get("allocation_weight"))
    _stats(report, "pace_multiplier").update(diagnostics.get("pace_multiplier"))

    for key in (
        "local_depth_imbalance",
        "local_microprice",
        "global_l1_imbalance",
        "global_mid_drift",
        "global_drift_shift_ticks",
        "drift_inventory_bias_ticks",
        "spread_zscore",
        "local_imbalance_zscore",
        "global_drift_zscore",
        "toxicity_score",
        "queue_fill_support",
        "passive_fill_probability",
        "slippage_quality_score",
    ):
        _stats(report, key).update(extra.get(key))

    if _same_signed(alpha_bias, local_imbalance):
        report.alpha_bias_local_agreement_count += 1
    elif _opposite_signed(alpha_bias, local_imbalance):
        report.alpha_bias_local_disagreement_count += 1

    if _same_signed(drift_bias_ticks, global_drift_ticks):
        report.drift_bias_global_agreement_count += 1
    elif _opposite_signed(drift_bias_ticks, global_drift_ticks):
        report.drift_bias_global_disagreement_count += 1


def _stats(report: DryRunSummaryReport, key: str) -> RunningStats:
    stats = report.feature_stats.get(key)
    if stats is None:
        stats = RunningStats()
        report.feature_stats[key] = stats
    return stats


def _same_signed(left: Any, right: Any) -> bool:
    if not _finite_nonzero(left) or not _finite_nonzero(right):
        return False
    return float(left) * float(right) > 0.0


def _opposite_signed(left: Any, right: Any) -> bool:
    if not _finite_nonzero(left) or not _finite_nonzero(right):
        return False
    return float(left) * float(right) < 0.0


def _finite_nonzero(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) != 0.0


if __name__ == "__main__":
    raise SystemExit(main())
