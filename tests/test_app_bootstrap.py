from __future__ import annotations

import json
import threading
import time
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, time as LocalTime, timezone
from pathlib import Path

from src.app.live_smoke import (
    build_live_smoke_config,
    parse_args,
    run_live_shift_smoke,
    run_live_shift_smoke_until_stopped,
)
from src.app.main import bootstrap_once, build_runtime
from src.app.preflight import run_preflight_checks
from src.core.config import MarketDataConfig, RiskConfig, RuntimeConfig, StrategyConfig, TelemetryConfig
from src.core.concurrency import SpscRingBuffer
from src.core.session import build_shift_session
from src.core.session_clock import SessionClock
from src.data.book_cache import BookCache
from src.data.featurespace.lut import PiecewiseLinearLut
from src.data.featurespace.online_stats import DecayingWelford, PSquareQuantile
from src.data.market_data import MarketDataLoop, MarketDataLoopConfig
from src.data.state import BestPriceSnapshot, BookLevel, MarketState, SymbolState
from src.execution.order_router import OrderRouter
from src.execution.order_state import (
    FillRecord,
    OrderCommand,
    OrderIntentAction,
    OrderLedger,
    OrderLiquidity,
    OrderSide,
    QuoteTarget,
    WorkingOrder,
)
from src.execution.reconciler import Reconciler, ReconciliationConfig
from src.risk.inventory import PortfolioLedger
from src.risk.kill_switch import (
    KillSwitchController,
    ReconciliationHealth,
    SafeMode,
    SafeModeConfig,
)
from src.risk.limits import RiskLimits, RiskLimitsConfig
from src.strategy.market_maker import TopOfBookMarketMaker, TopOfBookMarketMakerConfig
from src.strategy.mm_feature_batch import build_market_making_feature_batch
from src.strategy.mm_pipeline import build_quote_plan
from src.strategy.cj_glft import estimate_cj_glft_parameters
from src.strategy.lead_lag_graph import OnlineLeadLagGraph, OnlineLeadLagGraphConfig
from src.strategy.params import (
    AdaptiveHistoryConfig,
    AdaptiveParameterProvider,
    ParameterLookupTables,
    StaticParameterProvider,
    SymbolStrategyParameters,
)
from src.strategy.signals import QuoteGateDecision, compute_inventory_skew
from src.strategy.strategy_allocator import ContextualStrategyAllocator
from src.telemetry.metrics import build_session_metrics
from src.telemetry.logger import JsonlEventLogger
from src.telemetry.recorder import build_session_telemetry


@dataclass
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
        return self.bid_size

    def get_global_ask_price(self) -> float:
        return self.ask_price

    def get_global_ask_size(self) -> int:
        return self.ask_size

    def get_local_bid_price(self) -> float:
        return self.bid_price

    def get_local_bid_size(self) -> int:
        return self.bid_size

    def get_local_ask_price(self) -> float:
        return self.ask_price

    def get_local_ask_size(self) -> int:
        return self.ask_size


@dataclass
class FakeOrderBookEntry:
    price: float
    size: int
    destination: str = "SIM"
    time: datetime | None = None


@dataclass
class FakePortfolioItem:
    long_shares: int = 0
    short_shares: int = 0
    long_price: float = 0.0
    short_price: float = 0.0
    realized_pl: float = 0.0

    def get_long_shares(self) -> int:
        return self.long_shares

    def get_short_shares(self) -> int:
        return self.short_shares

    def get_long_price(self) -> float:
        return self.long_price

    def get_short_price(self) -> float:
        return self.short_price

    def get_realized_pl(self) -> float:
        return self.realized_pl


@dataclass
class FakePortfolioSummary:
    total_bp: float = 1_000_000.0
    total_shares: int = 0
    total_realized_pl: float = 0.0

    def get_total_bp(self) -> float:
        return self.total_bp

    def get_total_shares(self) -> int:
        return self.total_shares

    def get_total_realized_pl(self) -> float:
        return self.total_realized_pl


class FakeOrderBookType:
    GLOBAL_BID = "global_bid"
    GLOBAL_ASK = "global_ask"
    LOCAL_BID = "local_bid"
    LOCAL_ASK = "local_ask"


@dataclass
class FakeWaitingOrder:
    id: str
    symbol: str
    type: str
    price: float
    size: int
    executed_size: int
    status: str


class FakeTrader:
    def __init__(self, *, bid_price_offsets: tuple[float, ...] = ()) -> None:
        self.connected = False
        self.subscribed_symbols: list[str] = []
        self.subscribe_all_calls = 0
        self.best_price_calls = 0
        self.waiting_list_calls = 0
        self.execution_poll_calls = 0
        self.submitted_orders: list[object] = []
        self.cancelled_orders: list[object] = []
        self.raise_waiting_list = False
        self.raise_execution_sync = False
        self.raise_portfolio_items = False
        self.raise_portfolio_summary = False
        self.raise_submit_order = False
        self.raise_submit_cancellation = False
        self.raise_sub_order_book = False
        self.raise_connect = False
        self.raise_last_trade_time = False
        self.bid_price_offsets = bid_price_offsets
        self.stock_list = ["AAPL", "XOM"]
        self.last_trade_time = datetime(2026, 4, 2, 10, 0, 0)
        self.last_price_by_symbol = {"AAPL": 100.02, "XOM": 100.02}
        self.last_size_by_symbol = {"AAPL": 3, "XOM": 3}
        self.waiting_orders: list[FakeWaitingOrder] = []
        self.portfolio_items: dict[str, FakePortfolioItem] = {"AAPL": FakePortfolioItem()}
        self.portfolio_summary = FakePortfolioSummary()

    def connect(self, cfg_file: str, password: str) -> bool:
        del cfg_file
        del password
        if self.raise_connect:
            return False
        self.connected = True
        return True

    def disconnect(self) -> bool:
        self.connected = False
        return True

    def is_connected(self) -> bool:
        return self.connected

    def sub_order_book(self, symbol: str) -> None:
        if self.raise_sub_order_book:
            raise RuntimeError("subscription failed")
        self.subscribed_symbols.append(symbol)

    def sub_all_order_book(self) -> None:
        self.subscribe_all_calls += 1
        self.subscribed_symbols = list(self.stock_list)

    def unsub_order_book(self, symbol: str) -> None:
        if symbol in self.subscribed_symbols:
            self.subscribed_symbols.remove(symbol)

    def get_subscribed_order_book_list(self) -> list[str]:
        return list(self.subscribed_symbols)

    def get_stock_list(self) -> list[str]:
        return list(self.stock_list)

    def get_last_trade_time(self) -> datetime:
        if self.raise_last_trade_time:
            raise RuntimeError("last-trade-time poll failed")
        return self.last_trade_time

    def get_last_price(self, symbol: str) -> float:
        return self.last_price_by_symbol.get(symbol, 0.0)

    def get_last_size(self, symbol: str) -> int:
        return self.last_size_by_symbol.get(symbol, 0)

    def get_best_price(self, symbol: str) -> FakeBestPrice:
        del symbol
        self.best_price_calls += 1
        if self.bid_price_offsets:
            path_index = min(self.best_price_calls - 1, len(self.bid_price_offsets) - 1)
            bid_price = 100.0 + self.bid_price_offsets[path_index]
        else:
            bid_price = 100.0 + 0.01 * (self.best_price_calls - 1)
        ask_price = bid_price + 0.02
        return FakeBestPrice(bid_price=bid_price, bid_size=4, ask_price=ask_price, ask_size=6)

    def get_order_book(self, symbol: str, book_type, max_level: int = 99) -> list[FakeOrderBookEntry]:
        del symbol
        del max_level
        if book_type in {FakeOrderBookType.GLOBAL_BID, FakeOrderBookType.LOCAL_BID}:
            return [
                FakeOrderBookEntry(price=100.00, size=4),
                FakeOrderBookEntry(price=99.99, size=8),
            ]
        return [
            FakeOrderBookEntry(price=100.02, size=6),
            FakeOrderBookEntry(price=100.03, size=10),
        ]

    def get_waiting_list(self) -> list:
        self.waiting_list_calls += 1
        if self.raise_waiting_list:
            raise RuntimeError("waiting-list poll failed")
        return list(self.waiting_orders)

    def get_executed_orders(self, order_id: str) -> list:
        del order_id
        self.execution_poll_calls += 1
        if self.raise_execution_sync:
            raise RuntimeError("execution poll failed")
        return []

    def get_portfolio_items(self) -> dict[str, FakePortfolioItem]:
        if self.raise_portfolio_items:
            raise RuntimeError("portfolio items poll failed")
        return dict(self.portfolio_items)

    def get_portfolio_summary(self) -> FakePortfolioSummary:
        if self.raise_portfolio_summary:
            raise RuntimeError("portfolio summary poll failed")
        return self.portfolio_summary

    def submit_order(self, order) -> None:
        if self.raise_submit_order:
            raise RuntimeError("submit failed")
        self.submitted_orders.append(order)

    def submit_cancellation(self, order) -> None:
        if self.raise_submit_cancellation:
            raise RuntimeError("cancel failed")
        self.cancelled_orders.append(order)


@dataclass
class FakeBrokerOrder:
    id: str
    symbol: str
    side: OrderSide
    size: int
    price: float = 0.0


class FakeOrderFactory:
    def __init__(self) -> None:
        self.next_id = 1

    def build_limit_order(self, symbol: str, side: OrderSide, size: int, price: float):
        order = FakeBrokerOrder(
            id=f"limit-{self.next_id}",
            symbol=symbol,
            side=side,
            size=size,
            price=price,
        )
        self.next_id += 1
        return order

    def build_market_order(self, symbol: str, side: OrderSide, size: int):
        order = FakeBrokerOrder(
            id=f"market-{self.next_id}",
            symbol=symbol,
            side=side,
            size=size,
            price=0.0,
        )
        self.next_id += 1
        return order

    def build_cancel_order(self, live_order: WorkingOrder):
        order = FakeBrokerOrder(
            id=f"cancel-{live_order.order_id}",
            symbol=live_order.symbol,
            side=live_order.side,
            size=max(live_order.remaining_size, 1),
            price=live_order.price,
        )
        self.next_id += 1
        return order


class FakeStopSignal:
    def __init__(self) -> None:
        self._is_set = False
        self.wait_timeouts: list[float] = []

    def is_set(self) -> bool:
        return self._is_set

    def wait(self, timeout_s: float) -> bool:
        self.wait_timeouts.append(timeout_s)
        self._is_set = True
        return True


def attach_open_session_clock(runtime) -> None:
    runtime.session_clock = SessionClock(
        runtime.config.runtime.strategy,
        runtime.config.runtime.risk,
        now_provider=lambda: datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc),
    )


