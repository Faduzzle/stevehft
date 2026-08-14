from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.core.config import RuntimeConfig
from src.telemetry.recorder import next_event_log_path


@dataclass(slots=True)
class PreflightReport:
    passed: bool
    checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        lines = ["Preflight report:"]
        for check in self.checks:
            lines.append(f"  [ok] {check}")
        for warning in self.warnings:
            lines.append(f"  [warn] {warning}")
        for error in self.errors:
            lines.append(f"  [error] {error}")
        lines.append(f"Result: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def run_preflight_checks(
    runtime_config: RuntimeConfig,
    *,
    execute_orders: bool = False,
) -> PreflightReport:
    report = PreflightReport(passed=False)

    try:
        runtime_config.validate()
    except ValueError as exc:
        report.errors.append(str(exc))
        return report

    _check_symbols(runtime_config, report)
    _check_timing(runtime_config, report)
    _check_risk_budget(runtime_config, report)
    _check_paths(runtime_config, report)
    _check_live_mode_requirements(runtime_config, report, execute_orders=execute_orders)

    report.passed = not report.errors
    return report


def _check_symbols(runtime_config: RuntimeConfig, report: PreflightReport) -> None:
    symbols = runtime_config.market_data.symbols
    if not symbols:
        report.checks.append(
            "symbols omitted; runtime will discover tradable symbols from "
            "trader.get_stock_list() after connecting"
        )
        return
    normalized = [symbol.strip().upper() for symbol in symbols]
    if any(not symbol for symbol in normalized):
        report.errors.append("symbols must be non-empty after trimming whitespace")
        return
    duplicates = sorted({symbol for symbol in normalized if normalized.count(symbol) > 1})
    if duplicates:
        report.errors.append(f"duplicate symbols are not allowed: {duplicates}")
        return
    report.checks.append(f"symbols configured: {', '.join(normalized)}")


def _check_timing(runtime_config: RuntimeConfig, report: PreflightReport) -> None:
    update_interval_ms = runtime_config.market_data.update_interval_ms
    reconcile_interval_ms = runtime_config.risk.reconcile_interval_ms
    stale_book_after_ms = runtime_config.risk.stale_book_after_ms
    stale_order_after_ms = runtime_config.risk.stale_order_after_ms
    flatten_start_minutes = runtime_config.risk.flatten_start_minutes_to_close

    if update_interval_ms > stale_book_after_ms:
        report.errors.append(
            "update_interval_ms must be <= stale_book_after_ms so one slow cycle "
            "does not immediately make every book stale"
        )
    else:
        report.checks.append(
            f"market-data cadence {update_interval_ms}ms is within "
            f"stale-book threshold {stale_book_after_ms}ms"
        )

    if reconcile_interval_ms > stale_book_after_ms:
        report.errors.append(
            "reconcile_interval_ms must be <= stale_book_after_ms so broker-state "
            "polling cannot lag past the safe-mode staleness threshold"
        )
    elif reconcile_interval_ms > stale_order_after_ms:
        report.errors.append(
            "reconcile_interval_ms must be <= stale_order_after_ms so mandatory "
            "stale-order refresh checks are not skipped"
        )
    else:
        report.checks.append(
            f"reconcile cadence {reconcile_interval_ms}ms is within broker-state "
            f"staleness thresholds"
        )

    if stale_book_after_ms < 2 * update_interval_ms:
        report.warnings.append(
            "stale_book_after_ms is less than 2x update_interval_ms; expect tighter "
            "quote suppression during transient delays"
        )

    if flatten_start_minutes < 2:
        report.warnings.append(
            "flatten_start_minutes_to_close is below 2 minutes; cancel + partial-fill "
            "races may leave too little exit buffer"
        )
    else:
        report.checks.append(
            f"flatten starts {flatten_start_minutes} minutes before close"
        )


def _check_risk_budget(runtime_config: RuntimeConfig, report: PreflightReport) -> None:
    per_symbol_cap = runtime_config.risk.max_position_lots_per_symbol
    gross_cap = runtime_config.risk.max_gross_position_lots
    symbol_count = len(runtime_config.market_data.symbols)
    bp_reserve_fraction = runtime_config.risk.buying_power_reserve_fraction

    if gross_cap > 0 and gross_cap < per_symbol_cap:
        report.errors.append(
            "max_gross_position_lots must be >= max_position_lots_per_symbol"
        )
        return

    if gross_cap <= 0:
        report.checks.append(
            "static gross-lot cap disabled; router will enforce per-symbol caps "
            f"plus a {bp_reserve_fraction:.1%} BP reserve buffer"
        )
        return

    if symbol_count > 1 and gross_cap < min(symbol_count, 2) * per_symbol_cap:
        report.warnings.append(
            "gross position cap is tight relative to per-symbol caps; expect multiple "
            "symbols to become allocation-constrained quickly"
        )

    report.checks.append(
        "risk caps are internally consistent: "
        f"per_symbol={per_symbol_cap} lots, gross={gross_cap} lots"
    )


def _check_paths(runtime_config: RuntimeConfig, report: PreflightReport) -> None:
    initiator_cfg = Path(runtime_config.initiator_cfg)
    if not initiator_cfg.exists():
        report.errors.append(f"initiator config file does not exist: {initiator_cfg}")
    elif not initiator_cfg.is_file():
        report.errors.append(f"initiator config path is not a file: {initiator_cfg}")
    else:
        report.checks.append(f"initiator config found: {initiator_cfg}")

    session_dir = Path(runtime_config.telemetry.session_dir)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        probe_path = session_dir / ".preflight_write_probe"
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink(missing_ok=True)
    except OSError as exc:
        report.errors.append(
            f"telemetry session_dir is not writable: {session_dir} ({exc})"
        )
    else:
        report.checks.append(f"telemetry session_dir is writable: {session_dir}")
        report.checks.append(
            f"next telemetry log file: {next_event_log_path(session_dir).name} "
            "(events.jsonl points to the latest numbered run)"
        )


def _check_live_mode_requirements(
    runtime_config: RuntimeConfig,
    report: PreflightReport,
    *,
    execute_orders: bool,
) -> None:
    if not execute_orders:
        report.checks.append("dry-run mode selected; live order routing is disabled")
        return

    if not runtime_config.username.strip():
        report.errors.append("live-order mode requires a non-empty SHIFT username")
    if not runtime_config.password:
        report.errors.append("live-order mode requires a non-empty SHIFT password")

    if (
        runtime_config.risk.max_position_lots_per_symbol > 2
        or runtime_config.risk.max_gross_position_lots > 4
        or runtime_config.risk.max_gross_position_lots <= 0
    ):
        report.warnings.append(
            "live-order mode is using non-trivial position caps; confirm this is the "
            "intended deployment budget for the current session"
        )
    report.checks.append("live-order mode explicitly requested")
