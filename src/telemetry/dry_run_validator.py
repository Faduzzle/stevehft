from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DryRunCheckResult:
    name: str
    passed: bool
    details: str = ""


@dataclass(slots=True)
class DryRunValidationReport:
    event_path: Path
    checks: list[DryRunCheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def add(self, name: str, passed: bool, details: str = "") -> None:
        self.checks.append(DryRunCheckResult(name=name, passed=passed, details=details))

    def as_text(self) -> str:
        lines = [
            f"dry_run_validation={'PASS' if self.passed else 'FAIL'}",
            f"event_path={self.event_path}",
        ]
        for check in self.checks:
            suffix = f" ({check.details})" if check.details else ""
            lines.append(
                f"[{'PASS' if check.passed else 'FAIL'}] {check.name}{suffix}"
            )
        return "\n".join(lines)


def validate_dry_run_log(
    event_path: str | Path,
    *,
    tick_size: float,
) -> DryRunValidationReport:
    path = Path(event_path)
    report = DryRunValidationReport(event_path=path)
    if tick_size <= 0.0:
        raise ValueError("tick_size must be positive")
    if not path.exists():
        report.add("event_file_exists", False, "missing JSONL log")
        return report

    events = _load_jsonl(path)
    event_kinds = [str(event.get("kind", "")) for event in events]
    report.add("event_file_exists", True)
    report.add("events_parse", True, f"count={len(events)}")
    _check_required_event_families(report, event_kinds)
    _check_strategy_targets(report, events, tick_size=tick_size)
    _check_strategy_traces(report, events)
    _check_session_metrics(report, events)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a dry-run or smoke-run events.jsonl file.",
    )
    parser.add_argument(
        "event_path",
        help="Path to events.jsonl.",
    )
    parser.add_argument(
        "--tick-size",
        type=float,
        default=0.01,
        help="Tick size used to validate quoted prices.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = validate_dry_run_log(args.event_path, tick_size=args.tick_size)
    print(report.as_text())
    return 0 if report.passed else 2


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_no}: {exc}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"line {line_no} is not a JSON object")
        events.append(event)
    return events


def _check_required_event_families(
    report: DryRunValidationReport,
    event_kinds: list[str],
) -> None:
    required = (
        "app_started",
        "bootstrap_complete",
        "strategy_trace",
        "strategy_target",
        "strategy_cycle_complete",
        "session_metrics",
        "app_stopping",
    )
    missing = [kind for kind in required if kind not in event_kinds]
    report.add(
        "required_event_families",
        not missing,
        "missing=" + ",".join(missing) if missing else "",
    )


def _check_strategy_targets(
    report: DryRunValidationReport,
    events: list[dict[str, Any]],
    *,
    tick_size: float,
) -> None:
    crossed_count = 0
    misaligned_count = 0
    disabled_nonzero_count = 0
    flatten_two_sided_count = 0
    seen_targets = 0

    for event in events:
        if event.get("kind") != "strategy_target":
            continue
        target = event.get("payload", {}).get("target", {})
        seen_targets += 1
        bid_px = float(target.get("bid_px", 0.0))
        ask_px = float(target.get("ask_px", 0.0))
        bid_size = int(target.get("bid_size", 0))
        ask_size = int(target.get("ask_size", 0))
        enable_bid = bool(target.get("enable_bid", False))
        enable_ask = bool(target.get("enable_ask", False))
        flatten_mode = bool(target.get("flatten_mode", False))

        if ask_px <= bid_px:
            crossed_count += 1
        if not _is_tick_aligned(bid_px, tick_size) or not _is_tick_aligned(ask_px, tick_size):
            misaligned_count += 1
        if (not enable_bid and bid_size != 0) or (not enable_ask and ask_size != 0):
            disabled_nonzero_count += 1
        if flatten_mode and enable_bid and enable_ask:
            flatten_two_sided_count += 1

    report.add("strategy_targets_present", seen_targets > 0, f"count={seen_targets}")
    report.add("strategy_targets_cross_free", crossed_count == 0, f"violations={crossed_count}")
    report.add(
        "strategy_targets_tick_aligned",
        misaligned_count == 0,
        f"violations={misaligned_count}",
    )
    report.add(
        "disabled_sides_have_zero_size",
        disabled_nonzero_count == 0,
        f"violations={disabled_nonzero_count}",
    )
    report.add(
        "flatten_targets_not_two_sided",
        flatten_two_sided_count == 0,
        f"violations={flatten_two_sided_count}",
    )


