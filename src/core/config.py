from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as LocalTime
from pathlib import Path
from typing import Sequence


@dataclass(slots=True)
class TelemetryConfig:
    session_dir: Path = Path("runs/latest")
    enable_event_logging: bool = True
    logger_flush_every: int = 64
    logger_max_queue_size: int = 50_000


@dataclass(slots=True)
class DashboardConfig:
    enabled: bool = False
    redraw_interval_ms: int = 250


@dataclass(slots=True)
class MarketDataConfig:
    symbols: tuple[str, ...] = ("AAPL", "XOM")
    book_depth_levels: int = 5
    update_interval_ms: int = 100
    subscribe_all_books: bool = False


@dataclass(slots=True)
class RiskConfig:
    max_position_lots_per_symbol: int = 5
    # Set max_gross_position_lots <= 0 to disable the static gross-lot ceiling and
    # rely on BP-reserve + per-symbol caps instead.
    max_gross_position_lots: int = 0
    buying_power_reserve_fraction: float = 0.025
    reconcile_interval_ms: int = 250
    stale_book_after_ms: int = 750
    stale_order_after_ms: int = 2_500
    flatten_start_minutes_to_close: int = 30
    # Warmup period at session open: read the market, update estimators, but do not
    # place any quotes.  Allows MAs, RLS, and Welford stats to warm up on real data
    # before committing capital.
    warmup_minutes_at_open: int = 15


@dataclass(slots=True, init=False)
class StrategyConfig:
    tick_size: float = 0.01
    # Per-symbol overrides for tick size.  Symbols not in this map fall back to
    # the global `tick_size`.  Useful when trading instruments with non-penny
    # increments (e.g. ETFs at $0.005 or futures at a different minimum).
    tick_size_by_symbol: dict[str, float] = field(default_factory=dict)
    min_trades_per_day: int = 200
    session_timezone: str = "America/New_York"
    session_open_local: LocalTime = LocalTime(9, 30)
    session_close_local: LocalTime = LocalTime(16, 0)

    def __init__(
        self,
        tick_size: float = 0.01,
        tick_size_by_symbol: dict[str, float] | None = None,
        min_trades_per_day: int | None = None,
        *,
        target_trades_per_day: int | None = None,
        session_timezone: str = "America/New_York",
        session_open_local: LocalTime = LocalTime(9, 30),
        session_close_local: LocalTime = LocalTime(16, 0),
    ) -> None:
        if min_trades_per_day is None and target_trades_per_day is None:
            min_trades_per_day = 200
        elif min_trades_per_day is None:
            min_trades_per_day = target_trades_per_day
        elif target_trades_per_day is not None and target_trades_per_day != min_trades_per_day:
            raise ValueError(
                "min_trades_per_day and target_trades_per_day must match when both are set"
            )

        self.tick_size = tick_size
        self.tick_size_by_symbol = dict(tick_size_by_symbol) if tick_size_by_symbol else {}
        self.min_trades_per_day = int(min_trades_per_day)
        self.session_timezone = session_timezone
        self.session_open_local = session_open_local
        self.session_close_local = session_close_local

    @property
    def target_trades_per_day(self) -> int:
        return self.min_trades_per_day


