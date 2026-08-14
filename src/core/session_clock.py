from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from src.core.config import RiskConfig, StrategyConfig


@dataclass(slots=True)
class SessionProgress:
    now_local: datetime
    session_open_local: datetime
    session_close_local: datetime
    is_session_open: bool
    elapsed_fraction: float
    minutes_to_close: float
    session_length_minutes: float
    expected_trades_so_far: float
    flatten_only_mode: bool
    flatten_window_minutes: int
    in_warmup: bool          # True during warmup_minutes_at_open after session open
    minutes_since_open: float
    warmup_minutes: int      # warmup_minutes_at_open config value


class SessionClock:
    def __init__(
        self,
        strategy_config: StrategyConfig,
        risk_config: RiskConfig,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._strategy_config = strategy_config
        self._risk_config = risk_config
        self._timezone = ZoneInfo(strategy_config.session_timezone)
        self._now_provider = now_provider or self._default_now

    def snapshot(self) -> SessionProgress:
        now_value = self._now_provider()
        if now_value.tzinfo is None:
            now_value = now_value.replace(tzinfo=self._timezone)
        now_local = now_value.astimezone(self._timezone)
        session_open = datetime.combine(
            now_local.date(),
            self._strategy_config.session_open_local,
            tzinfo=self._timezone,
        )
        session_close = datetime.combine(
            now_local.date(),
            self._strategy_config.session_close_local,
            tzinfo=self._timezone,
        )
        if session_close <= session_open:
            session_close += timedelta(days=1)

        total_seconds = max((session_close - session_open).total_seconds(), 1.0)
        if now_local <= session_open:
            elapsed_fraction = 0.0
            minutes_to_close = (session_close - session_open).total_seconds() / 60.0
            is_session_open = False
        elif now_local >= session_close:
            elapsed_fraction = 1.0
            minutes_to_close = 0.0
            is_session_open = False
        else:
            elapsed_fraction = (now_local - session_open).total_seconds() / total_seconds
            minutes_to_close = (session_close - now_local).total_seconds() / 60.0
            is_session_open = True

        minutes_since_open = (
            (now_local - session_open).total_seconds() / 60.0
            if is_session_open else 0.0
        )
        flatten_only_mode = (
            is_session_open
            and minutes_to_close <= self._risk_config.flatten_start_minutes_to_close
        )
        in_warmup = (
            is_session_open
            and minutes_since_open < self._risk_config.warmup_minutes_at_open
        )
        return SessionProgress(
            now_local=now_local,
            session_open_local=session_open,
            session_close_local=session_close,
            is_session_open=is_session_open,
            elapsed_fraction=elapsed_fraction,
            minutes_to_close=minutes_to_close,
            session_length_minutes=total_seconds / 60.0,
            expected_trades_so_far=(
                self._strategy_config.min_trades_per_day * elapsed_fraction
            ),
            flatten_only_mode=flatten_only_mode,
            flatten_window_minutes=self._risk_config.flatten_start_minutes_to_close,
            in_warmup=in_warmup,
            minutes_since_open=minutes_since_open,
            warmup_minutes=self._risk_config.warmup_minutes_at_open,
        )

    def _default_now(self) -> datetime:
        return datetime.now(tz=self._timezone)

    def set_now_provider(self, now_provider: Callable[[], datetime] | None) -> None:
        self._now_provider = now_provider or self._default_now