def _check_strategy_traces(
    report: DryRunValidationReport,
    events: list[dict[str, Any]],
) -> None:
    trace_count = 0
    nonfinite_count = 0
    out_of_range_count = 0
    for event in events:
        if event.get("kind") != "strategy_trace":
            continue
        trace_count += 1
        diagnostics = event.get("payload", {}).get("trace", {}).get("diagnostics", {})
        extra = diagnostics.get("extra", {})
        numeric_values = (
            diagnostics.get("fair_value_anchor"),
            diagnostics.get("fair_value_center"),
            diagnostics.get("quote_width"),
            diagnostics.get("inventory_skew"),
            diagnostics.get("alpha_bias"),
            diagnostics.get("toxicity_score"),
            diagnostics.get("allocation_weight"),
            diagnostics.get("pace_multiplier"),
            extra.get("spread_mean_ticks"),
            extra.get("spread_std_ticks"),
            extra.get("spread_p90_ticks"),
            extra.get("spread_zscore"),
            extra.get("local_imbalance_zscore"),
            extra.get("global_drift_zscore"),
            extra.get("quote_age_mean_ms"),
            extra.get("quote_age_p90_ms"),
        )
        if not all(_is_finite_number(value) for value in numeric_values):
            nonfinite_count += 1
        if not _trace_extra_ranges_are_valid(extra):
            out_of_range_count += 1

    report.add("strategy_traces_present", trace_count > 0, f"count={trace_count}")
    report.add(
        "strategy_trace_numbers_finite",
        nonfinite_count == 0,
        f"violations={nonfinite_count}",
    )
    report.add(
        "strategy_trace_ranges_valid",
        out_of_range_count == 0,
        f"violations={out_of_range_count}",
    )


def _check_session_metrics(
    report: DryRunValidationReport,
    events: list[dict[str, Any]],
) -> None:
    metrics_count = 0
    nonfinite_count = 0
    ratio_out_of_bounds_count = 0
    for event in events:
        if event.get("kind") != "session_metrics":
            continue
        metrics_count += 1
        metrics = event.get("payload", {}).get("metrics", {})
        numeric_values = (
            metrics.get("estimated_fees"),
            metrics.get("estimated_rebates"),
            metrics.get("estimated_net_fees"),
            metrics.get("fill_vwap"),
            metrics.get("fill_twap"),
            metrics.get("arrival_shortfall"),
            metrics.get("decision_shortfall"),
        )
        if not all(_is_finite_number(value) for value in numeric_values):
            nonfinite_count += 1
        ratio = metrics.get("passive_fill_ratio", 0.0)
        if not _is_finite_number(ratio) or not 0.0 <= float(ratio) <= 1.0:
            ratio_out_of_bounds_count += 1

    report.add("session_metrics_present", metrics_count > 0, f"count={metrics_count}")
    report.add(
        "session_metrics_numbers_finite",
        nonfinite_count == 0,
        f"violations={nonfinite_count}",
    )
    report.add(
        "session_metrics_ratio_bounded",
        ratio_out_of_bounds_count == 0,
        f"violations={ratio_out_of_bounds_count}",
    )


def _is_tick_aligned(price: float, tick_size: float) -> bool:
    ticks = round(price / tick_size)
    return abs(price - ticks * tick_size) <= 1e-9


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _trace_extra_ranges_are_valid(extra: dict[str, Any]) -> bool:
    bounded_pairs = (
        ("queue_fill_support", 0.0, 1.0),
        ("bid_queue_share", 0.0, 1.0),
        ("ask_queue_share", 0.0, 1.0),
        ("toxicity_score", 0.0, 1.0),
        ("toxicity_width_multiplier", 1.0, 1.5),
        ("toxicity_size_multiplier", 0.5, 1.0),
        ("slippage_quality_score", 0.0, 1.2),
        ("passive_fill_probability", 0.0, 1.0),
    )
    for key, lower, upper in bounded_pairs:
        value = extra.get(key)
        if not _is_finite_number(value):
            return False
        numeric = float(value)
        if numeric < lower - 1e-9 or numeric > upper + 1e-9:
            return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
