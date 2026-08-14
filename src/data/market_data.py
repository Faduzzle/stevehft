from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Protocol

from src.core.concurrency import SpscRingBuffer
from .book_cache import BookCache, TraderMarketDataLike
from .state import MarketState
from src.telemetry.logger import EventLoggerLike


@dataclass(slots=True)
class MarketDataLoopConfig:
    symbols: tuple[str, ...]
    max_depth: int = 5
    update_interval_ms: int = 100


@dataclass(slots=True)
class MarketDataLoopStats:
    iterations: int = 0
    symbols_refreshed: int = 0
    last_cycle_started_ns: int = 0
    last_cycle_completed_ns: int = 0


@dataclass(slots=True)
class MarketDataUpdateEvent:
    symbols: tuple[str, ...]
    write_seq: int
    cycle_started_ns: int
    cycle_completed_ns: int
    iterations: int
    market_state: MarketState
    overwritten_previous: bool = False


class StopSignalLike(Protocol):
    def is_set(self) -> bool: ...


class MarketDataLoop:
    def __init__(
        self,
        trader: TraderMarketDataLike,
        book_cache: BookCache,
        config: MarketDataLoopConfig,
        *,
        update_events: SpscRingBuffer[MarketDataUpdateEvent] | None = None,
        event_logger: Optional[EventLoggerLike] = None,
        before_refresh: Callable[[], bool] | None = None,
    ) -> None:
        self._trader = trader
        self._book_cache = book_cache
        self._config = config
        self._update_events = update_events
        self._event_logger = event_logger
        self._before_refresh = before_refresh
        self._stats = MarketDataLoopStats()

    @property
    def stats(self) -> MarketDataLoopStats:
        return self._stats

    def run_once(self) -> MarketState:
        cycle_start_ns = time.monotonic_ns()
        self._stats.last_cycle_started_ns = cycle_start_ns
        try:
            refresh_ready = (
                self._before_refresh() if self._before_refresh is not None else True
            )
        except Exception as exc:
            refresh_ready = False
            if self._event_logger is not None:
                self._event_logger.log(
                    "market_data_pre_refresh_exception",
                    symbols=self._config.symbols,
                    iterations=self._stats.iterations + 1,
                    cycle_started_ns=cycle_start_ns,
                    error=repr(exc),
                )
        if not refresh_ready:
            self._stats.iterations += 1
            self._stats.last_cycle_completed_ns = time.monotonic_ns()
            if self._event_logger is not None:
                self._event_logger.log(
                    "market_data_pre_refresh_failed",
                    symbols=self._config.symbols,
                    iterations=self._stats.iterations,
                    cycle_started_ns=self._stats.last_cycle_started_ns,
                    cycle_completed_ns=self._stats.last_cycle_completed_ns,
                )
            return self._book_cache.market_state
        for symbol in self._config.symbols:
            self._book_cache.refresh_symbol(
                self._trader,
                symbol,
                max_depth=self._config.max_depth,
                now_ns=cycle_start_ns,
            )
            self._stats.symbols_refreshed += 1

        self._stats.iterations += 1
        self._stats.last_cycle_completed_ns = time.monotonic_ns()
        event_write_seq = 0
        event_overwritten = False
        if self._update_events is not None:
            event = MarketDataUpdateEvent(
                symbols=self._config.symbols,
                write_seq=self._update_events.write_seq + 1,
                cycle_started_ns=self._stats.last_cycle_started_ns,
                cycle_completed_ns=self._stats.last_cycle_completed_ns,
                iterations=self._stats.iterations,
                market_state=self._book_cache.market_state.clone(),
            )
            event_overwritten = self._update_events.push_overwrite_oldest(event)
            event.overwritten_previous = event_overwritten
            event_write_seq = self._update_events.write_seq
        if self._event_logger is not None:
            self._event_logger.log(
                "market_data_cycle",
                symbols=self._config.symbols,
                iterations=self._stats.iterations,
                cycle_started_ns=self._stats.last_cycle_started_ns,
                cycle_completed_ns=self._stats.last_cycle_completed_ns,
                event_write_seq=event_write_seq,
                event_overwritten=event_overwritten,
            )
        return self._book_cache.market_state

    def run_until_stopped(self, stop_signal: StopSignalLike) -> None:
        interval_s = self._config.update_interval_ms / 1000.0
        while not stop_signal.is_set():
            cycle_start_ns = time.monotonic_ns()
            self.run_once()
            elapsed_s = (time.monotonic_ns() - cycle_start_ns) / 1_000_000_000.0
            sleep_s = max(interval_s - elapsed_s, 0.0)
            if sleep_s > 0.0:
                self._wait_for_stop(stop_signal, sleep_s)

    @staticmethod
    def _wait_for_stop(stop_signal: StopSignalLike, timeout_s: float) -> None:
        wait_fn = getattr(stop_signal, "wait", None)
        if callable(wait_fn):
            wait_fn(timeout_s)
            return
        time.sleep(timeout_s)
