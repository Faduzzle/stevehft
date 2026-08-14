from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.execution.order_state import OrderIntentAction
from src.telemetry.logger import EventLoggerLike


class SafeMode(str, Enum):
    NORMAL = "normal"
    DEGRADED_RECONCILE = "degraded_reconcile"
    POSITION_MISMATCH = "position_mismatch"
    FLATTEN_ONLY = "flatten_only"
    KILL_SWITCH = "kill_switch"


@dataclass(slots=True)
class SafeModeConfig:
    max_waiting_list_staleness_ms: int = 2_000
    max_portfolio_staleness_ms: int = 2_000
    max_position_mismatch_lots: int = 1
    max_degraded_duration_ms: int = 10_000


@dataclass(slots=True)
class ReconciliationHealth:
    waiting_list_stale_ms: int = 0
    portfolio_stale_ms: int = 0
    position_mismatch_lots: int = 0
    broker_connected: bool = True


class KillSwitchController:
    def __init__(
        self,
        config: SafeModeConfig,
        *,
        event_logger: Optional[EventLoggerLike] = None,
    ) -> None:
        self._config = config
        self._event_logger = event_logger
        self._mode = SafeMode.NORMAL
        self._degraded_entered_ts_ns = 0

    @property
    def mode(self) -> SafeMode:
        return self._mode

    def update(
        self,
        health: ReconciliationHealth,
        *,
        now_ns: int | None = None,
    ) -> SafeMode:
        ts_ns = now_ns or time.monotonic_ns()
        previous = self._mode
        next_mode = self._derive_mode(health, now_ns=ts_ns)
        if previous == SafeMode.FLATTEN_ONLY and next_mode == SafeMode.DEGRADED_RECONCILE:
            next_mode = SafeMode.FLATTEN_ONLY
        if next_mode == SafeMode.DEGRADED_RECONCILE:
            if previous != SafeMode.DEGRADED_RECONCILE:
                self._degraded_entered_ts_ns = ts_ns
            elif (
                self._degraded_entered_ts_ns > 0
                and (ts_ns - self._degraded_entered_ts_ns) / 1_000_000
                > self._config.max_degraded_duration_ms
            ):
                next_mode = SafeMode.FLATTEN_ONLY
        else:
            self._degraded_entered_ts_ns = 0
        self._mode = next_mode
        if self._event_logger is not None and previous != next_mode:
            self._event_logger.log(
                "safe_mode_transition",
                previous_mode=previous.value,
                next_mode=next_mode.value,
                waiting_list_stale_ms=health.waiting_list_stale_ms,
                portfolio_stale_ms=health.portfolio_stale_ms,
                position_mismatch_lots=health.position_mismatch_lots,
                broker_connected=health.broker_connected,
            )
        return next_mode

    def blocks(self, action: OrderIntentAction, *, reduces_risk: bool = False) -> bool:
        if self._mode == SafeMode.NORMAL:
            return False
        if self._mode == SafeMode.DEGRADED_RECONCILE:
            return action == OrderIntentAction.SUBMIT and not reduces_risk
        if self._mode == SafeMode.POSITION_MISMATCH:
            if action == OrderIntentAction.CANCEL:
                return False
            return not reduces_risk
        if self._mode == SafeMode.FLATTEN_ONLY:
            if action == OrderIntentAction.CANCEL:
                return False
            return not reduces_risk and action != OrderIntentAction.FLATTEN
        return action not in {OrderIntentAction.CANCEL, OrderIntentAction.FLATTEN}

    def _derive_mode(
        self,
        health: ReconciliationHealth,
        *,
        now_ns: int,
    ) -> SafeMode:
        del now_ns
        if not health.broker_connected:
            return SafeMode.KILL_SWITCH
        if abs(health.position_mismatch_lots) > self._config.max_position_mismatch_lots:
            return SafeMode.POSITION_MISMATCH
        if (
            health.waiting_list_stale_ms > self._config.max_waiting_list_staleness_ms
            or health.portfolio_stale_ms > self._config.max_portfolio_staleness_ms
        ):
            return SafeMode.DEGRADED_RECONCILE
        return SafeMode.NORMAL