@dataclass(slots=True)
class RuntimeConfig:
    username: str
    password: str
    initiator_cfg: Path = Path("initiator.cfg")
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    def validate(self) -> None:
        if not self.username.strip():
            raise ValueError("username must be non-empty")
        if not self.password:
            raise ValueError("password must be non-empty")
        if not self.initiator_cfg:
            raise ValueError("initiator_cfg must be set")
        if self.telemetry.logger_flush_every <= 0:
            raise ValueError("logger_flush_every must be positive")
        if self.telemetry.logger_max_queue_size <= 0:
            raise ValueError("logger_max_queue_size must be positive")
        if self.dashboard.redraw_interval_ms <= 0:
            raise ValueError("redraw_interval_ms must be positive")
        if self.market_data.book_depth_levels <= 0:
            raise ValueError("book_depth_levels must be positive")
        if self.market_data.update_interval_ms <= 0:
            raise ValueError("update_interval_ms must be positive")
        if self.risk.max_position_lots_per_symbol <= 0:
            raise ValueError("max_position_lots_per_symbol must be positive")
        if self.risk.max_gross_position_lots < 0:
            raise ValueError("max_gross_position_lots must be non-negative")
        if not 0.0 <= self.risk.buying_power_reserve_fraction < 1.0:
            raise ValueError("buying_power_reserve_fraction must be in [0.0, 1.0)")
        if self.risk.reconcile_interval_ms <= 0:
            raise ValueError("reconcile_interval_ms must be positive")
        if (
            self.risk.max_gross_position_lots > 0
            and self.risk.max_gross_position_lots
            < self.risk.max_position_lots_per_symbol
        ):
            raise ValueError(
                "max_gross_position_lots must be >= max_position_lots_per_symbol"
            )
        if self.risk.stale_book_after_ms <= 0:
            raise ValueError("stale_book_after_ms must be positive")
        if self.risk.stale_order_after_ms <= 0:
            raise ValueError("stale_order_after_ms must be positive")
        if self.risk.flatten_start_minutes_to_close < 0:
            raise ValueError("flatten_start_minutes_to_close must be non-negative")
        if self.strategy.tick_size <= 0.0:
            raise ValueError("tick_size must be positive")
        if self.strategy.min_trades_per_day <= 0:
            raise ValueError("min_trades_per_day must be positive")
        if not self.strategy.session_timezone.strip():
            raise ValueError("session_timezone must be non-empty")
        if self.strategy.session_open_local >= self.strategy.session_close_local:
            raise ValueError("session_open_local must be earlier than session_close_local")

    def summary(self) -> dict[str, object]:
        return {
            "initiator_cfg": str(self.initiator_cfg),
            "telemetry": {
                "session_dir": str(self.telemetry.session_dir),
                "enable_event_logging": self.telemetry.enable_event_logging,
                "logger_flush_every": self.telemetry.logger_flush_every,
                "logger_max_queue_size": self.telemetry.logger_max_queue_size,
            },
            "dashboard": {
                "enabled": self.dashboard.enabled,
                "redraw_interval_ms": self.dashboard.redraw_interval_ms,
            },
            "market_data": {
                "symbols": self.market_data.symbols,
                "book_depth_levels": self.market_data.book_depth_levels,
                "update_interval_ms": self.market_data.update_interval_ms,
                "subscribe_all_books": self.market_data.subscribe_all_books,
            },
            "risk": {
                "max_position_lots_per_symbol": self.risk.max_position_lots_per_symbol,
                "max_gross_position_lots": self.risk.max_gross_position_lots,
                "buying_power_reserve_fraction": (
                    self.risk.buying_power_reserve_fraction
                ),
                "reconcile_interval_ms": self.risk.reconcile_interval_ms,
                "stale_book_after_ms": self.risk.stale_book_after_ms,
                "stale_order_after_ms": self.risk.stale_order_after_ms,
                "flatten_start_minutes_to_close": (
                    self.risk.flatten_start_minutes_to_close
                ),
            },
            "strategy": {
                "tick_size": self.strategy.tick_size,
                "min_trades_per_day": self.strategy.min_trades_per_day,
                "target_trades_per_day": self.strategy.min_trades_per_day,
                "session_timezone": self.strategy.session_timezone,
                "session_open_local": self.strategy.session_open_local.isoformat(),
                "session_close_local": self.strategy.session_close_local.isoformat(),
            },
        }


def build_runtime_config(
    *,
    username: str,
    password: str,
    symbols: Sequence[str],
    initiator_cfg: str | Path = "initiator.cfg",
) -> RuntimeConfig:
    config = RuntimeConfig(
        username=username,
        password=password,
        initiator_cfg=Path(initiator_cfg),
        market_data=MarketDataConfig(symbols=tuple(symbols)),
    )
    config.validate()
    return config