class BootstrapRuntimeTest(unittest.TestCase):
    def test_bootstrap_builds_market_and_trading_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = build_runtime(
                RuntimeConfig(
                    username="tester",
                    password="secret",
                    initiator_cfg=Path("initiator.cfg"),
                    telemetry=TelemetryConfig(),
                    market_data=MarketDataConfig(symbols=("AAPL",)),
                )
            )
            runtime.config.runtime.telemetry.enable_event_logging = False
            runtime.config.runtime.telemetry.session_dir = Path(tmpdir) / "runs"

            trader = FakeTrader()
            market_state = bootstrap_once(
                runtime,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )

            self.assertTrue(runtime.started)
            self.assertIs(runtime.market_state, market_state)
            self.assertIsNotNone(runtime.market_data_loop)
            self.assertIsNotNone(runtime.book_cache)
            self.assertIsNotNone(runtime.session)
            self.assertIsNotNone(runtime.trading)
            self.assertIn("AAPL", market_state.by_symbol)
            self.assertEqual(runtime.session.status.subscribed_symbols, ("AAPL",))
            self.assertAlmostEqual(market_state.by_symbol["AAPL"].spread, 0.02, places=8)
            self.assertEqual(runtime.trading.portfolio_ledger.summary.total_bp, 1_000_000.0)
            self.assertTrue(runtime.session.status.connected)

            runtime.stop()
            self.assertFalse(trader.connected)

    def test_spsc_ring_buffer_push_pop_sequences_and_overwrite(self) -> None:
        queue = SpscRingBuffer[str](capacity=2)

        self.assertTrue(queue.try_push("a"))
        self.assertTrue(queue.try_push("b"))
        self.assertFalse(queue.try_push("c"))
        self.assertEqual(queue.write_seq, 2)
        self.assertEqual(queue.read_seq, 0)

        self.assertEqual(queue.try_pop(), "a")
        self.assertEqual(queue.write_seq, 2)
        self.assertEqual(queue.read_seq, 1)

        self.assertFalse(queue.push_overwrite_oldest("c"))
        self.assertTrue(queue.push_overwrite_oldest("d"))
        self.assertEqual(queue.write_seq, 4)
        self.assertEqual(queue.read_seq, 2)
        self.assertEqual(queue.drain(), ["c", "d"])
        self.assertEqual(queue.try_pop(), None)

    def test_spsc_ring_buffer_wait_pop_returns_item_or_none_on_timeout(self) -> None:
        queue = SpscRingBuffer[str](capacity=2)

        self.assertIsNone(queue.wait_pop(timeout_s=0.02))
        self.assertTrue(queue.try_push("event"))
        self.assertEqual(queue.wait_pop(timeout_s=0.02), "event")

    def test_decaying_welford_tracks_recent_mean_and_variance(self) -> None:
        stats = DecayingWelford(decay=0.5)

        for value in (10.0, 10.0, 10.0, 20.0):
            stats.update(value)

        self.assertTrue(stats.initialized)
        self.assertGreater(stats.mean, 10.0)
        self.assertLess(stats.mean, 20.0)
        self.assertGreater(stats.variance, 0.0)

    def test_p_square_quantile_tracks_high_quantile_without_full_history(self) -> None:
        quantile = PSquareQuantile(0.90)

        for value in range(1, 101):
            quantile.update(float(value))

        self.assertEqual(quantile.sample_count, 100)
        self.assertGreater(quantile.estimate, 80.0)
        self.assertLess(quantile.estimate, 100.0)

    def test_market_data_loop_marks_overwritten_events_on_queue_pressure(self) -> None:
        trader = FakeTrader()
        market_state = MarketState()
        book_cache = BookCache(
            market_state,
            order_book_type=FakeOrderBookType,
        )
        update_events = SpscRingBuffer(capacity=1)
        market_data_loop = MarketDataLoop(
            trader,
            book_cache,
            MarketDataLoopConfig(symbols=("AAPL",), update_interval_ms=1),
            update_events=update_events,
        )

        market_data_loop.run_once()
        market_data_loop.run_once()
        event = update_events.try_pop()

        self.assertIsNotNone(event)
        assert event is not None
        self.assertTrue(event.overwritten_previous)
        self.assertEqual(event.write_seq, update_events.write_seq)

    def test_runtime_run_cycles_advances_market_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = build_runtime(
                RuntimeConfig(
                    username="tester",
                    password="secret",
                    initiator_cfg=Path("initiator.cfg"),
                    telemetry=TelemetryConfig(),
                    market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                )
            )
            runtime.config.runtime.telemetry.enable_event_logging = False
            runtime.config.runtime.telemetry.session_dir = Path(tmpdir) / "runs"

            trader = FakeTrader()
            first_state = bootstrap_once(
                runtime,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )
            first_mid = first_state.by_symbol["AAPL"].mid_price

            advanced_state = runtime.run_cycles(3)
            advanced_mid = advanced_state.by_symbol["AAPL"].mid_price

            self.assertGreater(trader.best_price_calls, 1)
            self.assertGreater(runtime.loop_stats.iterations, 1)
            self.assertGreater(advanced_mid, first_mid)

            runtime.stop()

    def test_poll_once_throttles_reconciliation_independently_from_market_data_refresh(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                risk=RiskConfig(
                    reconcile_interval_ms=5_000,
                    stale_book_after_ms=6_000,
                    stale_order_after_ms=7_000,
                ),
            )
        )
        trader = FakeTrader()
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        waiting_list_calls_before = trader.waiting_list_calls
        best_price_calls_before = trader.best_price_calls

        runtime.poll_once()

        self.assertEqual(trader.waiting_list_calls, waiting_list_calls_before)
        self.assertEqual(trader.best_price_calls, best_price_calls_before + 1)
        runtime.stop()

    def test_market_maker_applies_gross_budget_before_routing_low_priority_symbols(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL", "XOM")),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        provider = StaticParameterProvider(
            overrides={
                "AAPL": SymbolStrategyParameters(
                    quote_size_lots=1,
                    passive_ladder_depth_levels=1,
                    allocation_weight=1.0,
                    queue_fill_support=1.0,
                    slippage_quality_score=1.0,
                ),
                "XOM": SymbolStrategyParameters(
                    quote_size_lots=1,
                    passive_ladder_depth_levels=1,
                    allocation_weight=0.5,
                    queue_fill_support=0.0,
                    slippage_quality_score=0.8,
                ),
            }
        )
        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            TopOfBookMarketMakerConfig(max_gross_position_lots=1),
            parameter_provider=provider,
        )

        targets, traces = strategy.generate_targets(
            portfolio_ledger=PortfolioLedger(),
        )
        targets_by_symbol = {target.symbol: target for target in targets}
        traces_by_symbol = {trace.symbol: trace for trace in traces}

        self.assertNotEqual(
            targets_by_symbol["AAPL"].reason,
            "gross_budget_suppressed",
        )
        self.assertTrue(targets_by_symbol["AAPL"].enable_ask)
        self.assertFalse(targets_by_symbol["XOM"].enable_bid)
        self.assertFalse(targets_by_symbol["XOM"].enable_ask)
        self.assertEqual(targets_by_symbol["XOM"].reason, "gross_budget_suppressed")
        self.assertEqual(
            traces_by_symbol["XOM"].diagnostics.participation_mode,
            "suppressed",
        )
        self.assertEqual(
            traces_by_symbol["XOM"].decision_reason,
            "gross_budget_suppressed",
        )

        runtime.stop()

    def test_market_maker_gross_budget_counts_existing_live_orders(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL", "XOM")),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        assert runtime.trading is not None
        runtime.trading.order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="AAPL-LIVE-BID",
                price=100.0,
                size=1,
                status="new",
                liquidity=OrderLiquidity.LIMIT,
            )
        )

        provider = StaticParameterProvider(
            overrides={
                "AAPL": SymbolStrategyParameters(
                    quote_size_lots=1,
                    passive_ladder_depth_levels=1,
                    allocation_weight=1.0,
                    queue_fill_support=1.0,
                    slippage_quality_score=1.0,
                ),
                "XOM": SymbolStrategyParameters(
                    quote_size_lots=1,
                    passive_ladder_depth_levels=1,
                    allocation_weight=0.5,
                    queue_fill_support=0.0,
                    slippage_quality_score=0.8,
                ),
            }
        )
        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            TopOfBookMarketMakerConfig(max_gross_position_lots=1),
            parameter_provider=provider,
            order_ledger=runtime.trading.order_ledger,
        )

        targets, _ = strategy.generate_targets(
            portfolio_ledger=runtime.trading.portfolio_ledger,
        )
        targets_by_symbol = {target.symbol: target for target in targets}

        self.assertNotEqual(
            targets_by_symbol["AAPL"].reason,
            "gross_budget_suppressed",
        )
        self.assertFalse(targets_by_symbol["XOM"].enable_bid)
        self.assertFalse(targets_by_symbol["XOM"].enable_ask)
        self.assertEqual(
            targets_by_symbol["XOM"].reason,
            "gross_budget_suppressed",
        )

        runtime.stop()

    def test_control_cycle_handles_price_reversal_path(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                # Quoting is gated behind warmup_minutes_at_open real wall-clock
                # minutes since the provider's first observation (handles a
                # mid-session process restart); disable it so a single-cycle
                # test can observe quoting immediately.
                risk=RiskConfig(warmup_minutes_at_open=0),
            )
        )
        attach_open_session_clock(runtime)

        trader = FakeTrader(bid_price_offsets=(0.00, 0.00, 0.00, 0.05, -0.02))
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        runtime.attach_default_market_maker()

        up_state = runtime.control_cycle_once(execute_orders=False)
        up_mid = up_state.by_symbol["AAPL"].mid_price
        up_commands, up_traces = runtime.run_strategy_once(execute_orders=False)

        down_state = runtime.control_cycle_once(execute_orders=False)
        down_mid = down_state.by_symbol["AAPL"].mid_price
        down_commands, down_traces = runtime.run_strategy_once(execute_orders=False)

        self.assertGreater(up_mid, 100.0)
        self.assertLess(down_mid, up_mid)
        self.assertEqual(len(up_commands), 4)
        self.assertEqual(len(down_commands), 4)
        self.assertEqual(
            {
                (command.side.value, command.level_index)
                for command in up_commands
            },
            {("bid", 0), ("bid", 1), ("ask", 0), ("ask", 1)},
        )
        self.assertEqual(len(up_traces), 1)
        self.assertEqual(len(down_traces), 1)
        self.assertLess(
            down_traces[0].diagnostics.fair_value_center,
            up_traces[0].diagnostics.fair_value_center,
        )

        runtime.stop()

    def test_runtime_run_until_stopped_uses_interruptible_wait_when_available(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=5_000),
            )
        )
        trader = FakeTrader()
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        stop_signal = FakeStopSignal()
        iterations_before = runtime.loop_stats.iterations

        runtime.run_until_stopped(stop_signal, execute_orders=False)

        self.assertTrue(stop_signal.wait_timeouts)
        self.assertGreater(stop_signal.wait_timeouts[0], 0.0)
        self.assertEqual(runtime.loop_stats.iterations, iterations_before + 1)

        runtime.stop()

    def test_runtime_run_event_driven_until_stopped_consumes_market_data_events(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
            )
        )
        attach_open_session_clock(runtime)

        trader = FakeTrader()
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        runtime.attach_default_market_maker()
        assert runtime.market_data_events is not None

        stop_signal = threading.Event()
        stopper = threading.Timer(0.05, stop_signal.set)
        stopper.start()
        runtime.run_event_driven_until_stopped(stop_signal, execute_orders=False)
        stopper.join(timeout=1.0)

        self.assertIsNone(runtime.market_data_thread)
        self.assertIsNone(runtime.market_data_stop_signal)
        self.assertGreaterEqual(runtime.loop_stats.iterations, 5)
        self.assertGreater(runtime.loop_stats.last_market_data_event_seq, 0)
        self.assertGreaterEqual(runtime.market_data_events.write_seq, runtime.loop_stats.last_market_data_event_seq)

        runtime.stop()

    def test_runtime_strategy_tick_size_flows_into_ledger_reconciler_and_default_market_maker(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                strategy=StrategyConfig(tick_size=0.05),
            )
        )

        bootstrap_once(
            runtime,
            trader_factory=lambda username: FakeTrader(),
            order_book_type=FakeOrderBookType,
        )
        strategy = runtime.attach_default_market_maker()

        assert runtime.trading is not None
        self.assertAlmostEqual(runtime.trading.order_ledger.tick_size, 0.05, places=8)
        self.assertAlmostEqual(runtime.trading.reconciler._config.tick_size, 0.05, places=8)
        self.assertAlmostEqual(strategy._config.tick_size, 0.05, places=8)

        runtime.stop()

    def test_book_cache_filters_own_acknowledged_orders_from_local_book_only(self) -> None:
        market_state = MarketState()
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="own-bid",
                price=100.00,
                size=2,
                executed_size=0,
                status="new",
            )
        )
        book_cache = BookCache(
            market_state,
            order_book_type=FakeOrderBookType,
            order_ledger=order_ledger,
        )
        trader = FakeTrader()

        state = book_cache.refresh_symbol(trader, "AAPL", max_depth=2, now_ns=1)

        self.assertEqual(state.global_bids[0].size, 4)
        self.assertEqual(state.local_bids[0].size, 2)
        self.assertEqual(state.best_price.local_bid_sz, 2)
        self.assertEqual(state.local_asks[0].size, 6)

    def test_book_cache_does_not_filter_pending_new_orders_before_exchange_ack(self) -> None:
        market_state = MarketState()
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="pending-bid",
                price=100.00,
                size=2,
                executed_size=0,
                status="pending_new",
            )
        )
        book_cache = BookCache(
            market_state,
            order_book_type=FakeOrderBookType,
            order_ledger=order_ledger,
        )

        state = book_cache.refresh_symbol(FakeTrader(), "AAPL", max_depth=2, now_ns=1)

        self.assertEqual(state.local_bids[0].size, 4)

    def test_book_cache_filters_displaced_live_same_side_orders_during_replace_race(self) -> None:
        market_state = MarketState()
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="old-bid",
                price=99.99,
                size=3,
                executed_size=0,
                status="new",
                pending_cancel=True,
            )
        )
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="new-bid",
                price=100.00,
                size=2,
                executed_size=0,
                status="new",
            )
        )
        book_cache = BookCache(
            market_state,
            order_book_type=FakeOrderBookType,
            order_ledger=order_ledger,
        )

        state = book_cache.refresh_symbol(FakeTrader(), "AAPL", max_depth=2, now_ns=1)

        self.assertEqual([(level.price, level.size) for level in state.local_bids], [(100.0, 2), (99.99, 5)])

    def test_book_cache_computes_global_local_and_trade_linked_voi_inputs(self) -> None:
        market_state = MarketState()
        book_cache = BookCache(
            market_state,
            order_book_type=FakeOrderBookType,
        )
        trader = FakeTrader()
        trader.last_price_by_symbol["AAPL"] = 100.02
        trader.last_size_by_symbol["AAPL"] = 7

        state = book_cache.refresh_symbol(trader, "AAPL", max_depth=2, now_ns=1)

        self.assertLess(state.global_l1_voi, 0.0)
        self.assertLess(state.local_multi_level_voi, 0.0)
        self.assertEqual(state.last_trade_price, 100.02)
        self.assertEqual(state.last_trade_size, 7)
        self.assertGreater(state.trade_signed_volume, 0.0)

    def test_strategy_cycle_generates_quote_commands_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = build_runtime(
                RuntimeConfig(
                    username="tester",
                    password="secret",
                    initiator_cfg=Path("initiator.cfg"),
                    telemetry=TelemetryConfig(),
                    market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                    # See test_control_cycle_handles_price_reversal_path for why
                    # this is needed for a single-cycle test.
                    risk=RiskConfig(warmup_minutes_at_open=0),
                )
            )
            attach_open_session_clock(runtime)
            runtime.config.runtime.telemetry.enable_event_logging = False
            runtime.config.runtime.telemetry.session_dir = Path(tmpdir) / "runs"

            trader = FakeTrader()
            bootstrap_once(
                runtime,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )
            runtime.attach_default_market_maker()

            commands, traces = runtime.run_strategy_once(execute_orders=False)

            self.assertEqual(len(traces), 1)
            self.assertEqual(len(commands), 4)
            self.assertEqual({command.side.value for command in commands}, {"bid", "ask"})
            self.assertEqual(
                {
                    (command.side.value, command.level_index)
                    for command in commands
                },
                {("bid", 0), ("bid", 1), ("ask", 0), ("ask", 1)},
            )
            self.assertEqual(
                {command.action for command in commands},
                {OrderIntentAction.SUBMIT},
            )
            self.assertIn("live_quote_age_ms", traces[0].diagnostics.extra)
            self.assertIn("recent_fill_rate", traces[0].diagnostics.extra)
            self.assertIn("recent_cancel_rate", traces[0].diagnostics.extra)
            self.assertIn("cj_inventory_skew_ticks_per_lot", traces[0].diagnostics.extra)
            self.assertIn("glft_half_width_ticks", traces[0].diagnostics.extra)
            self.assertGreater(traces[0].diagnostics.extra["glft_arrival_intensity"], 0.0)
            self.assertEqual(
                len(traces[0].quote_target.bid_levels),
                2,
            )
            self.assertEqual(
                len(traces[0].quote_target.ask_levels),
                2,
            )

            runtime.stop()

    def test_strategy_cycle_is_skipped_when_session_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = build_runtime(
                RuntimeConfig(
                    username="tester",
                    password="secret",
                    initiator_cfg=Path("initiator.cfg"),
                    telemetry=TelemetryConfig(enable_event_logging=False),
                    market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                    strategy=StrategyConfig(
                        target_trades_per_day=200,
                        session_timezone="UTC",
                        session_open_local=LocalTime(9, 30),
                        session_close_local=LocalTime(16, 0),
                    ),
                )
            )
            runtime.config.runtime.telemetry.session_dir = Path(tmpdir) / "runs"
            runtime.session_clock = SessionClock(
                runtime.config.runtime.strategy,
                runtime.config.runtime.risk,
                now_provider=lambda: datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc),
            )

            trader = FakeTrader()
            trader.last_trade_time = datetime(2026, 1, 2, 17, 0, tzinfo=timezone.utc)
            bootstrap_once(
                runtime,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )
            runtime.attach_default_market_maker()

            commands, traces = runtime.run_strategy_once(execute_orders=True)

            self.assertEqual(commands, [])
            self.assertEqual(traces, [])
            self.assertEqual(trader.submitted_orders, [])

            runtime.stop()

    def test_live_smoke_runner_bootstraps_runs_bounded_cycles_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader = FakeTrader()
            runtime_config = build_live_smoke_config(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                symbols=("AAPL",),
                tick_size=0.05,
                session_dir=Path(tmpdir) / "live_smoke",
                update_interval_ms=1,
                book_depth_levels=2,
            )

            self.assertAlmostEqual(runtime_config.strategy.tick_size, 0.05, places=8)

            runtime = run_live_shift_smoke(
                runtime_config,
                cycles=3,
                execute_orders=False,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )

            self.assertFalse(runtime.started)
            self.assertEqual(runtime.loop_stats.iterations, 6)
            self.assertIsNotNone(runtime.market_state)
            assert runtime.market_state is not None
            self.assertIn("AAPL", runtime.market_state.by_symbol)
            self.assertFalse(trader.connected)

    def test_live_smoke_parse_args_allows_omitting_symbols_for_broker_discovery(self) -> None:
        args = parse_args([])

        from credentials import my_password, my_username

        self.assertEqual(args.username, my_username)
        self.assertEqual(args.password, my_password)
        self.assertEqual(args.symbols, ())
        self.assertIsNone(args.cycles)

    def test_live_smoke_parse_args_keeps_bounded_cycles_when_explicit(self) -> None:
        args = parse_args(["--cycles", "3"])

        self.assertEqual(args.cycles, 3)

    def test_bootstrap_once_discovers_symbols_from_trader_stock_list_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "initiator.cfg"
            cfg_path.write_text("cfg", encoding="utf-8")
            runtime = build_runtime(
                RuntimeConfig(
                    username="demo",
                    password="secret",
                    initiator_cfg=cfg_path,
                    telemetry=TelemetryConfig(session_dir=Path(tmpdir) / "telemetry"),
                    market_data=MarketDataConfig(symbols=(), update_interval_ms=1),
                )
            )
            trader = FakeTrader()
            trader.stock_list = ["aapl", "XOM", ""]

            bootstrap_once(
                runtime,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )

            self.assertEqual(runtime.config.runtime.market_data.symbols, ("AAPL", "XOM"))
            assert runtime.session is not None
            self.assertEqual(runtime.session.status.subscribed_symbols, ("AAPL", "XOM"))
            self.assertEqual(trader.subscribed_symbols, ["AAPL", "XOM"])
            assert runtime.market_state is not None
            self.assertIn("AAPL", runtime.market_state.by_symbol)
            self.assertIn("XOM", runtime.market_state.by_symbol)
            runtime.stop()

    def test_bootstrap_once_uses_broker_last_trade_time_for_session_clock(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                strategy=StrategyConfig(
                    session_timezone="UTC",
                    session_open_local=LocalTime(9, 30),
                    session_close_local=LocalTime(16, 0),
                ),
            )
        )
        trader = FakeTrader()
        trader.last_trade_time = datetime(2026, 4, 2, 10, 45, 0, tzinfo=timezone.utc)

        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )

        assert runtime.session_clock is not None
        progress = runtime.session_clock.snapshot()
        self.assertTrue(progress.is_session_open)
        self.assertEqual(progress.now_local.hour, 10)
        self.assertEqual(progress.now_local.minute, 45)
        runtime.stop()

    def test_session_clock_reuses_last_broker_time_after_clock_poll_failure(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                strategy=StrategyConfig(
                    session_timezone="UTC",
                    session_open_local=LocalTime(9, 30),
                    session_close_local=LocalTime(16, 0),
                ),
            )
        )
        trader = FakeTrader()
        trader.last_trade_time = datetime(2026, 4, 2, 10, 45, 0, tzinfo=timezone.utc)

        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )

        assert runtime.session_clock is not None
        first_snapshot = runtime.session_clock.snapshot()
        trader.raise_last_trade_time = True
        second_snapshot = runtime.session_clock.snapshot()

        self.assertEqual(second_snapshot.now_local, first_snapshot.now_local)
        self.assertTrue(second_snapshot.is_session_open)
        runtime.stop()

    def test_poll_once_repairs_missing_symbol_subscription(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(
                    symbols=("AAPL", "XOM"),
                    update_interval_ms=1,
                ),
            )
        )
        trader = FakeTrader()

        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        trader.unsub_order_book("XOM")

        runtime.poll_once()

        self.assertEqual(runtime.session.status.subscribed_symbols, ("AAPL", "XOM"))
        self.assertEqual(trader.subscribed_symbols.count("XOM"), 1)
        runtime.stop()

    def test_poll_once_reconnects_and_resubscribes_after_broker_disconnect(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
            )
        )
        trader = FakeTrader()

        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        trader.connected = False
        trader.subscribed_symbols = []

        runtime.poll_once()

        self.assertTrue(trader.connected)
        self.assertEqual(runtime.session.status.subscribed_symbols, ("AAPL",))
        self.assertEqual(trader.subscribed_symbols, ["AAPL"])
        runtime.stop()

    def test_poll_once_skips_book_refresh_when_reconnect_fails(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
            )
        )
        trader = FakeTrader()

        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        trader.connected = False
        trader.subscribed_symbols = []
        trader.raise_connect = True
        best_price_calls_before = trader.best_price_calls

        runtime.poll_once()

        self.assertFalse(runtime.session.status.connected)
        self.assertEqual(trader.best_price_calls, best_price_calls_before)
        runtime.stop()

    def test_bootstrap_once_disconnects_and_clears_partial_state_on_subscription_failure(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
            )
        )
        trader = FakeTrader()
        trader.raise_sub_order_book = True

        with self.assertRaises(RuntimeError):
            bootstrap_once(
                runtime,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )

        self.assertFalse(trader.connected)
        self.assertIsNone(runtime.session)
        self.assertIsNone(runtime.trading)
        self.assertIsNone(runtime.book_cache)
        self.assertIsNone(runtime.market_data_loop)
        self.assertIsNone(runtime.market_data_events)
        self.assertIsNone(runtime.market_state)
        runtime.stop()

    def test_bootstrap_once_applies_position_baseline_on_restart_inventory(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
            )
        )
        trader = FakeTrader()
        trader.portfolio_items = {
            "AAPL": FakePortfolioItem(
                long_shares=100,
                short_shares=0,
                long_price=100.0,
                short_price=0.0,
                realized_pl=0.0,
            )
        }

        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )

        assert runtime.trading is not None
        self.assertEqual(runtime.trading.order_ledger.get_net_executed_lots("AAPL"), 1)
        self.assertEqual(runtime.trading.kill_switch.mode, SafeMode.NORMAL)
        runtime.stop()

    def test_restart_bootstrap_recovers_live_waiting_order_and_does_not_duplicate_submit(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
            )
        )
        trader = FakeTrader()
        trader.waiting_orders = [
            FakeWaitingOrder(
                id="preexisting-bid",
                symbol="AAPL",
                type="LIMIT_BUY",
                price=100.02,
                size=1,
                executed_size=0,
                status="NEW",
            )
        ]

        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        assert runtime.trading is not None
        live_bid = runtime.trading.order_ledger.find_by_order_id("preexisting-bid")
        self.assertIsNotNone(live_bid)
        assert live_bid is not None
        self.assertTrue(live_bid.is_live)
        self.assertAlmostEqual(live_bid.price, 100.02, places=8)
        self.assertEqual(live_bid.size, 1)

        runtime.strategy = TopOfBookMarketMaker(
            runtime.market_state,
            parameter_provider=StaticParameterProvider(
                default=SymbolStrategyParameters(
                    quote_size_lots=1,
                    passive_ladder_depth_levels=1,
                    spread_floor_ticks=1,
                    spread_ceiling_ticks=2,
                    max_live_spread_ticks=10,
                    quote_age_limit_ms=10_000,
                    microprice_weight=0.0,
                    imbalance_shift_ticks=0.0,
                    cj_inventory_skew_ticks_per_lot=0.0,
                    glft_half_width_ticks=1.0,
                    symbol_enable_flag=True,
                    bid_enable_flag=True,
                    ask_enable_flag=False,
                )
            ),
        )

        commands, _ = runtime.run_strategy_once(execute_orders=False)

        self.assertEqual(commands, [])
        self.assertEqual(trader.submitted_orders, [])
        self.assertIs(
            runtime.trading.order_ledger.find_by_order_id("preexisting-bid"),
            live_bid,
        )
        runtime.stop()

    def test_bootstrap_once_fails_if_startup_reconciliation_cannot_reach_normal_mode(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
            )
        )
        trader = FakeTrader()
        trader.waiting_orders = [
            FakeWaitingOrder(
                id="bad-order",
                symbol="AAPL",
                type="UNKNOWN_TYPE",
                price=100.0,
                size=1,
                executed_size=0,
                status="NEW",
            )
        ]

        with self.assertRaisesRegex(RuntimeError, "startup reconciliation did not reach normal mode"):
            bootstrap_once(
                runtime,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )

        self.assertFalse(trader.connected)
        self.assertIsNone(runtime.session)
        self.assertIsNone(runtime.trading)
        runtime.stop()

    def test_preflight_report_accepts_dry_run_and_rejects_bad_live_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dry_run_config = build_live_smoke_config(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                symbols=("AAPL", "XOM"),
                session_dir=Path(tmpdir) / "dry_run",
                update_interval_ms=50,
                book_depth_levels=3,
            )
            dry_run_report = run_preflight_checks(
                dry_run_config,
                execute_orders=False,
            )

            self.assertTrue(dry_run_report.passed)
            self.assertTrue(
                any(
                    "dry-run mode selected" in check
                    for check in dry_run_report.checks
                )
            )

            bad_live_config = RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("missing-initiator.cfg"),
                telemetry=TelemetryConfig(session_dir=Path(tmpdir) / "live_run"),
                market_data=MarketDataConfig(
                    symbols=("AAPL", "aapl"),
                    update_interval_ms=1_000,
                ),
                risk=RiskConfig(stale_book_after_ms=250),
                strategy=StrategyConfig(tick_size=0.01),
            )
            bad_live_report = run_preflight_checks(
                bad_live_config,
                execute_orders=True,
            )

            self.assertFalse(bad_live_report.passed)
            self.assertTrue(
                any("duplicate symbols" in error for error in bad_live_report.errors)
            )
            self.assertTrue(
                any("update_interval_ms" in error for error in bad_live_report.errors)
            )
            self.assertTrue(
                any("initiator config file does not exist" in error for error in bad_live_report.errors)
            )

    def test_preflight_warns_when_session_dir_reuses_existing_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "reused_session"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "events.jsonl").write_text(
                '{"kind":"old_run","ts_ns":1,"payload":{}}\n',
                encoding="utf-8",
            )
            config = build_live_smoke_config(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                symbols=("AAPL",),
                session_dir=session_dir,
            )

            report = run_preflight_checks(config, execute_orders=False)

            self.assertTrue(report.passed)
            self.assertTrue(
                any(
                    "next telemetry log file: events_0002.jsonl" in check
                    for check in report.checks
                )
            )

    def test_live_smoke_until_stopped_uses_event_driven_market_data_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader = FakeTrader()
            runtime_config = build_live_smoke_config(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                symbols=("AAPL",),
                session_dir=Path(tmpdir) / "live_smoke",
                update_interval_ms=1,
                book_depth_levels=2,
            )
            stop_signal = threading.Event()
            stopper = threading.Timer(0.05, stop_signal.set)
            stopper.start()

            runtime = run_live_shift_smoke_until_stopped(
                runtime_config,
                stop_signal,
                execute_orders=False,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )
            stopper.join(timeout=1.0)

            self.assertFalse(runtime.started)
            self.assertGreater(runtime.loop_stats.last_market_data_event_seq, 0)
            self.assertIsNone(runtime.market_data_thread)
            self.assertFalse(trader.connected)

    def test_runtime_emits_expected_telemetry_event_families_after_one_filled_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = build_runtime(
                RuntimeConfig(
                    username="tester",
                    password="secret",
                    initiator_cfg=Path("initiator.cfg"),
                    telemetry=TelemetryConfig(
                        session_dir=Path(tmpdir) / "runs",
                        enable_event_logging=True,
                        logger_flush_every=1,
                    ),
                    market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
                )
            )
            attach_open_session_clock(runtime)
            trader = FakeTrader()

            bootstrap_once(
                runtime,
                trader_factory=lambda username: trader,
                order_book_type=FakeOrderBookType,
            )
            runtime.attach_default_market_maker()

            runtime.control_cycle_once(execute_orders=False)
            assert runtime.trading is not None
            audit = runtime.trading.order_ledger.ensure_audit(
                order_id="telemetry-fill",
                symbol="AAPL",
                side=OrderSide.BID,
                submit_price=100.0,
                submit_size=1,
                liquidity=OrderLiquidity.LIMIT,
            )
            audit.current_status = "filled"
            runtime.trading.order_ledger.append_fill(
                FillRecord(
                    order_id="telemetry-fill",
                    symbol="AAPL",
                    side=OrderSide.BID,
                    executed_size=1,
                    executed_price=100.0,
                    status="filled",
                    event_ts_ns=time.monotonic_ns(),
                    execution_index=0,
                )
            )
            runtime.event_logger.log(
                "order_fill",
                fill=audit.fills[-1],
                slippage=audit.slippage_summary(),
            )
            runtime.run_strategy_once(execute_orders=False)
            runtime.stop()

            event_log = Path(tmpdir) / "runs" / "events.jsonl"
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            kinds = {event["kind"] for event in events}

            self.assertIn("strategy_trace", kinds)
            self.assertIn("strategy_target", kinds)
            self.assertIn("order_fill", kinds)
            self.assertIn("portfolio_snapshot", kinds)
            self.assertIn("session_metrics", kinds)

    def test_runtime_stop_clears_trading_stack_and_strategy_to_avoid_session_carryover(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=1),
            )
        )
        attach_open_session_clock(runtime)

        trader = FakeTrader()
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        runtime.attach_default_market_maker()

        runtime.stop()

        self.assertIsNone(runtime.trading)
        self.assertIsNone(runtime.strategy)
        self.assertIsNone(runtime.book_cache)
        self.assertIsNone(runtime.market_data_loop)
        self.assertIsNone(runtime.market_data_events)


