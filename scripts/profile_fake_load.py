from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.app.main import bootstrap_once, build_runtime
from src.core.config import MarketDataConfig, RuntimeConfig, TelemetryConfig


@dataclass(slots=True)
class FakeBestPrice:
    bid_price: float
    bid_size: int
    ask_price: float
    ask_size: int

    def get_bid_price(self) -> float:
        return self.bid_price

    def get_bid_size(self) -> int:
        return self.bid_size

    def get_ask_price(self) -> float:
        return self.ask_price

    def get_ask_size(self) -> int:
        return self.ask_size

    def get_global_bid_price(self) -> float:
        return self.bid_price

    def get_global_bid_size(self) -> int:
        return self.bid_size + 2

    def get_global_ask_price(self) -> float:
        return self.ask_price

    def get_global_ask_size(self) -> int:
        return max(self.ask_size - 1, 1)

    def get_local_bid_price(self) -> float:
        return self.bid_price

    def get_local_bid_size(self) -> int:
        return self.bid_size

    def get_local_ask_price(self) -> float:
        return self.ask_price

    def get_local_ask_size(self) -> int:
        return self.ask_size


@dataclass(slots=True)
class FakeBookLevel:
    price: float
    size: int
    destination: str = "LOCAL"
    time: datetime = datetime.fromtimestamp(0, tz=timezone.utc)


@dataclass(slots=True)
class FakePortfolioSummary:
    total_bp: float = 1_000_000.0

    def get_total_bp(self) -> float:
        return self.total_bp

    def get_total_shares(self) -> int:
        return 0

    def get_total_realized_pl(self) -> float:
        return 0.0


class FakeOrderBookType:
    GLOBAL_BID = "GLOBAL_BID"
    GLOBAL_ASK = "GLOBAL_ASK"
    LOCAL_BID = "LOCAL_BID"
    LOCAL_ASK = "LOCAL_ASK"


class FakeTrader:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self._symbols = symbols
        self._connected = False
        self._tick = 0

    def connect(self, cfg_file: str, password: str) -> bool:
        del cfg_file, password
        self._connected = True
        return True

    def disconnect(self) -> bool:
        self._connected = False
        return True

    def is_connected(self) -> bool:
        return self._connected

    def sub_order_book(self, symbol: str) -> None:
        if symbol not in self._symbols:
            raise ValueError(f"unknown symbol: {symbol}")

    def sub_all_order_book(self) -> None:
        return

    def unsub_order_book(self, symbol: str) -> None:
        del symbol

    def get_subscribed_order_book_list(self) -> list[str]:
        return list(self._symbols)

    def submit_order(self, order) -> None:
        del order

    def submit_cancellation(self, order) -> None:
        del order

    def get_best_price(self, symbol: str) -> FakeBestPrice:
        symbol_idx = self._symbols.index(symbol)
        phase = (self._tick + symbol_idx) % 8
        bid_price = 100.0 + 0.01 * (phase - 4)
        spread = 0.01 if phase % 3 else 0.02
        bid_size = 2 + (phase % 5)
        ask_size = 2 + ((phase + 2) % 5)
        return FakeBestPrice(
            bid_price=bid_price,
            bid_size=bid_size,
            ask_price=bid_price + spread,
            ask_size=ask_size,
        )

    def get_order_book(self, symbol: str, book_type: str, max_level: int):
        symbol_idx = self._symbols.index(symbol)
        base_bid = 100.0 + 0.01 * (((self._tick + symbol_idx) % 8) - 4)
        is_bid = "BID" in str(book_type)
        is_global = "GLOBAL" in str(book_type)
        levels = 1 if is_global else max_level
        book = []
        for idx in range(levels):
            px_offset = -idx * 0.01 if is_bid else 0.01 + idx * 0.01
            size = 1 + ((self._tick + symbol_idx + idx) % 6)
            book.append(
                FakeBookLevel(
                    price=base_bid + px_offset,
                    size=size,
                    destination="GLOBAL" if is_global else "LOCAL",
                )
            )
        if symbol == self._symbols[-1] and "ASK" in str(book_type):
            self._tick += 1
        return book

    def get_waiting_list(self) -> list:
        return []

    def get_executed_orders(self, order_id: str) -> list:
        del order_id
        return []

    def get_portfolio_items(self) -> dict:
        return {}

    def get_portfolio_summary(self) -> FakePortfolioSummary:
        return FakePortfolioSummary()


def run_profile(symbols: tuple[str, ...], cycles: int, update_interval_ms: int) -> dict:
    runtime = build_runtime(
        RuntimeConfig(
            username="profile",
            password="fake",
            telemetry=TelemetryConfig(enable_event_logging=False),
            market_data=MarketDataConfig(
                symbols=symbols,
                book_depth_levels=5,
                update_interval_ms=update_interval_ms,
            ),
        )
    )
    bootstrap_once(
        runtime,
        trader_factory=lambda username: FakeTrader(symbols),
        order_book_type=FakeOrderBookType,
    )
    runtime.attach_default_market_maker()

    latencies_ns: list[int] = []
    tracemalloc.start()
    try:
        for _ in range(cycles):
            started_ns = time.perf_counter_ns()
            runtime.control_cycle_once(execute_orders=False)
            latencies_ns.append(time.perf_counter_ns() - started_ns)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        runtime.stop()

    latencies_us = [value / 1_000.0 for value in latencies_ns]
    return {
        "symbols": list(symbols),
        "cycles": cycles,
        "update_interval_ms": update_interval_ms,
        "latency_us_mean": statistics.fmean(latencies_us),
        "latency_us_p50": statistics.median(latencies_us),
        "latency_us_p95": sorted(latencies_us)[int(0.95 * (len(latencies_us) - 1))],
        "latency_us_max": max(latencies_us),
        "peak_memory_kb": peak_bytes / 1024.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile AppRuntime control-cycle latency against a deterministic fake trader.",
    )
    parser.add_argument("--symbols", nargs="+", default=["AAPL", "XOM"])
    parser.add_argument("--cycles", type=int, default=500)
    parser.add_argument("--update-interval-ms", type=int, default=50)
    args = parser.parse_args()

    profile = run_profile(
        symbols=tuple(args.symbols),
        cycles=args.cycles,
        update_interval_ms=args.update_interval_ms,
    )
    print(json.dumps(profile, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