class MarketMakerBlockTest(unittest.TestCase):
    def test_symbol_state_uses_ladder_depth_when_available(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]

        self.assertEqual(state.cumulative_bid_depth(3), 12)
        self.assertEqual(state.cumulative_ask_depth(3), 16)
        self.assertAlmostEqual(state.depth_imbalance(3), -4 / 28, places=8)
        self.assertEqual(state.one_sided_book_volume("bid"), 12)
        self.assertEqual(state.one_sided_book_volume("ask"), 16)
        self.assertEqual(state.levels_to_cover_side_volume_fraction("bid", 0.25), 1)
        self.assertEqual(state.levels_to_cover_side_volume_fraction("ask", 0.25), 1)

        state.global_bids.clear()
        state.global_asks.clear()
        self.assertEqual(state.cumulative_bid_depth(3), state.best_price.best_bid_sz)
        self.assertEqual(state.cumulative_ask_depth(3), state.best_price.best_ask_sz)
        self.assertAlmostEqual(
            state.combined_book_imbalance(3),
            state.book_imbalance,
            places=8,
        )
        self.assertEqual(state.one_sided_book_volume("bid"), state.best_price.best_bid_sz)
        self.assertEqual(state.levels_to_cover_side_volume_fraction("bid", 0.25), 1)

        runtime.stop()

    def test_symbol_state_local_depth_fallback_uses_clean_local_top_not_global_top(self) -> None:
        state = SymbolState(
            symbol="AAPL",
            best_price=BestPriceSnapshot(
                symbol="AAPL",
                best_bid_px=100.00,
                best_bid_sz=10,
                best_ask_px=100.02,
                best_ask_sz=12,
                local_bid_px=100.00,
                local_bid_sz=2,
                local_ask_px=100.02,
                local_ask_sz=4,
            ),
            global_bids=[],
            global_asks=[],
            local_bids=[],
            local_asks=[],
            last_book_update_ns=1,
        )

        self.assertEqual(state.cumulative_bid_depth(3, local=True), 2)
        self.assertEqual(state.cumulative_ask_depth(3, local=True), 4)
        self.assertAlmostEqual(state.depth_imbalance(3, local=True), -2 / 6, places=8)
        self.assertAlmostEqual(
            state.weighted_microprice(3, local=True),
            (100.02 * 2 + 100.00 * 4) / 6,
            places=8,
        )

    def test_symbol_state_microprice_and_imbalance_use_multi_level_book(self) -> None:
        state = SymbolState(
            symbol="AAPL",
            best_price=BestPriceSnapshot(
                symbol="AAPL",
                best_bid_px=100.00,
                best_bid_sz=10,
                best_ask_px=100.02,
                best_ask_sz=10,
            ),
            local_bids=[
                BookLevel(price=100.00, size=10),
                BookLevel(price=99.99, size=10),
                BookLevel(price=99.98, size=10),
            ],
            local_asks=[
                BookLevel(price=100.02, size=1),
                BookLevel(price=100.03, size=1),
                BookLevel(price=100.04, size=1),
            ],
            last_book_update_ns=1,
        )

        self.assertGreater(
            state.weighted_microprice(3, local=True),
            state.best_price.microprice,
        )
        self.assertGreater(state.microprice, state.mid_price)
        self.assertAlmostEqual(
            state.book_imbalance,
            0.6 * state.global_l1_imbalance + 0.4 * state.depth_imbalance(3, local=True),
            places=8,
        )
        self.assertGreater(state.book_imbalance, state.best_price.book_imbalance)

    def test_compute_inventory_skew_is_convex_in_inventory_size(self) -> None:
        one_lot_skew = compute_inventory_skew(
            1,
            tick_size=0.01,
            skew_ticks_per_lot=2.0,
            max_skew_ticks=10.0,
            inventory_limit_lots=5,
        )
        three_lot_skew = compute_inventory_skew(
            3,
            tick_size=0.01,
            skew_ticks_per_lot=2.0,
            max_skew_ticks=10.0,
            inventory_limit_lots=5,
        )

        self.assertGreater(three_lot_skew, 3.0 * one_lot_skew)

    def test_adaptive_parameter_provider_moves_with_book_state(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]

        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)
        narrow = provider.for_symbol("AAPL").values

        state.best_price.best_bid_px = 100.0
        state.best_price.best_ask_px = 100.06
        state.best_price.best_bid_sz = 1
        state.best_price.best_ask_sz = 1
        # Bump the book-update timestamp so observe_symbol_state actually
        # re-observes this book instead of treating it as a duplicate update
        # (it early-returns when last_book_update_ns hasn't advanced), which
        # otherwise leaves the EWMA-smoothed spread/depth signals driving
        # allocation_weight stale at the narrow-book reading.
        state.last_book_update_ns = (state.last_book_update_ns or 0) + 1_000_000_000
        wide = provider.for_symbol("AAPL").values

        self.assertGreaterEqual(wide.spread_floor_ticks, narrow.spread_floor_ticks)
        self.assertGreaterEqual(wide.spread_ceiling_ticks, narrow.spread_ceiling_ticks)
        self.assertLessEqual(wide.allocation_weight, narrow.allocation_weight)
        self.assertLessEqual(wide.passive_fill_probability, narrow.passive_fill_probability)

        runtime.stop()

    def test_adaptive_parameter_provider_emits_cj_glft_model_parameters(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        attach_open_session_clock(runtime)
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            session_clock=runtime.session_clock,
            fallback=SymbolStrategyParameters(
                spread_floor_ticks=1,
                spread_ceiling_ticks=4,
                inventory_skew_cap_ticks=3.0,
                inventory_limit_lots=5,
            ),
        )
        values = provider.for_symbol("AAPL", inventory_lots=2).values

        self.assertGreater(values.cj_inventory_skew_ticks_per_lot, 0.0)
        self.assertGreaterEqual(values.cj_horizon_fraction, 0.0)
        self.assertLessEqual(values.cj_horizon_fraction, 1.0)
        self.assertGreaterEqual(values.glft_half_width_ticks, values.spread_floor_ticks)
        self.assertLessEqual(values.glft_half_width_ticks, values.spread_ceiling_ticks)
        self.assertGreater(values.glft_arrival_intensity, 0.0)
        self.assertGreater(values.glft_depth_sensitivity, 0.0)

        runtime.stop()

    def test_adaptive_parameter_provider_tracks_depth_concentration(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)

        state.local_bids = [
            FakeOrderBookEntry(price=100.00, size=3),
            FakeOrderBookEntry(price=99.99, size=3),
            FakeOrderBookEntry(price=99.98, size=6),
        ]
        state.local_asks = [
            FakeOrderBookEntry(price=100.02, size=4),
            FakeOrderBookEntry(price=100.03, size=4),
            FakeOrderBookEntry(price=100.04, size=8),
        ]
        state.last_book_update_ns = time.monotonic_ns()
        stretched = provider.for_symbol("AAPL").values

        state.local_bids = [
            FakeOrderBookEntry(price=100.00, size=10),
            FakeOrderBookEntry(price=99.99, size=1),
            FakeOrderBookEntry(price=99.98, size=1),
        ]
        state.local_asks = [
            FakeOrderBookEntry(price=100.02, size=13),
            FakeOrderBookEntry(price=100.03, size=2),
            FakeOrderBookEntry(price=100.04, size=1),
        ]
        state.last_book_update_ns = 2
        concentrated = provider.for_symbol("AAPL").values

        self.assertEqual(concentrated.bid_book_volume_lk, 12)
        self.assertEqual(concentrated.ask_book_volume_lk, 16)
        self.assertLessEqual(concentrated.bid_25pct_levels, stretched.bid_25pct_levels)
        self.assertLessEqual(concentrated.ask_25pct_levels, stretched.ask_25pct_levels)
        self.assertGreaterEqual(
            concentrated.depth_concentration_score,
            stretched.depth_concentration_score,
        )

        runtime.stop()

    def test_adaptive_parameter_provider_tracks_front_shape_imbalance(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)

        state.local_bids = [
            FakeOrderBookEntry(price=100.00, size=12),
            FakeOrderBookEntry(price=99.99, size=1),
            FakeOrderBookEntry(price=99.98, size=1),
        ]
        state.local_asks = [
            FakeOrderBookEntry(price=100.02, size=1),
            FakeOrderBookEntry(price=100.03, size=1),
            FakeOrderBookEntry(price=100.04, size=12),
        ]
        state.last_book_update_ns = time.monotonic_ns()

        shaped = provider.for_symbol("AAPL").values

        self.assertEqual(shaped.bid_25pct_levels, 1.0)
        self.assertEqual(shaped.ask_25pct_levels, 3.0)
        self.assertGreater(shaped.front_shape_imbalance, 0.0)
        self.assertGreater(shaped.front_shape_shift_ticks, 0.0)

        runtime.stop()

    def test_adaptive_parameter_provider_uses_recent_history(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)

        state.best_price.best_bid_px = 100.00
        state.best_price.best_ask_px = 100.02
        state.best_price.best_bid_sz = 10
        state.best_price.best_ask_sz = 12
        state.last_book_update_ns = time.monotonic_ns()
        provider.for_symbol("AAPL")

        state.best_price.best_bid_px = 100.20
        state.best_price.best_ask_px = 100.30
        state.best_price.best_bid_sz = 1
        state.best_price.best_ask_sz = 1
        state.last_book_update_ns = 2
        shocked = provider.for_symbol("AAPL").values

        state.best_price.best_bid_px = 100.20
        state.best_price.best_ask_px = 100.22
        state.best_price.best_bid_sz = 10
        state.best_price.best_ask_sz = 12
        state.last_book_update_ns = 3
        cooled = provider.for_symbol("AAPL").values

        self.assertGreater(shocked.sigma_realized, 0.0)
        self.assertGreaterEqual(cooled.sigma_realized, 0.0)
        self.assertLessEqual(shocked.passive_fill_probability, cooled.passive_fill_probability)
        self.assertLessEqual(shocked.quote_age_limit_ms, cooled.quote_age_limit_ms)

        runtime.stop()

    def test_adaptive_parameter_provider_spread_ewma_cools_after_wide_shock(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)

        state.best_price.best_bid_px = 100.00
        state.best_price.best_ask_px = 100.10
        state.last_book_update_ns = 1
        wide = provider.for_symbol("AAPL").values

        state.best_price.best_bid_px = 100.00
        state.best_price.best_ask_px = 100.02
        state.last_book_update_ns = 2
        cooled = provider.for_symbol("AAPL").values

        self.assertGreater(wide.spread_floor_ticks, cooled.spread_floor_ticks)

        runtime.stop()

    def test_adaptive_parameter_provider_applies_regime_break_width_and_size_overlay(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)
        state = runtime.market_state.by_symbol["AAPL"]

        state.last_book_update_ns = 1
        baseline = provider.for_symbol("AAPL").values

        adaptive_state = provider.state_by_symbol["AAPL"]
        adaptive_state.spread_cusum.last_signal = 1
        adaptive_state.global_drift_page_hinkley.last_signal = -1
        state.last_book_update_ns = 2
        shocked = provider.for_symbol("AAPL").values

        self.assertGreaterEqual(
            shocked.bid_width_multiplier,
            baseline.bid_width_multiplier,
        )
        self.assertGreaterEqual(
            shocked.ask_width_multiplier,
            baseline.ask_width_multiplier,
        )
        self.assertLessEqual(
            shocked.bid_size_multiplier,
            baseline.bid_size_multiplier,
        )
        self.assertLessEqual(
            shocked.ask_size_multiplier,
            baseline.ask_size_multiplier,
        )

        runtime.stop()

    def test_adaptive_parameter_provider_uses_single_pass_signed_imbalance_ewma(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.global_bids = []
        state.global_asks = []
        state.local_bids = []
        state.local_asks = []
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            history_config=AdaptiveHistoryConfig(imbalance_alpha=0.25),
        )

        state.best_price.best_bid_px = 100.00
        state.best_price.best_ask_px = 100.02
        state.best_price.best_bid_sz = 9
        state.best_price.best_ask_sz = 3
        state.best_price.local_bid_px = 100.00
        state.best_price.local_ask_px = 100.02
        state.best_price.local_bid_sz = 9
        state.best_price.local_ask_sz = 3
        state.local_multi_level_voi = 0.0
        state.global_l1_voi = 0.0
        state.last_trade_price = 0.0
        state.last_trade_size = 0
        state.last_book_update_ns = 1
        provider.for_symbol("AAPL")

        state.best_price.best_bid_sz = 3
        state.best_price.best_ask_sz = 9
        state.best_price.local_bid_sz = 3
        state.best_price.local_ask_sz = 9
        state.last_book_update_ns = 2
        values = provider.for_symbol("AAPL").values

        self.assertAlmostEqual(values.side_size_tilt, 0.15, places=8)

        runtime.stop()

    def test_adaptive_parameter_provider_uses_rolling_obi_for_soft_one_sided_pressure(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.global_bids = []
        state.global_asks = []
        state.local_bids = []
        state.local_asks = []
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            history_config=AdaptiveHistoryConfig(one_sided_obi_gate_abs=0.70),
        )

        state.best_price.best_bid_px = 100.00
        state.best_price.best_ask_px = 100.02
        state.best_price.local_bid_px = 100.00
        state.best_price.local_ask_px = 100.02
        bid_crowded = None
        for update_ns in range(1, 5):
            state.best_price.local_bid_sz = 10
            state.best_price.local_ask_sz = 1
            state.last_book_update_ns = update_ns
            bid_crowded = provider.for_symbol("AAPL").values
        assert bid_crowded is not None

        ask_crowded = bid_crowded
        for update_ns in range(5, 15):
            state.best_price.local_bid_sz = 1
            state.best_price.local_ask_sz = 10
            state.last_book_update_ns = update_ns
            ask_crowded = provider.for_symbol("AAPL").values

        self.assertTrue(bid_crowded.bid_enable_flag)
        self.assertTrue(bid_crowded.ask_enable_flag)
        self.assertLess(bid_crowded.ask_size_multiplier, bid_crowded.bid_size_multiplier)
        self.assertGreater(bid_crowded.ask_width_multiplier, 1.0)

        self.assertTrue(ask_crowded.bid_enable_flag)
        self.assertTrue(ask_crowded.ask_enable_flag)
        self.assertLess(ask_crowded.bid_size_multiplier, ask_crowded.ask_size_multiplier)
        self.assertGreater(ask_crowded.bid_width_multiplier, 1.0)

        runtime.stop()

    def test_adaptive_parameter_provider_exposes_voi_and_trade_linked_voi_features(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.local_multi_level_voi = 0.80
        state.global_l1_voi = 0.40
        state.last_trade_price = state.best_price.best_ask_px
        state.last_trade_size = 5
        state.last_book_update_ns = 10

        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)
        values = provider.for_symbol("AAPL").values

        self.assertGreater(values.local_multi_level_voi, 0.0)
        self.assertGreater(values.global_l1_voi, 0.0)
        self.assertGreater(values.trade_signed_volume, 0.0)
        self.assertGreater(values.trade_linked_voi_score, 0.0)

        strategy = TopOfBookMarketMaker(runtime.market_state, parameter_provider=provider)
        _, traces = strategy.generate_targets(portfolio_ledger=PortfolioLedger())
        extra = traces[0].diagnostics.extra
        self.assertIn("local_multi_level_voi", extra)
        self.assertIn("global_l1_voi", extra)
        self.assertIn("trade_signed_volume", extra)
        self.assertIn("trade_linked_voi_score", extra)

        runtime.stop()

    def test_adaptive_parameter_provider_exposes_entropy_confidence_overlay(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.best_price.best_bid_px = 100.00
        state.best_price.best_ask_px = 100.02
        state.best_price.local_bid_px = 100.00
        state.best_price.local_ask_px = 100.02
        state.best_price.local_bid_sz = 8
        state.best_price.local_ask_sz = 8
        state.local_bids = [BookLevel(price=100.00, size=16)]
        state.local_asks = [BookLevel(price=100.02, size=16)]
        state.last_trade_price = 100.02
        state.last_trade_size = 4
        state.last_book_update_ns = 1

        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)
        concentrated = provider.for_symbol("AAPL").values

        state.local_bids = [
            BookLevel(price=100.00, size=4),
            BookLevel(price=99.99, size=4),
            BookLevel(price=99.98, size=4),
            BookLevel(price=99.97, size=4),
        ]
        state.local_asks = [
            BookLevel(price=100.02, size=4),
            BookLevel(price=100.03, size=4),
            BookLevel(price=100.04, size=4),
            BookLevel(price=100.05, size=4),
        ]
        state.last_trade_price = 100.00
        state.last_trade_size = 4
        state.last_book_update_ns = 2
        fragmented = provider.for_symbol("AAPL").values

        self.assertGreater(fragmented.depth_entropy, concentrated.depth_entropy)
        self.assertGreater(fragmented.flow_entropy, concentrated.flow_entropy)
        self.assertLess(
            fragmented.entropy_confidence_score,
            concentrated.entropy_confidence_score,
        )

        strategy = TopOfBookMarketMaker(runtime.market_state, parameter_provider=provider)
        _, traces = strategy.generate_targets(portfolio_ledger=PortfolioLedger())
        extra = traces[0].diagnostics.extra
        self.assertIn("depth_entropy", extra)
        self.assertIn("flow_entropy", extra)
        self.assertIn("entropy_confidence_score", extra)

        runtime.stop()

    def test_adaptive_parameter_provider_fast_maker_voi_reacts_faster_than_slow_trade_voi(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.local_bids = [BookLevel(price=100.00, size=1)]
        state.local_asks = [BookLevel(price=100.02, size=1)]
        state.best_price.best_bid_px = 100.00
        state.best_price.best_ask_px = 100.02
        state.best_price.local_bid_px = 100.00
        state.best_price.local_ask_px = 100.02
        state.best_price.local_bid_sz = 1
        state.best_price.local_ask_sz = 1
        state.last_trade_price = 100.02
        state.last_trade_size = 10

        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)

        state.local_multi_level_voi = 1.0
        state.global_l1_voi = 1.0
        state.last_book_update_ns = 1
        provider.for_symbol("AAPL")

        state.local_multi_level_voi = -1.0
        state.global_l1_voi = -1.0
        state.last_book_update_ns = 2
        values = provider.for_symbol("AAPL").values

        self.assertLess(values.fast_maker_voi_pressure, values.slow_trade_voi_pressure)
        self.assertGreater(values.fast_maker_voi_pressure, -1.0)
        self.assertGreater(values.slow_trade_voi_pressure, 0.0)

        runtime.stop()

    def test_adaptive_parameter_provider_uses_execution_ledger_state(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        order_ledger = OrderLedger()
        symbol_ledger = order_ledger.ensure_symbol("AAPL")
        symbol_ledger.set_order(
            OrderSide.BID,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=1,
                submitted_ts_ns=1,
                last_update_ts_ns=1,
                status="new",
            ),
        )
        fill_audit = order_ledger.ensure_audit(
            order_id="bid-1",
            symbol="AAPL",
            side=OrderSide.BID,
            submitted_ts_ns=1,
            submit_price=100.0,
            submit_size=1,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="bid-1",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=1,
                executed_price=100.0,
                status="filled",
                event_ts_ns=2,
                execution_index=0,
            )
        )

        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
            fallback=SymbolStrategyParameters(quote_age_limit_ms=750),
        )
        state = runtime.market_state.by_symbol["AAPL"]
        state.last_book_update_ns = 1
        with_fill = provider.for_symbol("AAPL").values

        fill_audit.cancel_requested_ts_ns = 3
        state.last_book_update_ns = 2
        with_cancel = provider.for_symbol("AAPL").values

        self.assertGreater(with_fill.live_quote_age_ms, 0.0)
        self.assertGreater(with_fill.recent_fill_rate, 0.0)
        self.assertGreater(with_cancel.recent_cancel_rate, 0.0)
        self.assertGreaterEqual(with_cancel.quote_age_limit_ms, with_fill.quote_age_limit_ms)

        runtime.stop()

    def test_adaptive_parameter_provider_does_not_treat_replace_cancels_as_cancel_pressure(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        order_ledger = OrderLedger()
        replace_audit = order_ledger.ensure_audit(
            order_id="replace-1",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_price=100.0,
            submit_size=1,
            liquidity=OrderLiquidity.LIMIT,
        )
        replace_audit.cancel_requested_ts_ns = 2
        replace_audit.replace_requested_ts_ns = 1

        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )
        state = runtime.market_state.by_symbol["AAPL"]
        state.last_book_update_ns = 1

        values = provider.for_symbol("AAPL").values

        self.assertEqual(order_ledger.cancel_count("AAPL"), 0)
        self.assertAlmostEqual(values.recent_cancel_rate, 0.0, places=8)

        runtime.stop()

    def test_adaptive_parameter_provider_uses_last_server_update_for_quote_age(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=1,
                submitted_ts_ns=1,
                last_update_ts_ns=time.monotonic_ns() - 5_000_000,
                status="new",
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )
        state = runtime.market_state.by_symbol["AAPL"]
        state.last_book_update_ns = time.monotonic_ns()

        values = provider.for_symbol("AAPL").values

        self.assertLess(values.live_quote_age_ms, 50.0)

        runtime.stop()

    def test_adaptive_parameter_provider_queue_share_changes_passive_fill_probability(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=1,
                status="new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )

        state.local_bids = [BookLevel(price=100.0, size=20)]
        state.best_price.local_bid_px = 100.0
        state.best_price.local_bid_sz = 20
        state.last_book_update_ns = 1
        deep_queue = provider.for_symbol("AAPL").values

        state.local_bids = [BookLevel(price=100.0, size=1)]
        state.best_price.local_bid_px = 100.0
        state.best_price.local_bid_sz = 1
        state.last_book_update_ns = 2
        front_queue = provider.for_symbol("AAPL").values

        self.assertLess(deep_queue.queue_fill_support, front_queue.queue_fill_support)
        self.assertGreater(deep_queue.bid_queue_ahead_lots, front_queue.bid_queue_ahead_lots)
        self.assertLess(deep_queue.bid_queue_share, front_queue.bid_queue_share)
        self.assertLess(
            deep_queue.passive_fill_probability,
            front_queue.passive_fill_probability,
        )

        runtime.stop()

    def test_adaptive_parameter_provider_exposes_online_spread_and_quote_age_summaries(self) -> None:
        trader = FakeTrader(bid_price_offsets=(0.00, 0.02, 0.04, 0.01, 0.03))
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=1,
                status="new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )

        for update_ns in range(1, 8):
            runtime.poll_once()
            runtime.market_state.by_symbol["AAPL"].last_book_update_ns = update_ns
            values = provider.for_symbol("AAPL").values

        self.assertGreater(values.spread_mean_ticks, 0.0)
        self.assertGreaterEqual(values.spread_p90_ticks, values.spread_mean_ticks)
        self.assertGreater(values.quote_age_mean_ms, 0.0)
        self.assertGreater(values.quote_age_p90_ms, 0.0)
        self.assertGreaterEqual(
            values.quote_age_p90_ms,
            0.99 * values.quote_age_mean_ms,
        )
        runtime.stop()

    def test_adaptive_parameter_provider_queue_support_uses_other_depth_plus_own_depth(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.local_bids = [BookLevel(price=100.0, size=3)]
        state.best_price.local_bid_px = 100.0
        state.best_price.local_bid_sz = 3
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                status="new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )

        (
            support,
            queue_latency_haircut,
            bid_ahead,
            ask_ahead,
            bid_share,
            ask_share,
        ) = provider._queue_fill_support(state)

        self.assertAlmostEqual(support, 0.4, places=8)
        self.assertEqual(queue_latency_haircut, 1.0)
        self.assertEqual(bid_ahead, 3)
        self.assertEqual(ask_ahead, 0)
        self.assertAlmostEqual(bid_share, 0.4, places=8)
        self.assertAlmostEqual(ask_share, 0.0, places=8)

        runtime.stop()

    def test_adaptive_parameter_provider_queue_support_is_full_when_cleaned_local_depth_is_zero(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.local_bids = []
        state.best_price.local_bid_sz = 0
        state.best_price.best_bid_sz = 99
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                status="new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )

        (
            support,
            queue_latency_haircut,
            bid_ahead,
            ask_ahead,
            bid_share,
            ask_share,
        ) = provider._queue_fill_support(state)

        self.assertAlmostEqual(support, 1.0, places=8)
        self.assertEqual(queue_latency_haircut, 1.0)
        self.assertEqual(bid_ahead, 0)
        self.assertEqual(ask_ahead, 0)
        self.assertAlmostEqual(bid_share, 1.0, places=8)
        self.assertAlmostEqual(ask_share, 0.0, places=8)

        runtime.stop()

    def test_adaptive_parameter_provider_queue_support_is_zero_when_order_is_behind_local_touch(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.local_bids = [
            BookLevel(price=100.0, size=4),
            BookLevel(price=99.99, size=8),
        ]
        state.best_price.local_bid_px = 100.0
        state.best_price.local_bid_sz = 4
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="stale-bid",
                price=99.99,
                size=2,
                status="new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )

        (
            support,
            queue_latency_haircut,
            bid_ahead,
            ask_ahead,
            bid_share,
            ask_share,
        ) = provider._queue_fill_support(state)

        self.assertAlmostEqual(support, 1.0 / 7.0, places=8)
        self.assertEqual(queue_latency_haircut, 1.0)
        self.assertEqual(bid_ahead, 12)
        self.assertEqual(ask_ahead, 0)
        self.assertAlmostEqual(bid_share, 1.0 / 7.0, places=8)
        self.assertAlmostEqual(ask_share, 0.0, places=8)
        runtime.stop()

    def test_adaptive_parameter_provider_queue_support_defaults_to_zero_without_live_orders(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=OrderLedger(),
        )
        state = runtime.market_state.by_symbol["AAPL"]

        (
            support,
            queue_latency_haircut,
            bid_ahead,
            ask_ahead,
            bid_share,
            ask_share,
        ) = provider._queue_fill_support(state)

        self.assertEqual(support, 0.0)
        self.assertEqual(queue_latency_haircut, 1.0)
        self.assertEqual(bid_ahead, 0)
        self.assertEqual(ask_ahead, 0)
        self.assertEqual(bid_share, 0.0)
        self.assertEqual(ask_share, 0.0)

        runtime.stop()

    def test_adaptive_parameter_provider_queue_support_haircuts_stale_queue_state(
        self,
    ) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.last_book_update_ns = 1_000_000_000
        state.local_bids = [BookLevel(price=100.0, size=3)]
        state.best_price.local_bid_px = 100.0
        state.best_price.local_bid_sz = 3
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                status="new",
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
            history_config=AdaptiveHistoryConfig(
                queue_latency_half_life_ms=100.0,
            ),
        )

        (
            stale_support,
            stale_queue_latency_haircut,
            bid_ahead,
            ask_ahead,
            stale_bid_share,
            stale_ask_share,
        ) = provider._queue_fill_support(state, now_ns=1_300_000_000)

        self.assertLess(stale_queue_latency_haircut, 0.2)
        self.assertAlmostEqual(stale_queue_latency_haircut, 0.125, places=8)
        self.assertAlmostEqual(stale_support, 0.05, places=8)
        self.assertEqual(bid_ahead, 3)
        self.assertEqual(ask_ahead, 0)
        self.assertAlmostEqual(stale_bid_share, 0.05, places=8)
        self.assertAlmostEqual(stale_ask_share, 0.0, places=8)

        runtime.stop()

    def test_adaptive_parameter_provider_tracks_post_fill_adverse_selection_toxicity(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="toxic-bid-fill",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_price=100.0,
            submit_size=1,
            liquidity=OrderLiquidity.LIMIT,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="toxic-bid-fill",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=1,
                executed_price=100.0,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
            history_config=AdaptiveHistoryConfig(toxicity_markout_delay_ns=0),
        )
        state = runtime.market_state.by_symbol["AAPL"]
        state.best_price.best_bid_px = 99.94
        state.best_price.best_ask_px = 99.96
        state.last_book_update_ns = 2

        toxic = provider.for_symbol("AAPL").values

        self.assertGreater(toxic.toxicity_score, 0.0)
        self.assertGreater(toxic.toxicity_markout_pct, 0.0)
        self.assertGreater(toxic.toxicity_width_multiplier, 1.0)
        self.assertLess(toxic.toxicity_size_multiplier, 1.0)
        self.assertGreaterEqual(toxic.bid_width_multiplier, toxic.toxicity_width_multiplier * 0.5)

        runtime.stop()

    def test_adaptive_parameter_provider_waits_for_default_toxicity_markout_delay(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="delayed-toxic-bid-fill",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_price=100.0,
            submit_size=1,
            liquidity=OrderLiquidity.LIMIT,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="delayed-toxic-bid-fill",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=1,
                executed_price=100.0,
                status="filled",
                event_ts_ns=time.monotonic_ns(),
                execution_index=0,
            )
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )
        state = runtime.market_state.by_symbol["AAPL"]
        state.best_price.best_bid_px = 99.94
        state.best_price.best_ask_px = 99.96
        state.last_book_update_ns = time.monotonic_ns()

        values = provider.for_symbol("AAPL").values

        self.assertEqual(values.toxicity_score, 0.0)
        self.assertEqual(values.toxicity_markout_pct, 0.0)

        runtime.stop()

    def test_cj_glft_sigma_is_converted_from_return_space_to_tick_space(self) -> None:
        params = estimate_cj_glft_parameters(
            smoothed_spread_ticks=2.0,
            sigma_realized=0.0002,
            mid_price=100.0,
            tick_size=0.01,
            gamma_inventory=1.0,
            session_minutes_to_close=195.0,
            session_length_minutes=390.0,
            passive_fill_probability=0.5,
            queue_fill_support=0.5,
            liquidity_score=1.0,
            maker_net_edge_ticks=0.75,
            min_half_width_ticks=1,
            max_half_width_ticks=20,
            inventory_skew_cap_ticks=20.0,
        )

        self.assertGreater(params.cj_inventory_skew_ticks_per_lot, 1.0)
        self.assertGreater(params.glft_half_width_ticks, 1.0)

    def test_cj_glft_discounts_lambda_when_expected_maker_edge_is_negative(
        self,
    ) -> None:
        good_edge = estimate_cj_glft_parameters(
            smoothed_spread_ticks=2.0,
            sigma_realized=0.0002,
            mid_price=100.0,
            tick_size=0.01,
            gamma_inventory=1.0,
            session_minutes_to_close=195.0,
            session_length_minutes=390.0,
            passive_fill_probability=0.6,
            queue_fill_support=0.7,
            liquidity_score=1.0,
            maker_net_edge_ticks=1.0,
            min_half_width_ticks=1,
            max_half_width_ticks=20,
            inventory_skew_cap_ticks=20.0,
        )
        bad_edge = estimate_cj_glft_parameters(
            smoothed_spread_ticks=2.0,
            sigma_realized=0.0002,
            mid_price=100.0,
            tick_size=0.01,
            gamma_inventory=1.0,
            session_minutes_to_close=195.0,
            session_length_minutes=390.0,
            passive_fill_probability=0.6,
            queue_fill_support=0.7,
            liquidity_score=1.0,
            maker_net_edge_ticks=-1.0,
            min_half_width_ticks=1,
            max_half_width_ticks=20,
            inventory_skew_cap_ticks=20.0,
        )

        self.assertLess(
            bad_edge.glft_arrival_intensity,
            good_edge.glft_arrival_intensity,
        )
        self.assertLess(
            bad_edge.maker_edge_intensity_factor,
            good_edge.maker_edge_intensity_factor,
        )
        self.assertGreaterEqual(
            bad_edge.glft_half_width_ticks,
            good_edge.glft_half_width_ticks,
        )

    def test_adaptive_parameter_provider_exposes_symbol_fee_and_passive_ratio(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="limit-1",
            symbol="AAPL",
            side=OrderSide.BID,
            liquidity=OrderLiquidity.LIMIT,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="limit-1",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=100,
                executed_price=100.0,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )
        order_ledger.ensure_audit(
            order_id="market-1",
            symbol="AAPL",
            side=OrderSide.ASK,
            liquidity=OrderLiquidity.MARKET,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="market-1",
                symbol="AAPL",
                side=OrderSide.ASK,
                executed_size=100,
                executed_price=100.1,
                status="filled",
                event_ts_ns=2,
                execution_index=0,
            )
        )

        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )
        values = provider.for_symbol("AAPL").values

        self.assertAlmostEqual(values.estimated_rebate, 0.20, places=8)
        self.assertAlmostEqual(values.estimated_fee, 0.30, places=8)
        self.assertAlmostEqual(values.estimated_net_fee, 0.10, places=8)
        self.assertAlmostEqual(values.passive_fill_ratio, 0.5, places=8)

        runtime.stop()

    def test_adaptive_parameter_provider_rewards_passive_fee_economics(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        maker_ledger = OrderLedger()
        taker_ledger = OrderLedger()
        for idx in range(4):
            maker_id = f"limit-{idx}"
            maker_ledger.ensure_audit(
                order_id=maker_id,
                symbol="AAPL",
                side=OrderSide.BID,
                liquidity=OrderLiquidity.LIMIT,
            )
            maker_ledger.append_fill(
                FillRecord(
                    order_id=maker_id,
                    symbol="AAPL",
                    side=OrderSide.BID,
                    executed_size=100,
                    executed_price=100.0,
                    status="filled",
                    event_ts_ns=idx + 1,
                    execution_index=0,
                )
            )

            taker_id = f"market-{idx}"
            taker_ledger.ensure_audit(
                order_id=taker_id,
                symbol="AAPL",
                side=OrderSide.ASK,
                liquidity=OrderLiquidity.MARKET,
            )
            taker_ledger.append_fill(
                FillRecord(
                    order_id=taker_id,
                    symbol="AAPL",
                    side=OrderSide.ASK,
                    executed_size=100,
                    executed_price=100.0,
                    status="filled",
                    event_ts_ns=idx + 1,
                    execution_index=0,
                )
            )

        maker_values = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=maker_ledger,
        ).for_symbol("AAPL").values
        taker_values = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=taker_ledger,
        ).for_symbol("AAPL").values

        self.assertGreater(maker_values.execution_cost_score, taker_values.execution_cost_score)
        self.assertGreaterEqual(maker_values.allocation_weight, taker_values.allocation_weight)
        self.assertGreaterEqual(maker_values.pace_multiplier, taker_values.pace_multiplier)
        self.assertLess(maker_values.avg_net_fee_per_fill, taker_values.avg_net_fee_per_fill)

        runtime.stop()

    def test_adaptive_parameter_provider_avg_net_fee_uses_symbol_fill_count(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL", "XOM")),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="aapl-market",
            symbol="AAPL",
            side=OrderSide.ASK,
            liquidity=OrderLiquidity.MARKET,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="aapl-market",
                symbol="AAPL",
                side=OrderSide.ASK,
                executed_size=100,
                executed_price=100.0,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )
        for idx in range(4):
            order_ledger.ensure_audit(
                order_id=f"xom-limit-{idx}",
                symbol="XOM",
                side=OrderSide.BID,
                liquidity=OrderLiquidity.LIMIT,
            )
            order_ledger.append_fill(
                FillRecord(
                    order_id=f"xom-limit-{idx}",
                    symbol="XOM",
                    side=OrderSide.BID,
                    executed_size=100,
                    executed_price=50.0,
                    status="filled",
                    event_ts_ns=idx + 2,
                    execution_index=0,
                )
            )

        values = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        ).for_symbol("AAPL").values

        self.assertAlmostEqual(values.avg_net_fee_per_fill, 0.30, places=8)
        runtime.stop()

    def test_adaptive_parameter_provider_scales_inventory_pressure_and_close_penalty(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            fallback=SymbolStrategyParameters(inventory_limit_lots=5),
        )

        flat_values = provider.for_symbol("AAPL", inventory_lots=0).values
        inventory_values = provider.for_symbol("AAPL", inventory_lots=4).values

        self.assertAlmostEqual(flat_values.inventory_close_penalty, 0.0, places=8)
        self.assertAlmostEqual(flat_values.inventory_pressure_score, 0.0, places=8)
        self.assertGreater(inventory_values.inventory_close_penalty, 0.0)
        self.assertAlmostEqual(inventory_values.inventory_pressure_score, 0.8, places=8)
        self.assertGreaterEqual(inventory_values.gamma_inventory, flat_values.gamma_inventory)
        self.assertGreaterEqual(
            inventory_values.cj_inventory_skew_ticks_per_lot,
            flat_values.cj_inventory_skew_ticks_per_lot,
        )

        runtime.stop()

    def test_adaptive_parameter_provider_uses_position_basis_for_close_penalty(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=120.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            portfolio_ledger=portfolio_ledger,
            fallback=SymbolStrategyParameters(inventory_limit_lots=5),
        )

        basis_values = provider.for_symbol("AAPL", inventory_lots=2).values
        self.assertAlmostEqual(basis_values.inventory_close_penalty, 240.0, places=8)

        portfolio_ledger.update_position(
            "AAPL",
            long_shares=0,
            short_shares=300,
            long_price=0.0,
            short_price=80.0,
            realized_pl=0.0,
            ts_ns=2,
        )
        short_values = provider.for_symbol("AAPL", inventory_lots=-3).values
        self.assertAlmostEqual(short_values.inventory_close_penalty, 240.0, places=8)

        runtime.stop()

    def test_adaptive_parameter_provider_pnl_allocation_score_uses_capital_per_lot_and_fill_count(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        order_ledger = OrderLedger()
        order_ledger.append_fill(
            FillRecord(
                order_id="fill-1",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=1,
                executed_price=100.0,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )

        profitable_portfolio = PortfolioLedger()
        profitable_portfolio.update_position(
            "AAPL",
            long_shares=0,
            short_shares=0,
            long_price=0.0,
            short_price=0.0,
            realized_pl=100.0,
            ts_ns=1,
        )
        losing_portfolio = PortfolioLedger()
        losing_portfolio.update_position(
            "AAPL",
            long_shares=0,
            short_shares=0,
            long_price=0.0,
            short_price=0.0,
            realized_pl=-100.0,
            ts_ns=1,
        )

        profitable_values = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
            portfolio_ledger=profitable_portfolio,
        ).for_symbol("AAPL").values
        losing_values = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
            portfolio_ledger=losing_portfolio,
        ).for_symbol("AAPL").values

        self.assertGreater(profitable_values.pnl_allocation_score, 0.5)
        self.assertLess(losing_values.pnl_allocation_score, 0.5)
        self.assertGreater(
            profitable_values.allocation_weight,
            losing_values.allocation_weight,
        )

        runtime.stop()

    def test_adaptive_parameter_provider_fades_drift_bias_when_inventory_pressure_is_high(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            fallback=SymbolStrategyParameters(inventory_limit_lots=5),
        )
        provider.for_symbol("AAPL", inventory_lots=0)
        state = provider.state_by_symbol["AAPL"]
        state.initialized = True
        state.ewma_global_mid_drift = 0.02

        flat_values = provider.for_symbol("AAPL", inventory_lots=0).values
        loaded_values = provider.for_symbol("AAPL", inventory_lots=5).values

        self.assertGreater(abs(flat_values.drift_inventory_bias_ticks), 0.0)
        self.assertLess(
            abs(loaded_values.drift_inventory_bias_ticks),
            abs(flat_values.drift_inventory_bias_ticks),
        )

        runtime.stop()

    def test_order_ledger_and_risk_limits_account_for_reserved_buying_power(self) -> None:
        order_ledger = OrderLedger()
        symbol_ledger = order_ledger.ensure_symbol("AAPL")
        symbol_ledger.set_order(
            OrderSide.BID,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=3,
                liquidity=OrderLiquidity.LIMIT,
                submitted_ts_ns=1,
                last_update_ts_ns=1,
                status="pending_new",
            ),
        )
        self.assertAlmostEqual(order_ledger.estimated_reserved_buying_power, 30_000.0, places=8)

        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 50_000.0
        limits = RiskLimits(
            RiskLimitsConfig(min_buying_power=25_000.0),
            portfolio_ledger=portfolio_ledger,
            order_ledger=order_ledger,
        )

        rejected = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=1,
            )
        )
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.first_reject_reason(), "buying_power_floor")

        symbol_ledger.bid_order.pending_cancel = True
        still_rejected = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=1,
            )
        )
        self.assertFalse(still_rejected.allowed)
        self.assertEqual(still_rejected.first_reject_reason(), "buying_power_floor")

        symbol_ledger.bid_order.status = "inactive"
        released = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=1,
            )
        )
        self.assertTrue(released.allowed)

    def test_adaptive_parameter_provider_exposes_reserved_buying_power(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        order_ledger = OrderLedger()
        order_ledger.ensure_symbol("AAPL").set_order(
            OrderSide.ASK,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.ASK,
                order_id="ask-1",
                price=101.0,
                size=2,
                liquidity=OrderLiquidity.LIMIT,
                submitted_ts_ns=1,
                last_update_ts_ns=1,
                status="pending_new",
            ),
        )

        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
        )
        values = provider.for_symbol("AAPL").values

        self.assertAlmostEqual(
            values.estimated_reserved_buying_power,
            20_200.0,
            places=8,
        )

        runtime.stop()

    def test_risk_limits_do_not_double_count_broker_reported_short_close_buying_power(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 50_000.0
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=0,
            short_shares=200,
            long_price=0.0,
            short_price=100.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        order_ledger = OrderLedger()
        limits = RiskLimits(
            RiskLimitsConfig(min_buying_power=25_000.0),
            portfolio_ledger=portfolio_ledger,
            order_ledger=order_ledger,
        )

        self.assertAlmostEqual(
            portfolio_ledger.estimated_short_close_buying_power,
            20_000.0,
            places=8,
        )

        allowed = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=1,
            )
        )
        self.assertTrue(allowed.allowed)

    def test_risk_limits_reserve_future_short_cover_for_new_and_existing_asks(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 30_000.0
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.ASK,
                order_id="ask-1",
                price=100.0,
                size=1,
                liquidity=OrderLiquidity.LIMIT,
                status="pending_new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        limits = RiskLimits(
            RiskLimitsConfig(min_buying_power=5_000.0),
            portfolio_ledger=portfolio_ledger,
            order_ledger=order_ledger,
        )

        rejected = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.ASK,
                price=100.0,
                size=1,
            )
        )

        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.first_reject_reason(), "buying_power_floor")

    def test_risk_limits_do_not_double_short_cover_when_ask_reduces_long_inventory(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 40_000.0
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        limits = RiskLimits(
            RiskLimitsConfig(min_buying_power=15_000.0),
            portfolio_ledger=portfolio_ledger,
            order_ledger=OrderLedger(),
        )

        allowed = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.ASK,
                price=100.0,
                size=2,
            )
        )

        self.assertTrue(allowed.allowed)

    def test_risk_limits_reject_oversized_short_cover_flatten_when_bp_is_insufficient(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 10_000.0
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=0,
            short_shares=200,
            long_price=0.0,
            short_price=100.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        limits = RiskLimits(
            RiskLimitsConfig(min_buying_power=0.0),
            portfolio_ledger=portfolio_ledger,
            order_ledger=OrderLedger(),
        )

        rejected = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.FLATTEN,
                symbol="AAPL",
                side=OrderSide.BID,
                size=2,
                decision_price=100.0,
            )
        )
        chunk_allowed = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.FLATTEN,
                symbol="AAPL",
                side=OrderSide.BID,
                size=1,
                decision_price=100.0,
            )
        )

        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.first_reject_reason(), "buying_power_floor")
        self.assertTrue(chunk_allowed.allowed)

    def test_risk_limits_allow_sell_flatten_without_buying_power_charge(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 0.0
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        limits = RiskLimits(
            RiskLimitsConfig(min_buying_power=0.0),
            portfolio_ledger=portfolio_ledger,
            order_ledger=OrderLedger(),
        )

        allowed = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.FLATTEN,
                symbol="AAPL",
                side=OrderSide.ASK,
                size=2,
                decision_price=100.0,
            )
        )

        self.assertTrue(allowed.allowed)

    def test_risk_limits_use_projected_gross_position_after_new_order(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 1_000_000.0
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=900,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        limits = RiskLimits(
            RiskLimitsConfig(max_position_lots_per_symbol=20, max_gross_position_lots=10),
            portfolio_ledger=portfolio_ledger,
            order_ledger=OrderLedger(),
        )

        rejected = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="MSFT",
                side=OrderSide.BID,
                price=100.0,
                size=2,
            )
        )
        reduced = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.ASK,
                price=100.0,
                size=2,
            )
        )

        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.first_reject_reason(), "gross_position_limit")
        self.assertTrue(reduced.allowed)

    def test_risk_limits_treat_replace_as_cancel_first_not_immediate_new_exposure(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 1_000_000.0
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=500,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        order_ledger = OrderLedger()
        order_ledger.ensure_symbol("AAPL").set_order(
            OrderSide.BID,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=99.9,
                size=2,
                status="new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            ),
        )
        limits = RiskLimits(
            RiskLimitsConfig(max_position_lots_per_symbol=5),
            portfolio_ledger=portfolio_ledger,
            order_ledger=order_ledger,
        )

        replace_eval = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.REPLACE,
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
            )
        )
        submit_eval = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=2,
            )
        )

        self.assertTrue(replace_eval.allowed)
        self.assertFalse(submit_eval.allowed)
        self.assertEqual(submit_eval.first_reject_reason(), "symbol_position_limit")

    def test_default_market_maker_logs_short_close_buying_power(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        attach_open_session_clock(runtime)
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.trading is not None
        runtime.trading.portfolio_ledger.update_position(
            "AAPL",
            long_shares=0,
            short_shares=300,
            long_price=0.0,
            short_price=101.0,
            realized_pl=0.0,
            ts_ns=time.monotonic_ns(),
        )
        runtime.attach_default_market_maker()

        _, traces = runtime.run_strategy_once(execute_orders=False)

        self.assertAlmostEqual(
            traces[0].diagnostics.extra["estimated_short_close_buying_power"],
            30_300.0,
            places=8,
        )

        runtime.stop()

    def test_adaptive_parameter_provider_throttles_allocation_when_bp_is_committed(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        low_usage_portfolio = PortfolioLedger()
        low_usage_portfolio.summary.total_bp = 100_000.0
        low_usage_provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=OrderLedger(),
            portfolio_ledger=low_usage_portfolio,
        )
        low_usage = low_usage_provider.for_symbol("AAPL").values

        high_usage_portfolio = PortfolioLedger()
        high_usage_portfolio.summary.total_bp = 20_000.0
        high_usage_portfolio.update_position(
            "AAPL",
            long_shares=0,
            short_shares=500,
            long_price=0.0,
            short_price=100.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        high_usage_ledger = OrderLedger()
        high_usage_ledger.ensure_symbol("AAPL").set_order(
            OrderSide.BID,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=3,
                liquidity=OrderLiquidity.LIMIT,
                submitted_ts_ns=1,
                last_update_ts_ns=1,
                status="pending_new",
            ),
        )
        high_usage_provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=high_usage_ledger,
            portfolio_ledger=high_usage_portfolio,
        )
        high_usage = high_usage_provider.for_symbol("AAPL", inventory_lots=-5).values

        self.assertGreater(high_usage.buying_power_usage_ratio, low_usage.buying_power_usage_ratio)
        self.assertLess(high_usage.buying_power_scale, low_usage.buying_power_scale)
        self.assertLess(high_usage.allocation_weight, low_usage.allocation_weight)
        self.assertLess(
            high_usage.estimated_available_buying_power,
            low_usage.estimated_available_buying_power,
        )

        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            order_ledger=high_usage_ledger,
        )
        assert runtime.trading is not None
        runtime.trading.portfolio_ledger.summary.total_bp = 100_000.0
        runtime.trading.portfolio_ledger.update_position(
            "AAPL",
            long_shares=0,
            short_shares=500,
            long_price=0.0,
            short_price=100.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        _, traces = strategy.generate_targets(
            portfolio_ledger=runtime.trading.portfolio_ledger,
        )

        self.assertIn("buying_power_scale", traces[0].diagnostics.extra)
        self.assertIn("estimated_available_buying_power", traces[0].diagnostics.extra)

        runtime.stop()

    def test_market_maker_disables_quotes_on_stale_book(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.last_book_update_ns = 0

        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            TopOfBookMarketMakerConfig(
                default_parameters=SymbolStrategyParameters(quote_age_limit_ms=1)
            ),
        )
        targets, traces = strategy.generate_targets(portfolio_ledger=PortfolioLedger())

        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0].enable_bid)
        self.assertFalse(targets[0].enable_ask)
        self.assertEqual(traces[0].decision_reason, "stale_book")

        runtime.stop()

    def test_market_maker_goes_one_sided_at_inventory_cap(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        portfolio = PortfolioLedger()
        portfolio.update_position(
            "AAPL",
            long_shares=500,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            TopOfBookMarketMakerConfig(
                default_parameters=SymbolStrategyParameters(inventory_limit_lots=5)
            ),
        )
        targets, traces = strategy.generate_targets(portfolio_ledger=portfolio)

        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0].enable_bid)
        self.assertTrue(targets[0].enable_ask)
        self.assertEqual(traces[0].diagnostics.extra["inventory_lots"], 5)

        runtime.stop()

    def test_market_maker_honors_symbol_disable_and_allocation_weight(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        disabled_provider = StaticParameterProvider(
            overrides={
                "AAPL": SymbolStrategyParameters(
                    symbol_enable_flag=False,
                    quote_size_lots=2,
                    allocation_weight=1.5,
                    pace_multiplier=1.0,
                )
            }
        )
        disabled_strategy = TopOfBookMarketMaker(
            runtime.market_state,
            parameter_provider=disabled_provider,
        )
        disabled_targets, disabled_traces = disabled_strategy.generate_targets(
            portfolio_ledger=PortfolioLedger()
        )

        self.assertEqual(len(disabled_targets), 1)
        self.assertFalse(disabled_targets[0].enable_bid)
        self.assertFalse(disabled_targets[0].enable_ask)
        self.assertEqual(disabled_targets[0].reason, "symbol_disabled")
        self.assertEqual(disabled_targets[0].bid_size, 0)
        self.assertEqual(disabled_targets[0].ask_size, 0)
        self.assertEqual(disabled_traces[0].diagnostics.allocation_weight, 1.5)

        weighted_provider = StaticParameterProvider(
            overrides={
                "AAPL": SymbolStrategyParameters(
                    quote_size_lots=2,
                    allocation_weight=1.5,
                    pace_multiplier=2.0,
                )
            }
        )
        weighted_strategy = TopOfBookMarketMaker(
            runtime.market_state,
            parameter_provider=weighted_provider,
        )
        weighted_targets, weighted_traces = weighted_strategy.generate_targets(
            portfolio_ledger=PortfolioLedger()
        )

        self.assertEqual(weighted_targets[0].bid_size, 6)
        self.assertEqual(weighted_targets[0].ask_size, 6)
        self.assertTrue(weighted_targets[0].enable_bid)
        self.assertTrue(weighted_targets[0].enable_ask)
        self.assertEqual(weighted_traces[0].diagnostics.pace_multiplier, 2.0)
        self.assertEqual(
            weighted_traces[0].diagnostics.extra["resolved_bid_size"],
            6,
        )
        self.assertEqual(
            weighted_traces[0].diagnostics.extra["resolved_ask_size"],
            6,
        )

        runtime.stop()

    def test_market_maker_leans_size_away_from_crowded_bid_and_tightens_sparse_ask(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.local_bids = [
            FakeOrderBookEntry(price=100.00, size=20),
            FakeOrderBookEntry(price=99.99, size=20),
            FakeOrderBookEntry(price=99.98, size=10),
        ]
        state.local_asks = [
            FakeOrderBookEntry(price=100.02, size=1),
            FakeOrderBookEntry(price=100.03, size=1),
            FakeOrderBookEntry(price=100.04, size=1),
        ]
        now_ns = time.monotonic_ns()
        state.last_book_update_ns = now_ns
        state.best_price.update_ts_ns = now_ns

        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            TopOfBookMarketMakerConfig(
                default_parameters=SymbolStrategyParameters(quote_size_lots=3)
            ),
        )
        targets, traces = strategy.generate_targets(portfolio_ledger=PortfolioLedger())

        self.assertEqual(len(targets), 1)
        self.assertLess(targets[0].bid_size, targets[0].ask_size)
        self.assertLess(
            traces[0].diagnostics.extra["bid_size_multiplier"],
            traces[0].diagnostics.extra["ask_size_multiplier"],
        )
        self.assertGreater(traces[0].diagnostics.extra["side_size_tilt"], 0.0)
        self.assertGreater(
            traces[0].diagnostics.extra["bid_width_multiplier"],
            traces[0].diagnostics.extra["ask_width_multiplier"],
        )
        self.assertGreater(traces[0].diagnostics.extra["side_width_tilt"], 0.0)

        runtime.stop()

    def test_market_maker_keeps_weak_ask_side_enabled_but_scales_its_size_down(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.local_bids = [
            FakeOrderBookEntry(price=100.00, size=10),
            FakeOrderBookEntry(price=99.99, size=10),
            FakeOrderBookEntry(price=99.98, size=10),
        ]
        state.local_asks = [
            FakeOrderBookEntry(price=100.02, size=1),
            FakeOrderBookEntry(price=100.03, size=0),
            FakeOrderBookEntry(price=100.04, size=0),
        ]
        state.last_book_update_ns = time.monotonic_ns()

        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            TopOfBookMarketMakerConfig(
                default_parameters=SymbolStrategyParameters(quote_size_lots=2)
            ),
        )
        targets, traces = strategy.generate_targets(portfolio_ledger=PortfolioLedger())

        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].enable_bid)
        self.assertTrue(targets[0].enable_ask)
        self.assertGreaterEqual(targets[0].ask_size, 1)
        self.assertLessEqual(
            traces[0].diagnostics.extra["ask_size_multiplier"],
            traces[0].diagnostics.extra["bid_size_multiplier"],
        )
        self.assertTrue(traces[0].diagnostics.extra["bid_enable_flag"])
        self.assertTrue(traces[0].diagnostics.extra["ask_enable_flag"])

        runtime.stop()

    def test_mm_quote_plan_can_shift_size_outward_when_front_edge_is_weak(self) -> None:
        state = SymbolState(
            symbol="AAPL",
            best_price=BestPriceSnapshot(
                symbol="AAPL",
                best_bid_px=100.0,
                best_bid_sz=10,
                best_ask_px=100.02,
                best_ask_sz=10,
                local_bid_px=100.0,
                local_bid_sz=10,
                local_ask_px=100.02,
                local_ask_sz=10,
            ),
            local_bids=[BookLevel(price=100.0, size=10)],
            local_asks=[BookLevel(price=100.02, size=10)],
        )
        params = SymbolStrategyParameters(
            quote_size_lots=2,
            passive_ladder_depth_levels=2,
            side_size_tilt=1.0,
            bid_queue_share=0.0,
            ask_queue_share=1.0,
            entropy_confidence_score=0.0,
            microprice_weight=0.0,
            imbalance_shift_ticks=0.0,
            cj_inventory_skew_ticks_per_lot=0.0,
            glft_half_width_ticks=1.0,
        )

        plan = build_quote_plan(
            symbol_state=state,
            params=params,
            inventory_lots=0,
            gate=QuoteGateDecision(
                allow_quotes=True,
                reason="quotable",
                age_ms=0.0,
                spread_ticks=2.0,
            ),
            tick_size=0.01,
        )
        target = plan.to_quote_target(
            symbol="AAPL",
            params=params,
            tick_size=0.01,
        )

        self.assertEqual(len(target.bid_levels), 2)
        self.assertEqual(len(target.ask_levels), 2)
        self.assertGreater(
            target.bid_levels[1].size,
            target.bid_levels[0].size,
        )
        self.assertLessEqual(
            target.ask_levels[1].size,
            target.ask_levels[0].size,
        )

    def test_online_lead_lag_graph_learns_asymmetric_auto_leader_edge_with_confidence(
        self,
    ) -> None:
        graph = OnlineLeadLagGraph(
            OnlineLeadLagGraphConfig(
                alpha=0.35,
                min_samples=6,
                min_abs_score=0.05,
                min_asymmetry=0.01,
                max_shift_ticks=0.75,
            )
        )
        market_state = MarketState(
            by_symbol={
                "AAPL": SymbolState(
                    symbol="AAPL",
                    best_price=BestPriceSnapshot(symbol="AAPL"),
                ),
                "MSFT": SymbolState(
                    symbol="MSFT",
                    best_price=BestPriceSnapshot(symbol="MSFT"),
                ),
            }
        )
        leader_moves = [
            1,
            -1,
            1,
            1,
            -1,
            1,
            -1,
            -1,
            1,
            -1,
            1,
            -1,
            -1,
            1,
            1,
            -1,
        ]
        aapl_mid = 100.0
        msft_mid = 200.0
        previous_aapl_move = 0

        for index, aapl_move in enumerate(leader_moves, start=1):
            aapl_mid += 0.01 * aapl_move
            msft_mid += 0.01 * previous_aapl_move
            previous_aapl_move = aapl_move

            aapl_state = market_state.by_symbol["AAPL"]
            msft_state = market_state.by_symbol["MSFT"]
            aapl_state.best_price.best_bid_px = aapl_mid - 0.01
            aapl_state.best_price.best_ask_px = aapl_mid + 0.01
            aapl_state.best_price.local_bid_px = aapl_mid - 0.01
            aapl_state.best_price.local_ask_px = aapl_mid + 0.01
            aapl_state.best_price.best_bid_sz = 10
            aapl_state.best_price.best_ask_sz = 10
            aapl_state.best_price.local_bid_sz = 10
            aapl_state.best_price.local_ask_sz = 10
            aapl_state.local_bids = [BookLevel(price=aapl_mid - 0.01, size=10 + aapl_move)]
            aapl_state.local_asks = [BookLevel(price=aapl_mid + 0.01, size=10 - aapl_move)]
            aapl_state.local_multi_level_voi = float(aapl_move)
            aapl_state.global_l1_voi = float(aapl_move)
            aapl_state.last_book_update_ns = index

            msft_state.best_price.best_bid_px = msft_mid - 0.01
            msft_state.best_price.best_ask_px = msft_mid + 0.01
            msft_state.best_price.local_bid_px = msft_mid - 0.01
            msft_state.best_price.local_ask_px = msft_mid + 0.01
            msft_state.best_price.best_bid_sz = 10
            msft_state.best_price.best_ask_sz = 10
            msft_state.best_price.local_bid_sz = 10
            msft_state.best_price.local_ask_sz = 10
            msft_state.local_bids = [BookLevel(price=msft_mid - 0.01, size=10)]
            msft_state.local_asks = [BookLevel(price=msft_mid + 0.01, size=10)]
            msft_state.local_multi_level_voi = 0.0
            msft_state.global_l1_voi = 0.0
            msft_state.last_book_update_ns = index

            rows = build_market_making_feature_batch(
                market_state,
                PortfolioLedger(),
                now_ns=index,
                tick_size=0.01,
            ).rows
            graph.update(rows, tick_size=0.01)

        overlay = graph.overlay_for("MSFT")
        spread_overlay = graph.spread_overlay_for("MSFT")
        forward_edge = graph.pair_state("MSFT", "AAPL")
        reverse_edge = graph.pair_state("AAPL", "MSFT")

        self.assertEqual(overlay.leader_symbol, "AAPL")
        self.assertGreater(overlay.confidence, 0.0)
        self.assertNotEqual(overlay.shift_ticks, 0.0)
        self.assertIsNotNone(forward_edge)
        self.assertIsNotNone(reverse_edge)
        assert forward_edge is not None
        assert reverse_edge is not None
        self.assertGreater(abs(forward_edge.score), abs(reverse_edge.score))
        self.assertEqual(spread_overlay.peer_symbol, "AAPL")
        self.assertGreater(spread_overlay.confidence, 0.0)
        self.assertNotEqual(spread_overlay.shift_ticks, 0.0)

        inventory_overlay = graph.inventory_overlay_for(
            "MSFT",
            inventory_lots_by_symbol={
                "AAPL": -5,
                "MSFT": 5,
            },
        )
        self.assertEqual(inventory_overlay.hedge_symbol, "AAPL")
        self.assertGreater(inventory_overlay.hedge_confidence, 0.0)
        self.assertGreater(inventory_overlay.hedge_ratio, 0.0)
        self.assertLess(inventory_overlay.hedge_multiplier, 1.0)

    def test_adaptive_parameter_provider_uses_session_trade_pacing(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
                strategy=StrategyConfig(
                    target_trades_per_day=200,
                    session_timezone="UTC",
                    session_open_local=LocalTime(9, 30),
                    session_close_local=LocalTime(16, 0),
                ),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        midday_clock = SessionClock(
            runtime.config.runtime.strategy,
            runtime.config.runtime.risk,
            now_provider=lambda: datetime(2026, 4, 2, 12, 45, tzinfo=timezone.utc),
        )
        order_ledger = OrderLedger()
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
            session_clock=midday_clock,
        )

        behind = provider.for_symbol("AAPL").values

        for idx in range(120):
            order_id = f"fill-{idx}"
            order_ledger.append_fill(
                FillRecord(
                    order_id=order_id,
                    symbol="AAPL",
                    side=OrderSide.BID,
                    executed_size=1,
                    executed_price=100.0,
                    status="filled",
                    event_ts_ns=idx + 1,
                    execution_index=0,
                )
            )

        runtime.market_state.by_symbol["AAPL"].last_book_update_ns = time.monotonic_ns()
        ahead = provider.for_symbol("AAPL").values

        self.assertAlmostEqual(behind.session_elapsed_fraction, 0.5, places=3)
        self.assertAlmostEqual(behind.expected_trade_count, 100.0, places=6)
        self.assertEqual(behind.observed_trade_count, 0)
        self.assertGreater(behind.pace_multiplier, ahead.pace_multiplier)
        self.assertGreater(ahead.trade_count_ratio, 1.0)
        self.assertGreaterEqual(ahead.pace_multiplier, 1.0)

        runtime.stop()

    def test_market_maker_switches_to_flatten_only_near_close(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
                risk=RiskConfig(flatten_start_minutes_to_close=10),
                strategy=StrategyConfig(
                    target_trades_per_day=200,
                    session_timezone="UTC",
                    session_open_local=LocalTime(9, 30),
                    session_close_local=LocalTime(16, 0),
                ),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        portfolio = PortfolioLedger()
        portfolio.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        closing_clock = SessionClock(
            runtime.config.runtime.strategy,
            runtime.config.runtime.risk,
            now_provider=lambda: datetime(2026, 4, 2, 15, 55, tzinfo=timezone.utc),
        )
        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            TopOfBookMarketMakerConfig(
                default_parameters=SymbolStrategyParameters(quote_size_lots=1)
            ),
            session_clock=closing_clock,
        )

        targets, traces = strategy.generate_targets(portfolio_ledger=portfolio)

        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0].enable_bid)
        self.assertTrue(targets[0].enable_ask)
        self.assertEqual(targets[0].ask_size, 2)
        self.assertEqual(targets[0].reason, "close_flatten_only")
        self.assertTrue(traces[0].diagnostics.extra["flatten_only_mode"])
        self.assertEqual(traces[0].diagnostics.extra["flatten_target_lots"], 2)
        self.assertGreaterEqual(
            traces[0].diagnostics.extra["close_urgency_multiplier"],
            1.0,
        )

        flat_portfolio = PortfolioLedger()
        flat_targets, flat_traces = strategy.generate_targets(
            portfolio_ledger=flat_portfolio,
        )

        self.assertFalse(flat_targets[0].enable_bid)
        self.assertFalse(flat_targets[0].enable_ask)
        self.assertTrue(flat_traces[0].diagnostics.extra["flatten_only_mode"])

        runtime.stop()

    def test_default_market_maker_keeps_quotes_enabled_in_short_degraded_safe_mode(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
                # See test_control_cycle_handles_price_reversal_path for why
                # this is needed for a single-cycle test.
                risk=RiskConfig(warmup_minutes_at_open=0),
            )
        )
        # Without an explicit session clock this test depends on real
        # wall-clock time (market hours) via build_runtime's default
        # SessionClock; pin it open so the test doesn't flake outside
        # trading hours.
        attach_open_session_clock(runtime)
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.trading is not None
        runtime.trading.kill_switch.update(
            ReconciliationHealth(
                waiting_list_stale_ms=99_999,
                portfolio_stale_ms=0,
                position_mismatch_lots=0,
                broker_connected=True,
            )
        )
        runtime.attach_default_market_maker()

        targets, traces = runtime.strategy.generate_targets(
            portfolio_ledger=runtime.trading.portfolio_ledger,
        )

        self.assertEqual(runtime.trading.kill_switch.mode, SafeMode.DEGRADED_RECONCILE)
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].enable_bid)
        self.assertTrue(targets[0].enable_ask)
        self.assertNotEqual(targets[0].reason, "symbol_disabled")
        self.assertEqual(
            traces[0].diagnostics.extra["safe_mode"],
            SafeMode.DEGRADED_RECONCILE.value,
        )
        self.assertLess(traces[0].diagnostics.allocation_weight, 1.5)

        runtime.stop()

    def test_default_market_maker_flatten_only_on_position_mismatch_safe_mode(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.trading is not None
        runtime.trading.portfolio_ledger.update_position(
            "AAPL",
            long_shares=0,
            short_shares=500,
            long_price=0.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=time.monotonic_ns(),
        )
        runtime.trading.kill_switch.update(
            ReconciliationHealth(
                waiting_list_stale_ms=0,
                portfolio_stale_ms=0,
                position_mismatch_lots=99,
                broker_connected=True,
            )
        )
        runtime.attach_default_market_maker()

        targets, traces = runtime.strategy.generate_targets(
            portfolio_ledger=runtime.trading.portfolio_ledger,
        )

        self.assertEqual(runtime.trading.kill_switch.mode, SafeMode.POSITION_MISMATCH)
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].enable_bid)
        self.assertFalse(targets[0].enable_ask)
        self.assertEqual(targets[0].bid_size, 5)
        self.assertEqual(targets[0].reason, "close_flatten_only")
        self.assertTrue(traces[0].diagnostics.extra["flatten_only_mode"])
        self.assertEqual(traces[0].diagnostics.extra["flatten_target_lots"], 5)
        self.assertEqual(
            traces[0].diagnostics.extra["safe_mode"],
            SafeMode.POSITION_MISMATCH.value,
        )

        runtime.stop()

    def test_default_market_maker_chunks_short_flatten_by_affordable_buying_power(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.trading is not None
        runtime.trading.portfolio_ledger.update_position(
            "AAPL",
            long_shares=0,
            short_shares=500,
            long_price=0.0,
            short_price=100.0,
            realized_pl=0.0,
            ts_ns=time.monotonic_ns(),
        )
        runtime.trading.portfolio_ledger.summary.total_bp = 25_000.0
        runtime.trading.kill_switch.update(
            ReconciliationHealth(
                waiting_list_stale_ms=0,
                portfolio_stale_ms=0,
                position_mismatch_lots=99,
                broker_connected=True,
            )
        )
        runtime.attach_default_market_maker()

        targets, traces = runtime.strategy.generate_targets(
            portfolio_ledger=runtime.trading.portfolio_ledger,
        )

        self.assertTrue(targets[0].flatten_mode)
        self.assertTrue(targets[0].enable_bid)
        self.assertFalse(targets[0].enable_ask)
        self.assertEqual(targets[0].bid_size, 2)
        self.assertEqual(traces[0].diagnostics.extra["flatten_target_lots"], 5)
        self.assertEqual(
            traces[0].diagnostics.extra["max_affordable_bid_flatten_lots"],
            2,
        )

        runtime.stop()

    def test_reconciler_emits_market_flatten_command_when_target_is_flatten_mode(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=100.0,
                    ask_px=100.02,
                    bid_size=0,
                    ask_size=3,
                    enable_bid=False,
                    enable_ask=True,
                    flatten_mode=True,
                    reason="close_flatten_only",
                )
            ],
            now_ns=time.monotonic_ns(),
        )

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0].action, OrderIntentAction.FLATTEN)
        self.assertEqual(result.commands[0].side, OrderSide.ASK)
        self.assertEqual(result.commands[0].size, 3)
        self.assertEqual(result.commands[0].reason, "close_flatten_only")

    def test_reconciler_flattens_even_if_target_side_is_disabled(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=100.0,
                    ask_px=100.02,
                    bid_size=0,
                    ask_size=2,
                    enable_bid=False,
                    enable_ask=False,
                    flatten_mode=True,
                    reason="close_flatten_only",
                )
            ],
            now_ns=time.monotonic_ns(),
        )

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0].action, OrderIntentAction.FLATTEN)
        self.assertEqual(result.commands[0].side, OrderSide.ASK)
        self.assertEqual(result.commands[0].size, 2)

    def test_market_maker_emits_flatten_target_on_stale_book_near_close(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
                risk=RiskConfig(flatten_start_minutes_to_close=10),
                strategy=StrategyConfig(
                    session_timezone="UTC",
                    session_open_local=LocalTime(9, 30),
                    session_close_local=LocalTime(16, 0),
                ),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.last_book_update_ns = time.monotonic_ns() - 5_000_000_000
        closing_clock = SessionClock(
            runtime.config.runtime.strategy,
            runtime.config.runtime.risk,
            now_provider=lambda: datetime(2026, 4, 2, 15, 59, tzinfo=timezone.utc),
        )
        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            TopOfBookMarketMakerConfig(
                default_parameters=SymbolStrategyParameters(quote_age_limit_ms=1)
            ),
            session_clock=closing_clock,
        )
        portfolio = PortfolioLedger()
        portfolio.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=time.monotonic_ns(),
        )

        targets, traces = strategy.generate_targets(portfolio_ledger=portfolio)

        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].flatten_mode)
        self.assertFalse(targets[0].enable_bid)
        self.assertTrue(targets[0].enable_ask)
        self.assertEqual(targets[0].ask_size, 2)
        self.assertEqual(targets[0].reason, "close_flatten_only")
        self.assertEqual(traces[0].diagnostics.participation_mode, "passive_one_sided")
        runtime.stop()

    def test_market_maker_flatten_converges_as_portfolio_position_shrinks(self) -> None:
        market_state = MarketState()
        market_state.by_symbol["AAPL"] = SymbolState(
            symbol="AAPL",
            best_price=BestPriceSnapshot(
                symbol="AAPL",
                best_bid_px=100.0,
                best_bid_sz=5,
                best_ask_px=100.02,
                best_ask_sz=5,
                global_bid_px=100.0,
                global_bid_sz=5,
                global_ask_px=100.02,
                global_ask_sz=5,
                local_bid_px=100.0,
                local_bid_sz=5,
                local_ask_px=100.02,
                local_ask_sz=5,
                update_ts_ns=time.monotonic_ns(),
            ),
            last_book_update_ns=time.monotonic_ns(),
        )
        strategy = TopOfBookMarketMaker(
            market_state,
            parameter_provider=StaticParameterProvider(
                default=SymbolStrategyParameters(
                    quote_size_lots=1,
                    symbol_enable_flag=True,
                    bid_enable_flag=True,
                    ask_enable_flag=True,
                    flatten_only_mode=True,
                    max_affordable_bid_flatten_lots=1,
                )
            ),
        )
        portfolio = PortfolioLedger()

        portfolio.update_position(
            "AAPL",
            long_shares=300,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        targets, _ = strategy.generate_targets(portfolio_ledger=portfolio)
        self.assertTrue(targets[0].flatten_mode)
        self.assertFalse(targets[0].enable_bid)
        self.assertTrue(targets[0].enable_ask)
        self.assertEqual(targets[0].ask_size, 3)

        portfolio.update_position(
            "AAPL",
            long_shares=100,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=2,
        )
        targets, _ = strategy.generate_targets(portfolio_ledger=portfolio)
        self.assertTrue(targets[0].enable_ask)
        self.assertEqual(targets[0].ask_size, 1)

        portfolio.update_position(
            "AAPL",
            long_shares=0,
            short_shares=0,
            long_price=0.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=3,
        )
        targets, _ = strategy.generate_targets(portfolio_ledger=portfolio)
        self.assertTrue(targets[0].flatten_mode)
        self.assertFalse(targets[0].enable_bid)
        self.assertFalse(targets[0].enable_ask)

    def test_market_maker_uses_inventory_taker_overlay_to_reduce_long_inventory(self) -> None:
        market_state = MarketState()
        market_state.by_symbol["AAPL"] = SymbolState(
            symbol="AAPL",
            best_price=BestPriceSnapshot(
                symbol="AAPL",
                best_bid_px=100.0,
                best_bid_sz=5,
                best_ask_px=100.02,
                best_ask_sz=5,
                global_bid_px=100.0,
                global_bid_sz=5,
                global_ask_px=100.02,
                global_ask_sz=5,
                local_bid_px=100.0,
                local_bid_sz=5,
                local_ask_px=100.02,
                local_ask_sz=5,
                update_ts_ns=time.monotonic_ns(),
            ),
            last_book_update_ns=time.monotonic_ns(),
        )
        strategy = TopOfBookMarketMaker(
            market_state,
            parameter_provider=StaticParameterProvider(
                default=SymbolStrategyParameters(
                    quote_size_lots=2,
                    symbol_enable_flag=True,
                    bid_enable_flag=True,
                    ask_enable_flag=True,
                    taker_overlay_mode=True,
                    taker_overlay_size_lots=1,
                    taker_overlay_edge_ticks=0.5,
                )
            ),
        )
        portfolio = PortfolioLedger()
        portfolio.update_position(
            "AAPL",
            long_shares=300,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )

        targets, traces = strategy.generate_targets(portfolio_ledger=portfolio)

        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].flatten_mode)
        self.assertFalse(targets[0].enable_bid)
        self.assertTrue(targets[0].enable_ask)
        self.assertEqual(targets[0].ask_size, 1)
        self.assertEqual(targets[0].reason, "inventory_taker_overlay")
        self.assertEqual(
            traces[0].diagnostics.extra["taker_overlay_size_lots"],
            1,
        )
        self.assertEqual(
            traces[0].diagnostics.extra["taker_overlay_edge_ticks"],
            0.5,
        )

    def test_adaptive_parameter_provider_taker_overlay_requires_poor_queue_and_cooldown(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        state.local_bids = [BookLevel(price=100.0, size=10)]
        state.local_asks = [BookLevel(price=100.01, size=10)]
        state.best_price.best_bid_px = 100.0
        state.best_price.best_bid_sz = 10
        state.best_price.best_ask_px = 100.01
        state.best_price.best_ask_sz = 10
        state.best_price.local_bid_px = 100.0
        state.best_price.local_bid_sz = 10
        state.best_price.local_ask_px = 100.01
        state.best_price.local_ask_sz = 10
        state.last_book_update_ns = 1

        order_ledger = OrderLedger()
        symbol_ledger = order_ledger.ensure_symbol("AAPL")
        order = WorkingOrder(
            symbol="AAPL",
            side=OrderSide.ASK,
            order_id="ask-1",
            price=100.01,
            size=1,
            status="new",
            submitted_ts_ns=1,
            last_update_ts_ns=1,
        )
        symbol_ledger.set_order(OrderSide.ASK, order)
        order_ledger.orders_by_order_id[order.order_id] = order
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            order_ledger=order_ledger,
            history_config=AdaptiveHistoryConfig(
                taker_overlay_max_queue_share=0.20,
                taker_overlay_cooldown_updates=4,
            ),
        )

        state.local_multi_level_voi = -1.0
        state.global_l1_voi = -1.0
        state.last_trade_price = 100.01
        state.last_trade_size = 10
        state.last_book_update_ns = 1
        first_values = provider.for_symbol("AAPL", inventory_lots=2).values

        self.assertTrue(first_values.taker_overlay_mode)
        self.assertEqual(first_values.taker_overlay_size_lots, 1)
        self.assertGreater(first_values.taker_overlay_edge_ticks, 0.05)

        state.last_book_update_ns = 3
        next_values = provider.for_symbol("AAPL", inventory_lots=2).values

        self.assertFalse(next_values.taker_overlay_mode)
        self.assertEqual(next_values.taker_overlay_size_lots, 0)
        runtime.stop()

    def test_strategy_marks_post_close_inventory_as_flatten_only(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
                strategy=StrategyConfig(
                    session_timezone="UTC",
                    session_open_local=LocalTime(9, 30),
                    session_close_local=LocalTime(16, 0),
                ),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        closed_clock = SessionClock(
            runtime.config.runtime.strategy,
            runtime.config.runtime.risk,
            now_provider=lambda: datetime(2026, 4, 2, 16, 1, tzinfo=timezone.utc),
        )
        strategy = TopOfBookMarketMaker(
            runtime.market_state,
            session_clock=closed_clock,
        )
        portfolio = PortfolioLedger()
        portfolio.update_position(
            "AAPL",
            long_shares=100,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=time.monotonic_ns(),
        )

        targets, traces = strategy.generate_targets(portfolio_ledger=portfolio)

        self.assertTrue(targets[0].flatten_mode)
        self.assertFalse(targets[0].enable_bid)
        self.assertTrue(targets[0].enable_ask)
        self.assertEqual(targets[0].ask_size, 1)
        self.assertEqual(targets[0].reason, "close_flatten_only")
        self.assertTrue(traces[0].diagnostics.extra["flatten_only_mode"])
        runtime.stop()

    def test_order_router_submits_market_flatten_and_updates_audit_ledger(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        router = OrderRouter(
            trader,
            FakeOrderFactory(),
            order_ledger,
        )

        router.apply(
            OrderCommand(
                action=OrderIntentAction.FLATTEN,
                symbol="AAPL",
                side=OrderSide.ASK,
                size=4,
                reason="close_flatten_only",
            )
        )

        self.assertEqual(len(trader.submitted_orders), 1)
        self.assertEqual(trader.submitted_orders[0].id, "market-1")
        self.assertEqual(trader.submitted_orders[0].size, 4)

        symbol_ledger = order_ledger.by_symbol["AAPL"]
        self.assertIsNotNone(symbol_ledger.ask_order)
        assert symbol_ledger.ask_order is not None
        self.assertEqual(symbol_ledger.ask_order.order_id, "market-1")
        self.assertEqual(symbol_ledger.ask_order.status, "pending_flatten")
        self.assertEqual(symbol_ledger.ask_order.price, 0.0)

        audit = order_ledger.audits_by_order_id["market-1"]
        self.assertEqual(audit.current_status, "pending_flatten")
        self.assertEqual(audit.submit_size, 4)
        self.assertEqual(audit.submit_price, 0.0)
        self.assertEqual(audit.liquidity, OrderLiquidity.MARKET)

    def test_order_router_does_not_mutate_ledger_when_limit_submit_fails(self) -> None:
        trader = FakeTrader()
        trader.raise_submit_order = True
        order_ledger = OrderLedger()
        router = OrderRouter(
            trader,
            FakeOrderFactory(),
            order_ledger,
        )

        router.apply(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=2,
                reason="test_submit_failure",
            )
        )

        self.assertEqual(trader.submitted_orders, [])
        self.assertEqual(order_ledger.by_symbol, {})
        self.assertEqual(order_ledger.audits_by_order_id, {})

    def test_order_router_does_not_mutate_ledger_when_flatten_submit_fails(self) -> None:
        trader = FakeTrader()
        trader.raise_submit_order = True
        order_ledger = OrderLedger()
        router = OrderRouter(
            trader,
            FakeOrderFactory(),
            order_ledger,
        )

        router.apply(
            OrderCommand(
                action=OrderIntentAction.FLATTEN,
                symbol="AAPL",
                side=OrderSide.ASK,
                size=3,
                reason="test_flatten_failure",
            )
        )

        self.assertEqual(trader.submitted_orders, [])
        self.assertEqual(order_ledger.by_symbol, {})
        self.assertEqual(order_ledger.audits_by_order_id, {})

    def test_order_router_does_not_mark_pending_cancel_when_cancel_submit_fails(self) -> None:
        trader = FakeTrader()
        trader.raise_submit_cancellation = True
        order_ledger = OrderLedger()
        live_order = WorkingOrder(
            symbol="AAPL",
            side=OrderSide.BID,
            order_id="bid-1",
            price=100.0,
            size=2,
            status="new",
            submitted_ts_ns=1,
            last_update_ts_ns=1,
        )
        order_ledger.register_live_order(live_order)
        router = OrderRouter(
            trader,
            FakeOrderFactory(),
            order_ledger,
        )

        router.apply(
            OrderCommand(
                action=OrderIntentAction.CANCEL,
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                reason="test_cancel_failure",
            )
        )

        self.assertEqual(trader.cancelled_orders, [])
        self.assertFalse(live_order.pending_cancel)
        self.assertNotIn("bid-1", order_ledger.audits_by_order_id)

    def test_reconciler_throttles_replace_until_cooldown_expires(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        symbol_ledger = order_ledger.ensure_symbol("AAPL")
        symbol_ledger.set_order(
            OrderSide.BID,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
                last_replace_request_ts_ns=1_050_000_000,
                status="new",
            ),
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(
                stale_order_after_ms=10,
                min_replace_interval_ms=150,
                max_price_drift_ticks=0,
                tick_size=0.01,
            ),
        )
        target = QuoteTarget(
            symbol="AAPL",
            bid_px=99.95,
            ask_px=None,
            bid_size=2,
            ask_size=0,
            enable_bid=True,
            enable_ask=False,
            reason="test",
        )

        throttled = reconciler.build_reconciliation_actions(
            [target],
            now_ns=1_100_000_000,
        )
        released = reconciler.build_reconciliation_actions(
            [target],
            now_ns=1_250_000_000,
        )

        self.assertEqual(len(throttled.commands), 0)
        self.assertEqual(len(released.commands), 1)
        self.assertEqual(released.commands[0].action, OrderIntentAction.REPLACE)
        self.assertEqual(released.commands[0].order_id, "bid-1")

    def test_reconciler_uses_last_update_time_not_original_submit_time_for_staleness(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_900_000_000,
                status="new",
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(
                stale_order_after_ms=750,
                max_price_drift_ticks=1,
                tick_size=0.01,
            ),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=100.0,
                    ask_px=None,
                    bid_size=2,
                    ask_size=0,
                    enable_bid=True,
                    enable_ask=False,
                    reason="fresh_server_update",
                )
            ],
            now_ns=2_000_000_000,
        )

        self.assertEqual(result.commands, [])

    def test_reconciler_keeps_stale_same_price_order_when_queue_share_is_good(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-front",
                price=100.0,
                size=2,
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
                status="new",
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(
                stale_order_after_ms=500,
                stale_keep_queue_share=0.60,
                max_price_drift_ticks=0,
                tick_size=0.01,
            ),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=100.0,
                    ask_px=None,
                    bid_size=2,
                    ask_size=0,
                    bid_queue_share=0.80,
                    enable_bid=True,
                    enable_ask=False,
                    reason="keep_front_queue",
                )
            ],
            now_ns=2_000_000_000,
        )

        self.assertEqual(result.commands, [])

    def test_reconciler_does_not_replace_after_small_partial_fill_topup(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                executed_size=1,
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
                status="new",
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(
                stale_order_after_ms=750,
                max_price_drift_ticks=1,
                tick_size=0.01,
            ),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=100.0,
                    ask_px=None,
                    bid_size=2,
                    ask_size=0,
                    enable_bid=True,
                    enable_ask=False,
                    reason="keep_queue_position",
                )
            ],
            now_ns=1_100_000_000,
        )

        self.assertEqual(result.commands, [])

    def test_reconciler_replaces_over_aggressive_fresh_bid_when_target_moves_down(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
                status="new",
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(
                stale_order_after_ms=750,
                max_price_drift_ticks=0,
                tick_size=0.01,
            ),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=99.95,
                    ask_px=None,
                    bid_size=2,
                    ask_size=0,
                    enable_bid=True,
                    enable_ask=False,
                    reason="preserve_queue_position",
                )
            ],
            now_ns=1_100_000_000,
        )

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0].action, OrderIntentAction.REPLACE)
        self.assertEqual(result.commands[0].order_id, "bid-1")

    def test_reconciler_replaces_less_competitive_fresh_ask_when_target_moves_down(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.ASK,
                order_id="ask-1",
                price=100.05,
                size=2,
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
                status="new",
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(
                stale_order_after_ms=750,
                max_price_drift_ticks=0,
                tick_size=0.01,
            ),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=None,
                    ask_px=100.02,
                    bid_size=0,
                    ask_size=2,
                    enable_bid=False,
                    enable_ask=True,
                    reason="improve_touch",
                )
            ],
            now_ns=1_100_000_000,
        )

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0].action, OrderIntentAction.REPLACE)
        self.assertEqual(result.commands[0].order_id, "ask-1")

    def test_reconciler_holds_submit_briefly_after_order_disappears_from_waiting_list(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.ensure_symbol("AAPL").set_order(
            OrderSide.BID,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                status="inactive",
                inactive_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
            ),
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(inactive_order_hold_ms=100),
        )
        target = QuoteTarget(
            symbol="AAPL",
            bid_px=100.0,
            ask_px=None,
            bid_size=2,
            ask_size=0,
            enable_bid=True,
            enable_ask=False,
            reason="test",
        )

        blocked = reconciler.build_reconciliation_actions(
            [target],
            now_ns=1_050_000_000,
        )
        released = reconciler.build_reconciliation_actions(
            [target],
            now_ns=1_120_000_000,
        )

        self.assertEqual(len(blocked.commands), 0)
        self.assertEqual(len(released.commands), 1)
        self.assertEqual(released.commands[0].action, OrderIntentAction.SUBMIT)

    def test_reconciler_submits_staged_replace_immediately_after_old_order_disappears(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                status="inactive",
                inactive_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
                pending_replace_price=99.95,
                pending_replace_size=3,
                pending_replace_reason="stale_or_off_target",
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(inactive_order_hold_ms=100),
        )
        target = QuoteTarget(
            symbol="AAPL",
            bid_px=99.90,
            ask_px=None,
            bid_size=1,
            ask_size=0,
            enable_bid=True,
            enable_ask=False,
            reason="newer_target",
        )

        result = reconciler.build_reconciliation_actions(
            [target],
            now_ns=1_010_000_000,
        )

        self.assertEqual(len(result.commands), 1)
        self.assertEqual(result.commands[0].action, OrderIntentAction.SUBMIT)
        self.assertAlmostEqual(result.commands[0].price, 99.95, places=8)
        self.assertEqual(result.commands[0].size, 3)
        self.assertEqual(result.commands[0].reason, "stale_or_off_target")
        self.assertFalse(order_ledger.by_symbol["AAPL"].bid_order.has_pending_replace)

    def test_router_and_reconciler_complete_replace_cancel_then_staged_submit_sequence(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 1_000_000.0
        router = OrderRouter(
            trader,
            FakeOrderFactory(),
            order_ledger,
            risk_limits=RiskLimits(
                RiskLimitsConfig(
                    max_position_lots_per_symbol=10,
                    max_gross_position_lots=10,
                    min_buying_power=0.0,
                ),
                portfolio_ledger=portfolio_ledger,
                order_ledger=order_ledger,
            ),
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(inactive_order_hold_ms=100),
        )
        router.apply(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=2,
                reason="initial_quote",
            )
        )

        live_order = order_ledger.by_symbol["AAPL"].bid_order
        assert live_order is not None
        trader.waiting_orders = [
            FakeWaitingOrder(
                id=live_order.order_id,
                symbol="AAPL",
                type="LIMIT_BUY",
                price=100.0,
                size=2,
                executed_size=0,
                status="NEW",
            )
        ]
        replace_target = QuoteTarget(
            symbol="AAPL",
            bid_px=100.05,
            ask_px=None,
            bid_size=3,
            ask_size=0,
            enable_bid=True,
            enable_ask=False,
            reason="repriced",
        )

        replace_result = reconciler.build_reconciliation_actions(
            [replace_target],
            now_ns=1_000_000_000,
        )
        self.assertEqual(len(replace_result.commands), 1)
        self.assertEqual(replace_result.commands[0].action, OrderIntentAction.REPLACE)

        router.apply(replace_result.commands[0])
        self.assertEqual(len(trader.cancelled_orders), 1)
        self.assertTrue(live_order.pending_cancel)
        self.assertAlmostEqual(live_order.pending_replace_price or 0.0, 100.05, places=8)
        self.assertEqual(live_order.pending_replace_size, 3)

        trader.waiting_orders = []
        reconciler.poll_server_state()
        staged_submit = reconciler.build_reconciliation_actions(
            [replace_target],
            now_ns=time.monotonic_ns(),
        )

        self.assertEqual(len(staged_submit.commands), 1)
        self.assertEqual(staged_submit.commands[0].action, OrderIntentAction.SUBMIT)
        self.assertAlmostEqual(staged_submit.commands[0].price or 0.0, 100.05, places=8)
        self.assertEqual(staged_submit.commands[0].size, 3)

        router.apply(staged_submit.commands[0])
        self.assertEqual(len(trader.submitted_orders), 2)
        new_bid = order_ledger.by_symbol["AAPL"].bid_order
        assert new_bid is not None
        self.assertNotEqual(new_bid.order_id, live_order.order_id)
        self.assertAlmostEqual(new_bid.price, 100.05, places=8)
        self.assertEqual(new_bid.size, 3)

    def test_reconciler_refreshes_staged_replace_while_cancel_is_pending(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                status="new",
                pending_cancel=True,
                pending_replace_price=99.95,
                pending_replace_size=2,
                pending_replace_reason="old_replace",
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
                last_replace_request_ts_ns=1_000_000_000,
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=99.90,
                    ask_px=None,
                    bid_size=4,
                    ask_size=0,
                    enable_bid=True,
                    enable_ask=False,
                    reason="newest_replace",
                )
            ],
            now_ns=1_010_000_000,
        )

        bid_order = order_ledger.by_symbol["AAPL"].bid_order
        assert bid_order is not None
        self.assertEqual(result.commands, [])
        self.assertAlmostEqual(bid_order.pending_replace_price or 0.0, 99.90, places=8)
        self.assertEqual(bid_order.pending_replace_size, 4)
        self.assertEqual(bid_order.pending_replace_reason, "newest_replace")

    def test_reconciler_clears_staged_replace_when_target_side_is_disabled(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-1",
                price=100.0,
                size=2,
                status="inactive",
                inactive_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
                pending_replace_price=99.95,
                pending_replace_size=3,
                pending_replace_reason="stale_or_off_target",
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(inactive_order_hold_ms=100),
        )

        result = reconciler.build_reconciliation_actions(
            [
                QuoteTarget(
                    symbol="AAPL",
                    bid_px=None,
                    ask_px=None,
                    bid_size=0,
                    ask_size=0,
                    enable_bid=False,
                    enable_ask=False,
                    reason="symbol_disabled",
                )
            ],
            now_ns=1_010_000_000,
        )

        bid_order = order_ledger.by_symbol["AAPL"].bid_order
        assert bid_order is not None
        self.assertEqual(result.commands, [])
        self.assertFalse(bid_order.has_pending_replace)

    def test_reconciler_skips_inactive_hold_for_flatten_after_passive_order_disappears(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.ensure_symbol("AAPL").set_order(
            OrderSide.ASK,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.ASK,
                order_id="ask-1",
                price=100.1,
                size=3,
                status="inactive",
                inactive_ts_ns=2_000_000_000,
                last_update_ts_ns=2_000_000_000,
            ),
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(inactive_order_hold_ms=100),
        )
        target = QuoteTarget(
            symbol="AAPL",
            bid_px=None,
            ask_px=100.1,
            bid_size=0,
            ask_size=3,
            enable_bid=False,
            enable_ask=True,
            flatten_mode=True,
            reason="close_flatten_only",
        )

        immediate = reconciler.build_reconciliation_actions(
            [target],
            now_ns=2_050_000_000,
        )

        self.assertEqual(len(immediate.commands), 1)
        self.assertEqual(immediate.commands[0].action, OrderIntentAction.FLATTEN)

    def test_reconciliation_health_compares_broker_position_to_executed_fills_not_open_orders(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        order_ledger.ensure_audit(
            order_id="buy-fill-1",
            symbol="AAPL",
            side=OrderSide.BID,
            liquidity=OrderLiquidity.LIMIT,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="buy-fill-1",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=100,
                executed_price=100.0,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )
        order_ledger.ensure_symbol("AAPL").set_order(
            OrderSide.ASK,
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.ASK,
                order_id="ask-live-1",
                price=100.1,
                size=2,
                executed_size=0,
                status="new",
                submitted_ts_ns=1_000_000_000,
                last_update_ts_ns=1_000_000_000,
            ),
        )
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=100,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1_000_000_000,
        )
        portfolio_ledger.summary.last_update_ts_ns = 1_000_000_000
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(),
        )

        health = reconciler.reconciliation_health(now_ns=1_000_000_000)

        self.assertEqual(order_ledger.get_net_executed_lots("AAPL"), 1)
        self.assertEqual(health.position_mismatch_lots, 0)

        portfolio_ledger.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1_000_000_000,
        )
        health_mismatch = reconciler.reconciliation_health(now_ns=1_000_000_000)

        self.assertEqual(health_mismatch.position_mismatch_lots, 1)

    def test_poll_server_state_rebaselines_local_position_to_broker_when_no_live_orders_remain(self) -> None:
        trader = FakeTrader()
        trader.connected = True
        trader.waiting_orders = []
        trader.portfolio_items = {
            "AAPL": FakePortfolioItem(
                long_shares=200,
                short_shares=0,
                long_price=100.0,
                short_price=0.0,
                realized_pl=0.0,
            )
        }
        trader.portfolio_summary = FakePortfolioSummary(
            total_bp=980_000.0,
            total_shares=200,
            total_realized_pl=0.0,
        )
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        kill_switch = KillSwitchController(
            SafeModeConfig(max_position_mismatch_lots=1)
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(),
            kill_switch=kill_switch,
        )

        reconciler.poll_server_state()

        self.assertEqual(portfolio_ledger.get_net_lots("AAPL"), 2)
        self.assertEqual(order_ledger.get_net_executed_lots("AAPL"), 2)
        self.assertEqual(
            reconciler.reconciliation_health().position_mismatch_lots,
            0,
        )
        self.assertEqual(kill_switch.mode, SafeMode.NORMAL)

    def test_poll_server_state_rebaselines_mismatched_symbol_even_with_unrelated_live_order(self) -> None:
        trader = FakeTrader()
        trader.connected = True
        trader.waiting_orders = [
            FakeWaitingOrder(
                id="xom-live-1",
                symbol="XOM",
                type="limit_buy",
                price=100.0,
                size=1,
                executed_size=0,
                status="NEW",
            ),
        ]
        trader.portfolio_items = {
            "AAPL": FakePortfolioItem(
                long_shares=200,
                short_shares=0,
                long_price=100.0,
                short_price=0.0,
                realized_pl=0.0,
            ),
            "XOM": FakePortfolioItem(
                long_shares=0,
                short_shares=0,
                long_price=0.0,
                short_price=0.0,
                realized_pl=0.0,
            ),
        }
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        kill_switch = KillSwitchController(
            SafeModeConfig(max_position_mismatch_lots=1)
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(),
            kill_switch=kill_switch,
        )

        reconciler.poll_server_state()

        self.assertEqual(portfolio_ledger.get_net_lots("AAPL"), 2)
        self.assertEqual(order_ledger.get_net_executed_lots("AAPL"), 2)
        self.assertIsNotNone(order_ledger.find_by_order_id("xom-live-1"))
        self.assertTrue(order_ledger.find_by_order_id("xom-live-1").is_live)
        self.assertEqual(
            reconciler.reconciliation_health().position_mismatch_lots,
            0,
        )
        self.assertEqual(kill_switch.mode, SafeMode.NORMAL)

    def test_poll_server_state_clears_local_position_when_symbol_disappears_from_broker_snapshot(self) -> None:
        trader = FakeTrader()
        trader.connected = True
        trader.portfolio_items = {
            "AAPL": FakePortfolioItem(
                long_shares=200,
                short_shares=0,
                long_price=101.0,
                short_price=0.0,
                realized_pl=12.5,
            )
        }
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        reconciler = Reconciler(
            trader,
            order_ledger,
            portfolio_ledger,
            ReconciliationConfig(),
        )

        reconciler.poll_server_state()
        self.assertEqual(portfolio_ledger.get_net_lots("AAPL"), 2)
        self.assertAlmostEqual(portfolio_ledger.positions["AAPL"].long_price, 101.0, places=8)

        trader.portfolio_items = {}
        reconciler.poll_server_state()

        self.assertEqual(portfolio_ledger.get_net_lots("AAPL"), 0)
        self.assertEqual(portfolio_ledger.positions["AAPL"].long_shares, 0)
        self.assertEqual(portfolio_ledger.positions["AAPL"].short_shares, 0)
        self.assertAlmostEqual(portfolio_ledger.positions["AAPL"].realized_pl, 12.5, places=8)

    def test_kill_switch_escalates_degraded_reconcile_to_flatten_only_after_timeout(self) -> None:
        kill_switch = KillSwitchController(
            SafeModeConfig(
                max_waiting_list_staleness_ms=100,
                max_portfolio_staleness_ms=100,
                max_position_mismatch_lots=1,
                max_degraded_duration_ms=500,
            )
        )
        stale_health = ReconciliationHealth(
            waiting_list_stale_ms=250,
            portfolio_stale_ms=0,
            position_mismatch_lots=0,
            broker_connected=True,
        )

        self.assertEqual(
            kill_switch.update(stale_health, now_ns=1_000_000_000),
            SafeMode.DEGRADED_RECONCILE,
        )
        self.assertEqual(
            kill_switch.update(stale_health, now_ns=1_400_000_000),
            SafeMode.DEGRADED_RECONCILE,
        )
        self.assertEqual(
            kill_switch.update(stale_health, now_ns=1_600_000_000),
            SafeMode.FLATTEN_ONLY,
        )
        self.assertEqual(
            kill_switch.update(stale_health, now_ns=1_700_000_000),
            SafeMode.FLATTEN_ONLY,
        )

        healthy = ReconciliationHealth(
            waiting_list_stale_ms=0,
            portfolio_stale_ms=0,
            position_mismatch_lots=0,
            broker_connected=True,
        )
        self.assertEqual(
            kill_switch.update(healthy, now_ns=1_800_000_000),
            SafeMode.NORMAL,
        )

    def test_order_ledger_estimates_limit_rebates_and_market_fees(self) -> None:
        order_ledger = OrderLedger()

        order_ledger.ensure_audit(
            order_id="limit-1",
            symbol="AAPL",
            side=OrderSide.BID,
            submitted_ts_ns=1,
            submit_price=100.0,
            submit_size=1,
            liquidity=OrderLiquidity.LIMIT,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="limit-1",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=100,
                executed_price=100.0,
                status="filled",
                event_ts_ns=2,
                execution_index=0,
            )
        )

        order_ledger.ensure_audit(
            order_id="market-1",
            symbol="AAPL",
            side=OrderSide.ASK,
            submitted_ts_ns=3,
            submit_price=0.0,
            submit_size=1,
            liquidity=OrderLiquidity.MARKET,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="market-1",
                symbol="AAPL",
                side=OrderSide.ASK,
                executed_size=100,
                executed_price=100.1,
                status="filled",
                event_ts_ns=4,
                execution_index=0,
            )
        )

        limit_audit = order_ledger.audits_by_order_id["limit-1"]
        market_audit = order_ledger.audits_by_order_id["market-1"]

        self.assertAlmostEqual(limit_audit.estimated_rebate, 0.20, places=8)
        self.assertAlmostEqual(limit_audit.estimated_fee, 0.0, places=8)
        self.assertAlmostEqual(limit_audit.estimated_net_fee, -0.20, places=8)

        self.assertAlmostEqual(market_audit.estimated_rebate, 0.0, places=8)
        self.assertAlmostEqual(market_audit.estimated_fee, 0.30, places=8)
        self.assertAlmostEqual(market_audit.estimated_net_fee, 0.30, places=8)

        self.assertAlmostEqual(order_ledger.estimated_total_rebate, 0.20, places=8)
        self.assertAlmostEqual(order_ledger.estimated_total_fee, 0.30, places=8)
        self.assertAlmostEqual(order_ledger.estimated_total_net_fee, 0.10, places=8)

    def test_order_audit_normalizes_fill_size_in_lots_or_shares(self) -> None:
        order_ledger = OrderLedger()
        lots_audit = order_ledger.ensure_audit(
            order_id="lot-fill",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_price=100.0,
            submit_size=2,
            liquidity=OrderLiquidity.LIMIT,
        )
        shares_audit = order_ledger.ensure_audit(
            order_id="share-fill",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_price=100.0,
            submit_size=2,
            liquidity=OrderLiquidity.LIMIT,
        )

        order_ledger.append_fill(
            FillRecord(
                order_id="lot-fill",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=2,
                executed_price=100.25,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="share-fill",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=200,
                executed_price=100.25,
                status="filled",
                event_ts_ns=2,
                execution_index=0,
            )
        )

        self.assertEqual(lots_audit.cumulative_executed_lots, 2)
        self.assertEqual(lots_audit.cumulative_executed_shares, 200)
        self.assertAlmostEqual(lots_audit.volume_weighted_fill_price, 100.25, places=8)
        self.assertAlmostEqual(lots_audit.estimated_rebate, 0.40, places=8)

        self.assertEqual(shares_audit.cumulative_executed_lots, 2)
        self.assertEqual(shares_audit.cumulative_executed_shares, 200)
        self.assertAlmostEqual(shares_audit.volume_weighted_fill_price, 100.25, places=8)
        self.assertAlmostEqual(shares_audit.estimated_rebate, 0.40, places=8)

    def test_order_audit_clamps_sub_lot_share_fills_to_one_lot_when_parent_is_known(self) -> None:
        order_ledger = OrderLedger()
        audit = order_ledger.ensure_audit(
            order_id="partial-share-fill",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_price=100.0,
            submit_size=1,
            liquidity=OrderLiquidity.LIMIT,
        )

        order_ledger.append_fill(
            FillRecord(
                order_id="partial-share-fill",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=50,
                executed_price=100.0,
                status="partially_filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )

        self.assertEqual(audit.cumulative_executed_lots, 1)
        self.assertEqual(audit.cumulative_executed_shares, 100)
        self.assertAlmostEqual(audit.estimated_rebate, 0.20, places=8)

    def test_build_session_metrics_aggregates_passive_aggressive_and_fee_mix(self) -> None:
        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="limit-1",
            symbol="AAPL",
            side=OrderSide.BID,
            liquidity=OrderLiquidity.LIMIT,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="limit-1",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=100,
                executed_price=100.0,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )
        order_ledger.ensure_audit(
            order_id="market-1",
            symbol="AAPL",
            side=OrderSide.ASK,
            liquidity=OrderLiquidity.MARKET,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="market-1",
                symbol="AAPL",
                side=OrderSide.ASK,
                executed_size=200,
                executed_price=100.1,
                status="filled",
                event_ts_ns=2,
                execution_index=0,
            )
        )

        metrics = build_session_metrics(order_ledger)

        self.assertEqual(metrics.executed_trades, 2)
        self.assertEqual(metrics.executed_shares, 300)
        self.assertEqual(metrics.passive_fills, 1)
        self.assertEqual(metrics.aggressive_fills, 1)
        self.assertAlmostEqual(metrics.passive_fill_ratio, 0.5, places=8)
        self.assertAlmostEqual(metrics.estimated_rebates, 0.20, places=8)
        self.assertAlmostEqual(metrics.estimated_fees, 0.60, places=8)
        self.assertAlmostEqual(metrics.estimated_net_fees, 0.40, places=8)

    def test_build_session_metrics_stays_correct_after_completed_audits_are_archived(self) -> None:
        order_ledger = OrderLedger(max_completed_audits_retained=1, min_completed_audit_retention_ns=0)
        for order_id, side, liquidity, size in (
            ("limit-1", OrderSide.BID, OrderLiquidity.LIMIT, 100),
            ("market-1", OrderSide.ASK, OrderLiquidity.MARKET, 200),
        ):
            audit = order_ledger.ensure_audit(
                order_id=order_id,
                symbol="AAPL",
                side=side,
                submitted_ts_ns=1,
                liquidity=liquidity,
            )
            order_ledger.append_fill(
                FillRecord(
                    order_id=order_id,
                    symbol="AAPL",
                    side=side,
                    executed_size=size,
                    executed_price=100.0,
                    status="filled",
                    event_ts_ns=2,
                    execution_index=0,
                )
            )
            audit.current_status = "filled"
            audit.last_update_ts_ns = 2

        before_archive = build_session_metrics(order_ledger)
        order_ledger.archive_completed_audits(now_ns=10)
        after_archive = build_session_metrics(order_ledger)

        self.assertLess(len(order_ledger.audits_by_order_id), 2)
        self.assertEqual(after_archive, before_archive)

    def test_order_ledger_keeps_previous_order_id_lookup_after_slot_replacement(self) -> None:
        order_ledger = OrderLedger()
        first = WorkingOrder(
            symbol="AAPL",
            side=OrderSide.BID,
            order_id="bid-old",
            price=100.0,
            size=2,
            status="new",
            submitted_ts_ns=1,
            last_update_ts_ns=1,
        )
        second = WorkingOrder(
            symbol="AAPL",
            side=OrderSide.BID,
            order_id="bid-new",
            price=100.01,
            size=2,
            status="new",
            submitted_ts_ns=2,
            last_update_ts_ns=2,
        )

        order_ledger.register_live_order(first)
        order_ledger.register_live_order(second)

        self.assertIs(order_ledger.find_by_order_id("bid-old"), first)
        self.assertIs(order_ledger.find_by_order_id("bid-new"), second)
        self.assertIs(order_ledger.by_symbol["AAPL"].bid_order, second)

    def test_adaptive_ewma_preserves_negative_signed_imbalance(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        state = runtime.market_state.by_symbol["AAPL"]
        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)

        state.global_bids = [FakeOrderBookEntry(price=100.00, size=1)]
        state.global_asks = [FakeOrderBookEntry(price=100.02, size=9)]
        state.last_book_update_ns = 2
        provider.for_symbol("AAPL")

        state.global_bids = [FakeOrderBookEntry(price=100.00, size=9)]
        state.global_asks = [FakeOrderBookEntry(price=100.02, size=1)]
        state.last_book_update_ns = 3
        provider.for_symbol("AAPL")

        ewma_signed = provider.state_by_symbol["AAPL"].ewma_signed_imbalance
        self.assertLess(ewma_signed, 0.0)
        self.assertGreater(ewma_signed, -1.0)

        runtime.stop()

    def test_adaptive_provider_emits_explored_strategy_sleeve_weights(self) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None
        provider = AdaptiveParameterProvider(runtime.market_state, tick_size=0.01)

        values = provider.for_symbol("AAPL").values
        sleeve_weights = [
            values.sleeve_weight_base_mm,
            values.sleeve_weight_lead_lag,
            values.sleeve_weight_pair_spread,
            values.sleeve_weight_inv_hedge,
            values.sleeve_weight_taker_exit,
            values.sleeve_weight_noise_fade,
        ]

        self.assertAlmostEqual(sum(sleeve_weights), 1.0, places=8)
        self.assertGreaterEqual(
            values.sleeve_weight_base_mm,
            provider.history_config.sleeve_min_base_weight,
        )
        self.assertTrue(all(weight > 0.0 for weight in sleeve_weights))

        runtime.stop()

    def test_online_strategy_allocator_rewards_sleeve_with_realized_pnl_credit(self) -> None:
        allocator = ContextualStrategyAllocator()
        weights_before = allocator.update(
            {
                "BASE_MM": 0.6,
                "LEAD_LAG": 0.1,
                "PAIR_SPREAD": 0.1,
                "INV_HEDGE": 0.1,
                "TAKER_EXIT": 0.1,
                "NOISE_FADE": 0.1,
            },
            reward_multiplier=1.0,
            sleeve_rewards={
                "LEAD_LAG": 0.8,
            },
        )

        for _ in range(8):
            weights_after = allocator.update(
                {
                    "BASE_MM": 0.6,
                    "LEAD_LAG": 0.1,
                    "PAIR_SPREAD": 0.1,
                    "INV_HEDGE": 0.1,
                    "TAKER_EXIT": 0.1,
                    "NOISE_FADE": 0.1,
                },
                reward_multiplier=1.0,
                sleeve_rewards={
                    "LEAD_LAG": 0.8,
                },
            )

        self.assertGreater(
            weights_after.lead_lag,
            weights_before.lead_lag,
        )
        self.assertGreater(
            weights_after.lead_lag,
            weights_after.pair_spread,
        )

    def test_risk_limits_include_existing_live_orders_in_symbol_projection(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 1_000_000.0
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="bid-live",
                price=100.0,
                size=3,
                status="new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        limits = RiskLimits(
            RiskLimitsConfig(max_position_lots_per_symbol=5, max_gross_position_lots=10),
            portfolio_ledger=portfolio_ledger,
            order_ledger=order_ledger,
        )

        rejected = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=3,
            )
        )

        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.first_reject_reason(), "symbol_position_limit")

    def test_risk_limits_do_not_net_opposite_side_live_orders_when_checking_new_submit(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 1_000_000.0
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=500,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.ASK,
                order_id="ask-live",
                price=100.1,
                size=5,
                status="new",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        limits = RiskLimits(
            RiskLimitsConfig(max_position_lots_per_symbol=5, max_gross_position_lots=10),
            portfolio_ledger=portfolio_ledger,
            order_ledger=order_ledger,
        )

        rejected_bid = limits.evaluate(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=1,
            )
        )

        self.assertFalse(rejected_bid.allowed)
        self.assertEqual(rejected_bid.first_reject_reason(), "symbol_position_limit")

    def test_reconciliation_health_ignores_inactive_orders_for_waiting_staleness(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="inactive-bid",
                price=100.0,
                size=1,
                status="inactive",
                submitted_ts_ns=1,
                last_update_ts_ns=1,
                inactive_ts_ns=1,
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            PortfolioLedger(),
            ReconciliationConfig(stale_order_after_ms=10),
        )

        health = reconciler.reconciliation_health(now_ns=10_000_000_000)

        self.assertEqual(health.waiting_list_stale_ms, 0)

    def test_poll_server_state_marks_orphaned_replaced_orders_inactive_by_order_id(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        old_order = WorkingOrder(
            symbol="AAPL",
            side=OrderSide.BID,
            order_id="bid-old",
            price=100.0,
            size=2,
            status="new",
            pending_cancel=True,
            submitted_ts_ns=1,
            last_update_ts_ns=1,
        )
        new_order = WorkingOrder(
            symbol="AAPL",
            side=OrderSide.BID,
            order_id="bid-new",
            price=99.99,
            size=2,
            status="new",
            submitted_ts_ns=2,
            last_update_ts_ns=2,
        )
        order_ledger.register_live_order(old_order)
        order_ledger.register_live_order(new_order)
        reconciler = Reconciler(
            trader,
            order_ledger,
            PortfolioLedger(),
            ReconciliationConfig(),
        )

        reconciler.poll_server_state()

        self.assertEqual(order_ledger.find_by_order_id("bid-old").status, "inactive")
        self.assertFalse(order_ledger.find_by_order_id("bid-old").pending_cancel)
        self.assertEqual(order_ledger.find_by_order_id("bid-new").status, "inactive")

    def test_poll_server_state_keeps_fresh_pending_new_orders_live_during_waiting_list_race(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="pending-bid",
                price=100.0,
                size=1,
                status="pending_new",
                submitted_ts_ns=time.monotonic_ns(),
                last_update_ts_ns=time.monotonic_ns(),
            )
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            PortfolioLedger(),
            ReconciliationConfig(new_order_grace_ms=5_000),
        )

        reconciler.poll_server_state()

        order = order_ledger.find_by_order_id("pending-bid")
        self.assertIsNotNone(order)
        assert order is not None
        self.assertEqual(order.status, "pending_new")

    def test_poll_server_state_degrades_health_on_unknown_waiting_order_type(self) -> None:
        trader = FakeTrader()
        trader.connected = True
        trader.waiting_orders = [
            FakeWaitingOrder(
                id="mystery-order",
                symbol="AAPL",
                type="UNKNOWN_SIDE_KIND",
                price=100.0,
                size=1,
                executed_size=0,
                status="NEW",
            )
        ]
        reconciler = Reconciler(
            trader,
            OrderLedger(),
            PortfolioLedger(),
            ReconciliationConfig(max_parse_failures=1),
        )

        reconciler.poll_server_state()
        health = reconciler.reconciliation_health(now_ns=1_000_000_000)

        self.assertFalse(health.broker_connected)

    def test_reconciler_marks_broker_disconnected_after_execution_sync_failures(self) -> None:
        trader = FakeTrader()
        trader.connected = True
        trader.raise_execution_sync = True
        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="limit-1",
            symbol="AAPL",
            side=OrderSide.BID,
            liquidity=OrderLiquidity.LIMIT,
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            PortfolioLedger(),
            ReconciliationConfig(max_execution_sync_failures=2),
        )

        reconciler.poll_server_state()
        first_health = reconciler.reconciliation_health(now_ns=1_000_000_000)
        reconciler.poll_server_state()
        second_health = reconciler.reconciliation_health(now_ns=1_100_000_000)

        self.assertTrue(first_health.broker_connected)
        self.assertFalse(second_health.broker_connected)

    def test_reconciler_marks_broker_disconnected_after_waiting_list_poll_failure(self) -> None:
        trader = FakeTrader()
        trader.connected = True
        trader.raise_waiting_list = True
        reconciler = Reconciler(
            trader,
            OrderLedger(),
            PortfolioLedger(),
            ReconciliationConfig(max_waiting_list_sync_failures=1),
        )

        reconciler.poll_server_state()
        health = reconciler.reconciliation_health(now_ns=1_000_000_000)

        self.assertFalse(health.broker_connected)

    def test_reconciler_marks_broker_disconnected_after_portfolio_poll_failure(self) -> None:
        trader = FakeTrader()
        trader.connected = True
        trader.raise_portfolio_summary = True
        reconciler = Reconciler(
            trader,
            OrderLedger(),
            PortfolioLedger(),
            ReconciliationConfig(max_portfolio_sync_failures=1),
        )

        reconciler.poll_server_state()
        health = reconciler.reconciliation_health(now_ns=1_000_000_000)

        self.assertFalse(health.broker_connected)

    def test_reconciler_does_not_let_one_success_mask_execution_sync_failures(self) -> None:
        trader = FakeTrader()
        trader.connected = True

        def mixed_execution_poll(order_id: str) -> list:
            if order_id == "bad-order":
                raise RuntimeError("bad order failed")
            return []

        trader.get_executed_orders = mixed_execution_poll  # type: ignore[method-assign]

        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="bad-order",
            symbol="AAPL",
            side=OrderSide.BID,
            liquidity=OrderLiquidity.LIMIT,
        )
        order_ledger.ensure_audit(
            order_id="good-order",
            symbol="AAPL",
            side=OrderSide.ASK,
            liquidity=OrderLiquidity.LIMIT,
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            PortfolioLedger(),
            ReconciliationConfig(max_execution_sync_failures=1),
        )

        reconciler.poll_server_state()
        health = reconciler.reconciliation_health(now_ns=1_000_000_000)

        self.assertFalse(health.broker_connected)

    def test_poll_server_state_logs_failures_and_safe_mode_transitions(self) -> None:
        scenarios = (
            (
                "waiting_list_sync_failed",
                lambda trader, order_ledger: setattr(trader, "raise_waiting_list", True),
                ReconciliationConfig(max_waiting_list_sync_failures=1),
            ),
            (
                "portfolio_sync_failed",
                lambda trader, order_ledger: setattr(trader, "raise_portfolio_summary", True),
                ReconciliationConfig(max_portfolio_sync_failures=1),
            ),
            (
                "execution_sync_failed",
                lambda trader, order_ledger: (
                    order_ledger.ensure_audit(
                        order_id="limit-1",
                        symbol="AAPL",
                        side=OrderSide.BID,
                        liquidity=OrderLiquidity.LIMIT,
                    ),
                    setattr(trader, "raise_execution_sync", True),
                ),
                ReconciliationConfig(max_execution_sync_failures=1),
            ),
            (
                "waiting_order_parse_failed",
                lambda trader, order_ledger: trader.waiting_orders.append(
                    FakeWaitingOrder(
                        id="bad-waiting",
                        symbol="AAPL",
                        type="UNKNOWN_SIDE_KIND",
                        price=100.0,
                        size=1,
                        executed_size=0,
                        status="NEW",
                    )
                ),
                ReconciliationConfig(max_parse_failures=1),
            ),
        )

        for expected_failure_kind, setup_fn, config in scenarios:
            with self.subTest(expected_failure_kind=expected_failure_kind):
                with tempfile.TemporaryDirectory() as tmpdir:
                    telemetry = build_session_telemetry(
                        Path(tmpdir),
                        flush_every=1,
                        max_queue_size=64,
                    )
                    telemetry.start()
                    try:
                        trader = FakeTrader()
                        trader.connected = True
                        order_ledger = OrderLedger()
                        setup_fn(trader, order_ledger)
                        kill_switch = KillSwitchController(
                            SafeModeConfig(),
                            event_logger=telemetry.event_logger,
                        )
                        reconciler = Reconciler(
                            trader,
                            order_ledger,
                            PortfolioLedger(),
                            config,
                            kill_switch=kill_switch,
                            event_logger=telemetry.event_logger,
                        )

                        reconciler.poll_server_state()

                        self.assertEqual(kill_switch.mode, SafeMode.KILL_SWITCH)
                    finally:
                        telemetry.stop()

                    events = [
                        json.loads(line)
                        for line in (Path(tmpdir) / "events.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                        if line.strip()
                    ]
                    kinds = [event.get("kind") for event in events]
                    self.assertIn(expected_failure_kind, kinds)
                    self.assertIn("safe_mode_transition", kinds)
                    transition_events = [
                        event
                        for event in events
                        if event.get("kind") == "safe_mode_transition"
                    ]
                    self.assertEqual(
                        transition_events[-1]["payload"]["next_mode"],
                        SafeMode.KILL_SWITCH.value,
                    )

    def test_reconciler_ingests_new_fill_when_execution_list_rebases_to_shorter_tail(self) -> None:
        @dataclass
        class FakeExecutedOrder:
            id: str
            symbol: str
            type: str
            executed_size: int
            executed_price: float
            status: str
            timestamp: str

        trader = FakeTrader()
        trader.connected = True
        execution_polls = [
            [
                FakeExecutedOrder(
                    id="limit-1",
                    symbol="AAPL",
                    type="LIMIT_BUY",
                    executed_size=1,
                    executed_price=100.0,
                    status="PARTIALLY_FILLED",
                    timestamp="ts-1",
                ),
                FakeExecutedOrder(
                    id="limit-1",
                    symbol="AAPL",
                    type="LIMIT_BUY",
                    executed_size=1,
                    executed_price=100.01,
                    status="PARTIALLY_FILLED",
                    timestamp="ts-2",
                ),
            ],
            [
                FakeExecutedOrder(
                    id="limit-1",
                    symbol="AAPL",
                    type="LIMIT_BUY",
                    executed_size=1,
                    executed_price=100.02,
                    status="FILLED",
                    timestamp="ts-3",
                ),
            ],
        ]

        def truncated_execution_poll(order_id: str) -> list:
            self.assertEqual(order_id, "limit-1")
            if execution_polls:
                return execution_polls.pop(0)
            return []

        trader.get_executed_orders = truncated_execution_poll  # type: ignore[method-assign]

        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="limit-1",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_size=3,
            liquidity=OrderLiquidity.LIMIT,
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            PortfolioLedger(),
            ReconciliationConfig(),
        )

        reconciler.poll_server_state()
        reconciler.poll_server_state()
        audit = order_ledger.audits_by_order_id["limit-1"]

        self.assertEqual(audit.cumulative_executed_lots, 3)
        self.assertEqual(len(audit.fills), 3)
        self.assertEqual(audit.fills[-1].broker_timestamp, "ts-3")

    def test_reconciler_ingests_cumulative_execution_size_updates_at_same_index(self) -> None:
        @dataclass
        class FakeExecutedOrder:
            id: str
            symbol: str
            type: str
            executed_size: int
            executed_price: float
            status: str
            timestamp: str

        trader = FakeTrader()
        trader.connected = True
        execution_polls = [
            [
                FakeExecutedOrder(
                    id="limit-1",
                    symbol="AAPL",
                    type="LIMIT_BUY",
                    executed_size=1,
                    executed_price=100.0,
                    status="PARTIALLY_FILLED",
                    timestamp="same-row",
                )
            ],
            [
                FakeExecutedOrder(
                    id="limit-1",
                    symbol="AAPL",
                    type="LIMIT_BUY",
                    executed_size=3,
                    executed_price=100.0,
                    status="FILLED",
                    timestamp="same-row",
                )
            ],
        ]

        def cumulative_execution_poll(order_id: str) -> list:
            self.assertEqual(order_id, "limit-1")
            if execution_polls:
                return execution_polls.pop(0)
            return []

        trader.get_executed_orders = cumulative_execution_poll  # type: ignore[method-assign]

        order_ledger = OrderLedger()
        order_ledger.ensure_audit(
            order_id="limit-1",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_size=3,
            liquidity=OrderLiquidity.LIMIT,
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            PortfolioLedger(),
            ReconciliationConfig(),
        )

        reconciler.poll_server_state()
        reconciler.poll_server_state()

        audit = order_ledger.audits_by_order_id["limit-1"]
        self.assertEqual(audit.cumulative_executed_lots, 3)
        self.assertEqual(len(audit.fills), 2)
        self.assertEqual(audit.fills[-1].executed_size, 2)

    def test_zero_price_canceled_execution_records_are_ignored(self) -> None:
        order_ledger = OrderLedger()
        audit = order_ledger.ensure_audit(
            order_id="cancel-1",
            symbol="AAPL",
            side=OrderSide.ASK,
            submit_size=1,
            liquidity=OrderLiquidity.LIMIT,
        )

        order_ledger.append_fill(
            FillRecord(
                order_id="cancel-1",
                symbol="AAPL",
                side=OrderSide.ASK,
                executed_size=1,
                executed_price=0.0,
                status="canceled",
                event_ts_ns=1,
                execution_index=0,
            )
        )

        self.assertEqual(len(audit.fills), 0)
        self.assertEqual(audit.cumulative_executed_lots, 0)
        self.assertEqual(order_ledger.get_net_executed_lots("AAPL"), 0)

    def test_poll_server_state_ingests_partial_fill_during_cancel_in_flight(self) -> None:
        @dataclass
        class FakeExecutedOrder:
            id: str
            symbol: str
            type: str
            executed_size: int
            executed_price: float
            status: str
            timestamp: str

        trader = FakeTrader()
        trader.connected = True

        def execution_poll(order_id: str) -> list:
            self.assertEqual(order_id, "cancel-bid")
            return [
                FakeExecutedOrder(
                    id="cancel-bid",
                    symbol="AAPL",
                    type="LIMIT_BUY",
                    executed_size=1,
                    executed_price=100.0,
                    status="PARTIALLY_FILLED",
                    timestamp="fill-during-cancel",
                )
            ]

        trader.get_executed_orders = execution_poll  # type: ignore[method-assign]
        order_ledger = OrderLedger()
        order_ledger.register_live_order(
            WorkingOrder(
                symbol="AAPL",
                side=OrderSide.BID,
                order_id="cancel-bid",
                price=100.0,
                size=2,
                status="pending_cancel",
                pending_cancel=True,
                submitted_ts_ns=1,
                last_update_ts_ns=1,
            )
        )
        order_ledger.ensure_audit(
            order_id="cancel-bid",
            symbol="AAPL",
            side=OrderSide.BID,
            liquidity=OrderLiquidity.LIMIT,
        )
        reconciler = Reconciler(
            trader,
            order_ledger,
            PortfolioLedger(),
            ReconciliationConfig(),
        )

        reconciler.poll_server_state()

        audit = order_ledger.audits_by_order_id["cancel-bid"]
        live_order = order_ledger.find_by_order_id("cancel-bid")
        self.assertEqual(len(audit.fills), 1)
        self.assertEqual(audit.cumulative_executed_lots, 1)
        self.assertEqual(audit.fills[0].broker_timestamp, "fill-during-cancel")
        self.assertIsNotNone(live_order)
        assert live_order is not None
        self.assertEqual(live_order.status, "inactive")

    def test_kill_switch_allows_market_flatten_orders_in_kill_switch_mode(self) -> None:
        kill_switch = KillSwitchController(SafeModeConfig())
        kill_switch.update(
            ReconciliationHealth(
                waiting_list_stale_ms=0,
                portfolio_stale_ms=0,
                position_mismatch_lots=0,
                broker_connected=False,
            ),
            now_ns=1,
        )

        self.assertEqual(kill_switch.mode, SafeMode.KILL_SWITCH)
        self.assertFalse(kill_switch.blocks(OrderIntentAction.CANCEL))
        self.assertFalse(kill_switch.blocks(OrderIntentAction.FLATTEN))
        self.assertTrue(kill_switch.blocks(OrderIntentAction.SUBMIT))

    def test_risk_limits_dynamic_flatten_only_blocks_position_increasing_submits(self) -> None:
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=100,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        risk_limits = RiskLimits(
            RiskLimitsConfig(flatten_only=False),
            portfolio_ledger=portfolio_ledger,
            order_ledger=OrderLedger(),
        )

        risk_limits.set_flatten_only(True)

        buy_command = OrderCommand(
            action=OrderIntentAction.SUBMIT,
            symbol="AAPL",
            side=OrderSide.BID,
            price=100.0,
            size=1,
        )
        sell_command = OrderCommand(
            action=OrderIntentAction.SUBMIT,
            symbol="AAPL",
            side=OrderSide.ASK,
            price=100.0,
            size=1,
        )

        self.assertFalse(risk_limits.evaluate(buy_command).allowed)
        self.assertTrue(risk_limits.evaluate(sell_command).allowed)

    def test_order_router_allows_position_reducing_limit_submit_in_flatten_only_mode(self) -> None:
        trader = FakeTrader()
        order_ledger = OrderLedger()
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.summary.total_bp = 100_000.0
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )
        kill_switch = KillSwitchController(
            SafeModeConfig(max_degraded_duration_ms=1),
        )
        kill_switch.update(
            ReconciliationHealth(
                waiting_list_stale_ms=10_000,
                portfolio_stale_ms=0,
                position_mismatch_lots=0,
                broker_connected=True,
            ),
            now_ns=1_000_000_000,
        )
        kill_switch.update(
            ReconciliationHealth(
                waiting_list_stale_ms=10_000,
                portfolio_stale_ms=0,
                position_mismatch_lots=0,
                broker_connected=True,
            ),
            now_ns=1_010_000_000,
        )
        self.assertEqual(kill_switch.mode, SafeMode.FLATTEN_ONLY)

        router = OrderRouter(
            trader,
            FakeOrderFactory(),
            order_ledger,
            risk_limits=RiskLimits(
                RiskLimitsConfig(
                    max_position_lots_per_symbol=10,
                    max_gross_position_lots=10,
                    min_buying_power=0.0,
                ),
                portfolio_ledger=portfolio_ledger,
                order_ledger=order_ledger,
            ),
            kill_switch=kill_switch,
        )

        router.apply(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.ASK,
                price=100.1,
                size=1,
                reason="reduce_long_inventory",
            )
        )

        self.assertEqual(len(trader.submitted_orders), 1)
        live_ask = order_ledger.by_symbol["AAPL"].ask_order
        self.assertIsNotNone(live_ask)
        assert live_ask is not None
        self.assertEqual(live_ask.status, "pending_new")

    def test_reconciler_infer_side_rejects_unknown_order_types(self) -> None:
        with self.assertRaises(ValueError):
            Reconciler._infer_side("MYSTERY_ORDER_KIND")

    def test_order_ledger_archives_old_completed_audits_without_losing_aggregates(self) -> None:
        order_ledger = OrderLedger(max_completed_audits_retained=1)

        old_audit = order_ledger.ensure_audit(
            order_id="limit-old",
            symbol="AAPL",
            side=OrderSide.BID,
            submitted_ts_ns=1,
            submit_price=100.0,
            submit_size=1,
            liquidity=OrderLiquidity.LIMIT,
        )
        order_ledger.append_fill(
            FillRecord(
                order_id="limit-old",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=100,
                executed_price=100.0,
                status="filled",
                event_ts_ns=2,
                execution_index=0,
            )
        )
        old_audit.current_status = "filled"
        old_audit.last_update_ts_ns = 2

        new_audit = order_ledger.ensure_audit(
            order_id="market-new",
            symbol="AAPL",
            side=OrderSide.ASK,
            submitted_ts_ns=3,
            submit_price=0.0,
            submit_size=1,
            liquidity=OrderLiquidity.MARKET,
        )
        new_audit.cancel_requested_ts_ns = 4
        order_ledger.append_fill(
            FillRecord(
                order_id="market-new",
                symbol="AAPL",
                side=OrderSide.ASK,
                executed_size=100,
                executed_price=100.1,
                status="filled",
                event_ts_ns=5,
                execution_index=0,
            )
        )
        new_audit.current_status = "filled"
        new_audit.last_update_ts_ns = 5

        order_ledger.archive_completed_audits()

        self.assertEqual(len(order_ledger.audits_by_order_id), 1)
        self.assertNotIn("limit-old", order_ledger.audits_by_order_id)
        self.assertEqual(order_ledger.get_net_executed_lots("AAPL"), 0)
        self.assertEqual(order_ledger.fill_count("AAPL"), 2)
        self.assertEqual(order_ledger.cancel_count("AAPL"), 1)
        self.assertAlmostEqual(order_ledger.estimated_total_rebate, 0.20, places=8)
        self.assertAlmostEqual(order_ledger.estimated_total_fee, 0.30, places=8)
        self.assertAlmostEqual(order_ledger.estimated_total_net_fee, 0.10, places=8)

        estimated_rebate, estimated_fee, passive_ratio = order_ledger.symbol_fee_stats("AAPL")
        self.assertAlmostEqual(estimated_rebate, 0.20, places=8)
        self.assertAlmostEqual(estimated_fee, 0.30, places=8)
        self.assertAlmostEqual(passive_ratio, 0.5, places=8)

        metrics = build_session_metrics(order_ledger)
        self.assertEqual(metrics.executed_trades, 2)
        self.assertEqual(metrics.executed_shares, 200)
        self.assertEqual(metrics.passive_fills, 1)
        self.assertEqual(metrics.aggressive_fills, 1)

    def test_order_ledger_retains_recent_completed_audits_until_min_retention_passes(self) -> None:
        order_ledger = OrderLedger(
            max_completed_audits_retained=1,
            min_completed_audit_retention_ns=1_000_000_000,
        )
        old_audit = order_ledger.ensure_audit(
            order_id="old-1",
            symbol="AAPL",
            side=OrderSide.BID,
            liquidity=OrderLiquidity.LIMIT,
        )
        old_audit.current_status = "filled"
        old_audit.last_update_ts_ns = 1_000_000_000
        recent_audit = order_ledger.ensure_audit(
            order_id="recent-1",
            symbol="AAPL",
            side=OrderSide.ASK,
            liquidity=OrderLiquidity.LIMIT,
        )
        recent_audit.current_status = "filled"
        recent_audit.last_update_ts_ns = 2_500_000_000

        order_ledger.archive_completed_audits(now_ns=2_700_000_000)
        self.assertNotIn("old-1", order_ledger.audits_by_order_id)
        self.assertIn("recent-1", order_ledger.audits_by_order_id)

        order_ledger.archive_completed_audits(now_ns=4_000_000_000)
        self.assertEqual(len(order_ledger.audits_by_order_id), 1)
        self.assertIn("recent-1", order_ledger.audits_by_order_id)

    def test_runtime_live_risk_detects_displaced_orders_by_id_map(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        trader = FakeTrader()
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.trading is not None
        order_ledger = runtime.trading.order_ledger
        slot_order = WorkingOrder(
            symbol="AAPL",
            side=OrderSide.BID,
            order_id="slot-bid",
            price=99.99,
            size=1,
            status="new",
        )
        displaced_order = WorkingOrder(
            symbol="AAPL",
            side=OrderSide.BID,
            order_id="displaced-bid",
            price=99.98,
            size=1,
            status="pending_cancel",
            pending_cancel=True,
        )
        order_ledger.ensure_symbol("AAPL").set_order(OrderSide.BID, slot_order)
        order_ledger.orders_by_order_id["displaced-bid"] = displaced_order

        trader.portfolio_items = {}
        runtime.trading.portfolio_ledger.positions.clear()
        try:
            self.assertTrue(runtime._has_live_risk())
        finally:
            runtime.stop()

    def test_json_logger_snapshots_mutable_payloads_at_log_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "events.jsonl"
            logger = JsonlEventLogger(log_path, flush_every=1, max_queue_size=32)
            payload = {
                "target": QuoteTarget(
                    symbol="AAPL",
                    bid_px=100.0,
                    ask_px=100.01,
                    bid_size=1,
                    ask_size=1,
                )
            }
            logger.start()
            try:
                logger.log("strategy_target", **payload)
                payload["target"].bid_px = 99.0
                logger.stop()
            finally:
                logger.stop()

            line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
            event = json.loads(line)
            self.assertEqual(event["kind"], "strategy_target")
            self.assertAlmostEqual(event["payload"]["target"]["bid_px"], 100.0, places=8)

    def test_session_telemetry_rotates_numbered_event_logs_and_tracks_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            session_dir = Path(tmp_dir) / "session"

            first_telemetry = build_session_telemetry(
                session_dir,
                flush_every=1,
                max_queue_size=32,
            )
            first_telemetry.start()
            first_telemetry.event_logger.log("first_run_marker", run_id=1)
            first_telemetry.stop()

            second_telemetry = build_session_telemetry(
                session_dir,
                flush_every=1,
                max_queue_size=32,
            )
            second_telemetry.start()
            second_telemetry.event_logger.log("second_run_marker", run_id=2)
            second_telemetry.stop()

            self.assertEqual(first_telemetry.event_path.name, "events_0001.jsonl")
            self.assertEqual(second_telemetry.event_path.name, "events_0002.jsonl")

            first_events = [
                json.loads(line)
                for line in first_telemetry.event_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            second_events = [
                json.loads(line)
                for line in second_telemetry.event_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(
                [event["kind"] for event in first_events],
                ["first_run_marker"],
            )
            self.assertEqual(first_events[0]["payload"]["run_id"], 1)
            self.assertEqual(
                [event["kind"] for event in second_events],
                ["second_run_marker"],
            )
            self.assertEqual(second_events[0]["payload"]["run_id"], 2)

            latest_path = session_dir / "events.jsonl"
            if latest_path.is_symlink():
                self.assertEqual(latest_path.readlink().name, "events_0002.jsonl")
            else:
                self.assertEqual(
                    latest_path.read_text(encoding="utf-8").strip(),
                    "events_0002.jsonl",
                )

    def test_runtime_resets_session_order_statistics_on_flat_session_reopen(self) -> None:
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        attach_open_session_clock(runtime)
        trader = FakeTrader()
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        runtime.attach_default_market_maker()
        assert runtime.trading is not None
        assert runtime.strategy is not None

        runtime.trading.order_ledger.archived_fill_count_by_symbol["AAPL"] = 7
        runtime.trading.order_ledger.archived_estimated_fee_by_symbol["AAPL"] = 1.4
        runtime.run_strategy_once(execute_orders=False)
        parameter_provider = runtime.strategy._parameter_provider
        parameter_provider.state_by_symbol["AAPL"].ewma_toxicity_score = 0.95
        runtime._last_session_open = False

        _, traces = runtime.run_strategy_once(execute_orders=False)

        self.assertEqual(runtime.trading.order_ledger.total_fill_count(), 0)
        self.assertEqual(
            runtime.trading.order_ledger.archived_estimated_fee_by_symbol,
            {},
        )
        self.assertAlmostEqual(
            traces[0].diagnostics.extra["toxicity_score"],
            0.0,
            places=8,
        )
        runtime.stop()

    def test_piecewise_linear_lut_interpolates_and_clamps(self) -> None:
        lut = PiecewiseLinearLut.from_pairs(
            (
                (0.0, 1.0),
                (0.5, 2.0),
                (1.0, 4.0),
            )
        )

        self.assertAlmostEqual(lut.evaluate(-0.2), 1.0, places=8)
        self.assertAlmostEqual(lut.evaluate(0.25), 1.5, places=8)
        self.assertAlmostEqual(lut.evaluate(0.75), 3.0, places=8)
        self.assertAlmostEqual(lut.evaluate(1.5), 4.0, places=8)

    def test_adaptive_provider_uses_lookup_tables_for_toxicity_and_inventory_pressure(
        self,
    ) -> None:
        trader = FakeTrader()
        runtime = build_runtime(
            RuntimeConfig(
                username="tester",
                password="secret",
                initiator_cfg=Path("initiator.cfg"),
                telemetry=TelemetryConfig(enable_event_logging=False),
                market_data=MarketDataConfig(symbols=("AAPL",)),
            )
        )
        bootstrap_once(
            runtime,
            trader_factory=lambda username: trader,
            order_book_type=FakeOrderBookType,
        )
        assert runtime.market_state is not None

        lookup_tables = ParameterLookupTables(
            toxicity_width_multiplier=PiecewiseLinearLut.from_pairs(
                ((0.0, 1.0), (1.0, 1.4))
            ),
            toxicity_size_multiplier=PiecewiseLinearLut.from_pairs(
                ((0.0, 1.0), (1.0, 0.6))
            ),
            inventory_pressure_gamma_multiplier=PiecewiseLinearLut.from_pairs(
                ((0.0, 1.0), (1.0, 2.0))
            ),
        )
        provider = AdaptiveParameterProvider(
            runtime.market_state,
            tick_size=0.01,
            fallback=SymbolStrategyParameters(inventory_limit_lots=5),
            lookup_tables=lookup_tables,
        )

        values = provider.for_symbol("AAPL", inventory_lots=5).values
        self.assertGreaterEqual(values.gamma_inventory, 1.5)

        adaptive_state = provider.state_by_symbol["AAPL"]
        adaptive_state.ewma_toxicity_score = 0.5
        toxic_values = provider.for_symbol("AAPL", inventory_lots=0).values

        self.assertAlmostEqual(
            toxic_values.toxicity_width_multiplier,
            1.2,
            places=8,
        )
        self.assertAlmostEqual(
            toxic_values.toxicity_size_multiplier,
            0.8,
            places=8,
        )
        runtime.stop()

    def test_subscribe_all_books_repair_does_not_repeat_subscribe_all_every_check(
        self,
    ) -> None:
        trader = FakeTrader()
        session = build_shift_session(
            trader,
            username="tester",
            initiator_cfg=Path("initiator.cfg"),
        )
        self.assertTrue(session.connect("secret"))

        session.subscribe_symbols(("AAPL", "XOM"), subscribe_all_books=True)
        self.assertEqual(trader.subscribe_all_calls, 1)

        self.assertTrue(session.ensure_connected_and_subscribed("secret"))
        self.assertEqual(trader.subscribe_all_calls, 1)

    def test_reconciler_skips_terminal_zero_fill_audits_without_repolling_executions(
        self,
    ) -> None:
        trader = FakeTrader()
        portfolio = PortfolioLedger()
        ledger = OrderLedger()
        reconciler = Reconciler(
            trader,
            ledger,
            portfolio,
            ReconciliationConfig(),
        )
        audit = ledger.ensure_audit(
            order_id="dead-order",
            symbol="AAPL",
            side=OrderSide.BID,
            liquidity=OrderLiquidity.LIMIT,
        )
        audit.current_status = "canceled"
        audit.last_update_ts_ns = 1

        reconciler.poll_server_state()

        self.assertEqual(trader.execution_poll_calls, 0)

    def test_market_making_feature_batch_builds_snapshot_and_quote_gates(self) -> None:
        market_state = MarketState()
        aapl_state = market_state.ensure_symbol("AAPL")
        aapl_state.best_price = BestPriceSnapshot(
            symbol="AAPL",
            best_bid_px=100.0,
            best_bid_sz=4,
            best_ask_px=100.02,
            best_ask_sz=6,
            global_bid_px=100.0,
            global_bid_sz=4,
            global_ask_px=100.02,
            global_ask_sz=6,
            local_bid_px=100.0,
            local_bid_sz=3,
            local_ask_px=100.02,
            local_ask_sz=5,
            update_ts_ns=1_000,
        )
        aapl_state.local_bids = [BookLevel(price=100.0, size=3)]
        aapl_state.local_asks = [BookLevel(price=100.02, size=5)]
        aapl_state.local_multi_level_voi = 0.25
        aapl_state.global_l1_voi = 0.10
        aapl_state.last_book_update_ns = 900_000_000

        stale_state = market_state.ensure_symbol("STALE")
        stale_state.best_price = BestPriceSnapshot(
            symbol="STALE",
            best_bid_px=50.0,
            best_bid_sz=1,
            best_ask_px=50.01,
            best_ask_sz=1,
            global_bid_px=50.0,
            global_bid_sz=1,
            global_ask_px=50.01,
            global_ask_sz=1,
            update_ts_ns=1_000,
        )
        stale_state.local_bids = [BookLevel(price=50.0, size=1)]
        stale_state.local_asks = [BookLevel(price=50.01, size=1)]
        stale_state.last_book_update_ns = 100_000_000

        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=900_000_000,
        )

        batch = build_market_making_feature_batch(
            market_state,
            portfolio_ledger,
            now_ns=1_000_000_000,
            tick_size=0.01,
        )

        self.assertEqual(batch.symbols, ("AAPL", "STALE"))
        self.assertEqual(tuple(batch.inventory_lots), (2, 0))
        self.assertAlmostEqual(batch.rows[0].local_microprice, 100.0075, places=8)
        self.assertAlmostEqual(batch.rows[0].local_depth_imbalance, -0.25, places=8)
        self.assertAlmostEqual(batch.rows[0].local_multi_level_voi, 0.25, places=8)

        gates = batch.compute_quote_gates(
            max_staleness_ms=(250, 250),
            max_spread_ticks=(8, 8),
        )

        self.assertTrue(gates[0].allow_quotes)
        self.assertEqual(gates[0].reason, "quoting_enabled")
        self.assertFalse(gates[1].allow_quotes)
        self.assertEqual(gates[1].reason, "stale_book")


if __name__ == "__main__":
    unittest.main()
