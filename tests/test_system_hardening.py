from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from datetime import time as LocalTime
from pathlib import Path

from src.app.dashboard import TerminalDashboard, TerminalDashboardConfig
from scripts.profile_fake_load import run_profile
from src.app.replay import ReplayFrame, run_strategy_replay
from src.core.config import MarketDataConfig, RiskConfig, RuntimeConfig, StrategyConfig, TelemetryConfig
from src.data.featurespace.lut import PiecewiseLinearLut
from src.data.featurespace.online_stats import CusumDrift, DecayingWelford, PageHinkleyDrift
from src.data.state import BestPriceSnapshot, BookLevel, MarketState, SymbolState
from src.execution.order_router import OrderRouter
from src.execution.order_state import (
    FillRecord,
    OrderCommand,
    OrderIntentAction,
    OrderLedger,
    OrderLiquidity,
    OrderSide,
)
from src.risk.inventory import PortfolioLedger
from src.risk.kill_switch import (
    KillSwitchController,
    ReconciliationHealth,
    SafeMode,
    SafeModeConfig,
)
from src.risk.limits import RiskLimits, RiskLimitsConfig
from src.strategy.market_maker import TopOfBookMarketMaker, TopOfBookMarketMakerConfig
from src.strategy.base import StrategyDecisionTrace, StrategyDiagnostics
from src.strategy.params import (
    AdaptiveParameterProvider,
    StaticParameterProvider,
    SymbolStrategyParameters,
)
from src.telemetry.dry_run_validator import validate_dry_run_log
from src.telemetry.dry_run_summary import summarize_dry_run_log
from src.telemetry.ladder_calibration import calibrate_ladder_from_log
from src.telemetry.logger import JsonlEventLogger
from src.telemetry.metrics import build_session_metrics
from src.telemetry.recorder import build_session_telemetry


def _build_market_state() -> MarketState:
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
            global_bid_sz=7,
            global_ask_px=100.02,
            global_ask_sz=4,
            local_bid_px=100.0,
            local_bid_sz=3,
            local_ask_px=100.02,
            local_ask_sz=6,
            update_ts_ns=1_000,
        ),
        local_bids=[
            BookLevel(price=100.0, size=3),
            BookLevel(price=99.99, size=4),
            BookLevel(price=99.98, size=5),
        ],
        local_asks=[
            BookLevel(price=100.02, size=6),
            BookLevel(price=100.03, size=4),
            BookLevel(price=100.04, size=2),
        ],
        global_bids=[BookLevel(price=100.0, size=7)],
        global_asks=[BookLevel(price=100.02, size=4)],
        last_book_update_ns=1_000,
    )
    return market_state


class RuntimeConfigValidationTest(unittest.TestCase):
    def test_runtime_config_rejects_invalid_risk_and_session_bounds(self) -> None:
        config = RuntimeConfig(
            username="tester",
            password="secret",
            market_data=MarketDataConfig(symbols=("AAPL",), update_interval_ms=10),
            telemetry=TelemetryConfig(logger_flush_every=1, logger_max_queue_size=8),
            risk=RiskConfig(
                max_position_lots_per_symbol=5,
                max_gross_position_lots=4,
                stale_book_after_ms=100,
            ),
            strategy=StrategyConfig(
                session_open_local=LocalTime(16, 0),
                session_close_local=LocalTime(9, 30),
            ),
        )

        with self.assertRaises(ValueError):
            config.validate()

    def test_runtime_config_summary_redacts_password_and_reports_effective_settings(self) -> None:
        config = RuntimeConfig(
            username="tester",
            password="secret",
            market_data=MarketDataConfig(symbols=("AAPL", "XOM"), update_interval_ms=20),
            telemetry=TelemetryConfig(logger_flush_every=8, logger_max_queue_size=1024),
            risk=RiskConfig(max_position_lots_per_symbol=5, max_gross_position_lots=10),
            strategy=StrategyConfig(min_trades_per_day=300),
        )

        summary = config.summary()

        self.assertNotIn("password", summary)
        self.assertEqual(summary["market_data"]["symbols"], ("AAPL", "XOM"))
        self.assertEqual(summary["market_data"]["update_interval_ms"], 20)
        self.assertEqual(summary["strategy"]["min_trades_per_day"], 300)
        self.assertEqual(summary["strategy"]["target_trades_per_day"], 300)

    def test_strategy_config_accepts_legacy_target_trades_alias_as_min_trade_floor(self) -> None:
        config = StrategyConfig(target_trades_per_day=250)

        self.assertEqual(config.min_trades_per_day, 250)
        self.assertEqual(config.target_trades_per_day, 250)


class StateAndLedgerInvariantTest(unittest.TestCase):
    def test_market_state_clone_is_deep_copy(self) -> None:
        original = _build_market_state()
        clone = original.clone()

        clone.by_symbol["AAPL"].best_price.best_bid_px = 99.5
        clone.by_symbol["AAPL"].local_bids[0].size = 99

        self.assertAlmostEqual(
            original.by_symbol["AAPL"].best_price.best_bid_px,
            100.0,
            places=8,
        )
        self.assertEqual(original.by_symbol["AAPL"].local_bids[0].size, 3)

    def test_order_archive_preserves_symbol_net_position_and_session_totals(self) -> None:
        order_ledger = OrderLedger(max_completed_audits_retained=1, min_completed_audit_retention_ns=0)
        first_audit = order_ledger.ensure_audit(
            order_id="bid-fill",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_size=2,
            liquidity=OrderLiquidity.LIMIT,
        )
        first_audit.current_status = "filled"
        first_audit.last_update_ts_ns = 1
        order_ledger.append_fill(
            FillRecord(
                order_id="bid-fill",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=2,
                executed_price=100.0,
                status="filled",
                event_ts_ns=2,
                execution_index=0,
            )
        )
        second_audit = order_ledger.ensure_audit(
            order_id="ask-fill",
            symbol="AAPL",
            side=OrderSide.ASK,
            submit_size=1,
            liquidity=OrderLiquidity.MARKET,
        )
        second_audit.current_status = "filled"
        second_audit.last_update_ts_ns = 3
        order_ledger.append_fill(
            FillRecord(
                order_id="ask-fill",
                symbol="AAPL",
                side=OrderSide.ASK,
                executed_size=1,
                executed_price=100.02,
                status="filled",
                event_ts_ns=4,
                execution_index=0,
            )
        )

        order_ledger.archive_completed_audits(now_ns=10)

        self.assertEqual(order_ledger.total_fill_count(), 2)
        self.assertEqual(order_ledger.total_executed_shares(), 300)
        self.assertEqual(order_ledger.get_net_executed_lots("AAPL"), 1)
        self.assertAlmostEqual(order_ledger.estimated_total_rebate, 0.40, places=8)
        self.assertAlmostEqual(order_ledger.estimated_total_fee, 0.30, places=8)
        self.assertAlmostEqual(order_ledger.session_fill_vwap, 100.0066666667, places=8)
        self.assertAlmostEqual(order_ledger.session_fill_twap, 100.01, places=8)

    def test_order_audit_tracks_realized_slippage_and_shortfall(self) -> None:
        order_ledger = OrderLedger()
        audit = order_ledger.ensure_audit(
            order_id="bid-1",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_price=100.01,
            submit_size=1,
            decision_price=100.01,
            arrival_bid_px=100.0,
            arrival_ask_px=100.02,
            liquidity=OrderLiquidity.LIMIT,
        )

        order_ledger.append_fill(
            FillRecord(
                order_id="bid-1",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=1,
                executed_price=100.02,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )
        metrics = build_session_metrics(order_ledger)

        self.assertGreater(audit.realized_arrival_slippage_ticks, 0.0)
        self.assertGreater(audit.realized_decision_slippage_ticks, 0.0)
        self.assertAlmostEqual(audit.arrival_implementation_shortfall, 1.0, places=8)
        self.assertAlmostEqual(metrics.arrival_shortfall, 1.0, places=8)
        self.assertAlmostEqual(metrics.decision_shortfall, 1.0, places=8)

    def test_order_audit_slippage_ticks_use_ledger_tick_size(self) -> None:
        order_ledger = OrderLedger(tick_size=0.05)
        audit = order_ledger.ensure_audit(
            order_id="bid-5c",
            symbol="AAPL",
            side=OrderSide.BID,
            submit_price=100.0,
            submit_size=1,
            decision_price=100.0,
            arrival_bid_px=99.95,
            arrival_ask_px=100.05,
            liquidity=OrderLiquidity.LIMIT,
        )

        order_ledger.append_fill(
            FillRecord(
                order_id="bid-5c",
                symbol="AAPL",
                side=OrderSide.BID,
                executed_size=1,
                executed_price=100.05,
                status="filled",
                event_ts_ns=1,
                execution_index=0,
            )
        )

        self.assertAlmostEqual(audit.realized_arrival_slippage_ticks, 1.0, places=8)
        self.assertAlmostEqual(audit.realized_decision_slippage_ticks, 1.0, places=8)


class OnlineStatsTest(unittest.TestCase):
    def test_decaying_welford_zscore_and_drift_detectors(self) -> None:
        welford = DecayingWelford(decay=0.8)
        for value in (1.0, 1.0, 1.0, 2.0):
            welford.update(value)

        self.assertGreater(welford.std, 0.0)
        self.assertGreater(welford.zscore(3.0), 0.0)

        cusum = CusumDrift(threshold=1.0)
        cusum_signals = [cusum.update(value) for value in (0.1, 0.2, 0.8, 0.9)]
        self.assertIn(1, cusum_signals)

        page_hinkley = PageHinkleyDrift(threshold=0.4, alpha=0.5)
        ph_signals = [page_hinkley.update(value) for value in (0.0, 0.0, 0.8, 0.9)]
        self.assertIn(1, ph_signals)


class StrategyInvariantTest(unittest.TestCase):
    def test_market_maker_outputs_cross_free_quotes_and_one_sided_flatten(self) -> None:
        market_state = _build_market_state()
        parameter_provider = StaticParameterProvider(
            default=SymbolStrategyParameters(
                quote_size_lots=2,
                spread_floor_ticks=1,
                spread_ceiling_ticks=4,
                max_live_spread_ticks=10,
                symbol_enable_flag=True,
                bid_enable_flag=True,
                ask_enable_flag=True,
                flatten_only_mode=True,
                inventory_limit_lots=5,
            )
        )
        strategy = TopOfBookMarketMaker(
            market_state,
            config=TopOfBookMarketMakerConfig(tick_size=0.01),
            parameter_provider=parameter_provider,
        )
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=100.0,
            short_price=0.0,
            realized_pl=0.0,
            ts_ns=1,
        )

        targets, _ = strategy.generate_targets(portfolio_ledger=portfolio_ledger)
        target = targets[0]

        self.assertGreater(target.ask_px, target.bid_px)
        self.assertAlmostEqual((target.bid_px / 0.01) % 1.0, 0.0, places=8)
        self.assertAlmostEqual((target.ask_px / 0.01) % 1.0, 0.0, places=8)
        self.assertTrue(target.flatten_mode)
        self.assertFalse(target.enable_bid)
        self.assertTrue(target.enable_ask)
        self.assertEqual(target.bid_size > 0, target.enable_bid)
        self.assertEqual(target.ask_size > 0, target.enable_ask)
        self.assertEqual(target.ask_size, 2)

    def test_adaptive_provider_keeps_local_depth_signals_separate_from_global_l1_reference(self) -> None:
        market_state = MarketState()
        market_state.by_symbol["AAPL"] = SymbolState(
            symbol="AAPL",
            best_price=BestPriceSnapshot(
                symbol="AAPL",
                best_bid_px=100.00,
                best_bid_sz=10,
                best_ask_px=100.02,
                best_ask_sz=10,
                global_bid_px=100.00,
                global_bid_sz=2,
                global_ask_px=100.02,
                global_ask_sz=8,
                local_bid_px=100.00,
                local_bid_sz=9,
                local_ask_px=100.02,
                local_ask_sz=1,
                update_ts_ns=1_000,
            ),
            local_bids=[
                BookLevel(price=100.00, size=9),
                BookLevel(price=99.99, size=6),
                BookLevel(price=99.98, size=4),
            ],
            local_asks=[
                BookLevel(price=100.02, size=1),
                BookLevel(price=100.03, size=1),
                BookLevel(price=100.04, size=1),
            ],
            global_bids=[BookLevel(price=100.00, size=2)],
            global_asks=[BookLevel(price=100.02, size=8)],
            last_book_update_ns=1_000,
        )
        provider = AdaptiveParameterProvider(
            market_state=market_state,
            tick_size=0.01,
        )

        values = provider.for_symbol("AAPL").values
        state = market_state.by_symbol["AAPL"]

        self.assertGreater(values.local_depth_imbalance, 0.0)
        self.assertLess(values.global_l1_imbalance, 0.0)
        self.assertGreater(values.local_microprice, state.mid_price)
        self.assertEqual(values.bid_25pct_levels, 1.0)
        self.assertGreaterEqual(values.ask_25pct_levels, 1.0)


class SafeModeDrillTest(unittest.TestCase):
    def test_kill_switch_drill_escalates_and_flushes_transition_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            telemetry = build_session_telemetry(Path(tmpdir), flush_every=1, max_queue_size=32)
            telemetry.start()
            try:
                kill_switch = KillSwitchController(
                    SafeModeConfig(
                        max_waiting_list_staleness_ms=100,
                        max_portfolio_staleness_ms=100,
                        max_position_mismatch_lots=1,
                        max_degraded_duration_ms=100,
                    ),
                    event_logger=telemetry.event_logger,
                )

                degraded = ReconciliationHealth(
                    waiting_list_stale_ms=500,
                    portfolio_stale_ms=0,
                    position_mismatch_lots=0,
                    broker_connected=True,
                )
                disconnected = ReconciliationHealth(
                    waiting_list_stale_ms=0,
                    portfolio_stale_ms=0,
                    position_mismatch_lots=0,
                    broker_connected=False,
                )

                self.assertEqual(
                    kill_switch.update(degraded, now_ns=1_000_000_000),
                    SafeMode.DEGRADED_RECONCILE,
                )
                self.assertEqual(
                    kill_switch.update(degraded, now_ns=1_200_000_000),
                    SafeMode.FLATTEN_ONLY,
                )
                self.assertEqual(
                    kill_switch.update(disconnected, now_ns=1_300_000_000),
                    SafeMode.KILL_SWITCH,
                )

                self.assertTrue(
                    kill_switch.blocks(
                        OrderIntentAction.SUBMIT,
                        reduces_risk=False,
                    )
                )
                self.assertFalse(kill_switch.blocks(OrderIntentAction.CANCEL))
                self.assertFalse(kill_switch.blocks(OrderIntentAction.FLATTEN))
            finally:
                telemetry.stop()

            events = [
                json.loads(line)
                for line in (Path(tmpdir) / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            transitions = [
                event
                for event in events
                if event.get("kind") == "safe_mode_transition"
            ]

            self.assertEqual(
                [event["payload"]["next_mode"] for event in transitions],
                [
                    SafeMode.DEGRADED_RECONCILE.value,
                    SafeMode.FLATTEN_ONLY.value,
                    SafeMode.KILL_SWITCH.value,
                ],
            )


class TelemetryBackpressureTest(unittest.TestCase):
    def test_jsonl_logger_drops_events_instead_of_blocking_when_queue_is_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = JsonlEventLogger(
                Path(tmpdir) / "events.jsonl",
                flush_every=1024,
                max_queue_size=4,
            )

            started_s = time.perf_counter()
            for idx in range(500):
                logger.log(
                    "stress_event",
                    idx=idx,
                    payload={
                        "symbol": "AAPL",
                        "values": [idx, idx + 1, idx + 2],
                        "nested": {"a": idx, "b": str(idx)},
                    },
                )
            elapsed_s = time.perf_counter() - started_s

            self.assertGreater(logger.dropped_events, 0)
            self.assertLess(elapsed_s, 0.5)

    def test_market_maker_never_crosses_quotes_across_inventory_grid(self) -> None:
        market_state = _build_market_state()
        strategy = TopOfBookMarketMaker(
            market_state,
            config=TopOfBookMarketMakerConfig(tick_size=0.01),
            parameter_provider=StaticParameterProvider(
                default=SymbolStrategyParameters(
                    spread_floor_ticks=1,
                    spread_ceiling_ticks=8,
                    max_live_spread_ticks=20,
                    quote_size_lots=2,
                    symbol_enable_flag=True,
                    bid_enable_flag=True,
                    ask_enable_flag=True,
                    inventory_limit_lots=5,
                    cj_inventory_skew_ticks_per_lot=1.5,
                    glft_half_width_ticks=1.0,
                )
            ),
        )

        for inventory_lots in range(-5, 6):
            portfolio_ledger = PortfolioLedger()
            if inventory_lots > 0:
                portfolio_ledger.update_position(
                    "AAPL",
                    long_shares=inventory_lots * 100,
                    short_shares=0,
                    long_price=100.0,
                    short_price=0.0,
                    realized_pl=0.0,
                    ts_ns=1,
                )
            elif inventory_lots < 0:
                portfolio_ledger.update_position(
                    "AAPL",
                    long_shares=0,
                    short_shares=abs(inventory_lots) * 100,
                    long_price=0.0,
                    short_price=100.0,
                    realized_pl=0.0,
                    ts_ns=1,
                )
            targets, _ = strategy.generate_targets(portfolio_ledger=portfolio_ledger)
            self.assertGreater(targets[0].ask_px, targets[0].bid_px)
            self.assertAlmostEqual((targets[0].bid_px / 0.01) % 1.0, 0.0, places=8)
            self.assertAlmostEqual((targets[0].ask_px / 0.01) % 1.0, 0.0, places=8)
            self.assertEqual(targets[0].bid_size > 0, targets[0].enable_bid)
            self.assertEqual(targets[0].ask_size > 0, targets[0].enable_ask)

    def test_market_maker_suppresses_wide_spread_and_invalid_touch_books(self) -> None:
        market_state = _build_market_state()
        state = market_state.by_symbol["AAPL"]
        now_ns = time.monotonic_ns()
        state.last_book_update_ns = now_ns
        state.best_price.update_ts_ns = now_ns

        strategy = TopOfBookMarketMaker(
            market_state,
            config=TopOfBookMarketMakerConfig(
                tick_size=0.01,
                default_parameters=SymbolStrategyParameters(
                    quote_age_limit_ms=10_000,
                    max_live_spread_ticks=4,
                    symbol_enable_flag=True,
                    bid_enable_flag=True,
                    ask_enable_flag=True,
                ),
            ),
            parameter_provider=StaticParameterProvider(
                default=SymbolStrategyParameters(
                    quote_age_limit_ms=10_000,
                    max_live_spread_ticks=4,
                    symbol_enable_flag=True,
                    bid_enable_flag=True,
                    ask_enable_flag=True,
                )
            ),
        )

        state.best_price.best_bid_px = 100.0
        state.best_price.best_ask_px = 100.20
        wide_targets, wide_traces = strategy.generate_targets(
            portfolio_ledger=PortfolioLedger()
        )

        self.assertFalse(wide_targets[0].enable_bid)
        self.assertFalse(wide_targets[0].enable_ask)
        self.assertEqual(wide_targets[0].bid_size, 0)
        self.assertEqual(wide_targets[0].ask_size, 0)
        self.assertEqual(wide_traces[0].decision_reason, "spread_too_wide")

        state.best_price.best_bid_px = 100.0
        state.best_price.best_ask_px = 99.99
        invalid_targets, invalid_traces = strategy.generate_targets(
            portfolio_ledger=PortfolioLedger()
        )

        self.assertFalse(invalid_targets[0].enable_bid)
        self.assertFalse(invalid_targets[0].enable_ask)
        self.assertEqual(invalid_targets[0].bid_size, 0)
        self.assertEqual(invalid_targets[0].ask_size, 0)
        self.assertEqual(
            invalid_traces[0].decision_reason,
            "locked_or_invalid_spread",
        )

    def test_lookup_table_outputs_are_monotone_and_bounded(self) -> None:
        width_lut = PiecewiseLinearLut.from_pairs(
            (
                (0.0, 1.0),
                (0.2, 1.08),
                (0.4, 1.18),
                (0.7, 1.35),
                (1.0, 1.50),
            )
        )
        size_lut = PiecewiseLinearLut.from_pairs(
            (
                (0.0, 1.0),
                (0.2, 0.94),
                (0.4, 0.84),
                (0.7, 0.66),
                (1.0, 0.50),
            )
        )

        xs = [idx / 20.0 for idx in range(21)]
        width_values = [width_lut.evaluate(x) for x in xs]
        size_values = [size_lut.evaluate(x) for x in xs]

        self.assertTrue(all(1.0 <= value <= 1.5 for value in width_values))
        self.assertTrue(all(0.5 <= value <= 1.0 for value in size_values))
        self.assertTrue(
            all(right >= left for left, right in zip(width_values, width_values[1:]))
        )
        self.assertTrue(
            all(right <= left for left, right in zip(size_values, size_values[1:]))
        )

    def test_replay_harness_replays_frames_without_mutating_input_snapshots(self) -> None:
        frame1 = ReplayFrame(market_state=_build_market_state())
        frame2_state = _build_market_state()
        frame2_state.by_symbol["AAPL"].best_price.best_bid_px = 100.03
        frame2_state.by_symbol["AAPL"].best_price.best_ask_px = 100.05
        frame2 = ReplayFrame(market_state=frame2_state)
        strategy = TopOfBookMarketMaker(
            frame1.market_state.clone(),
            config=TopOfBookMarketMakerConfig(tick_size=0.01),
            parameter_provider=StaticParameterProvider(),
        )

        replay_results = run_strategy_replay(strategy, [frame1, frame2])

        self.assertEqual(len(replay_results), 2)
        self.assertGreater(
            replay_results[1].traces[0].diagnostics.fair_value_center,
            replay_results[0].traces[0].diagnostics.fair_value_center,
        )
        self.assertAlmostEqual(
            frame1.market_state.by_symbol["AAPL"].best_price.best_bid_px,
            100.0,
            places=8,
        )

    def test_adaptive_provider_exposes_expected_slippage_features(self) -> None:
        provider = AdaptiveParameterProvider(
            market_state=_build_market_state(),
            tick_size=0.01,
        )

        values = provider.for_symbol("AAPL").values

        self.assertGreater(values.expected_aggressive_slippage_ticks, 0.0)
        self.assertGreaterEqual(
            values.expected_aggressive_slippage_ticks,
            values.expected_passive_slippage_ticks,
        )
        self.assertGreater(values.slippage_quality_score, 1.0)


class _FailingOrderFactory:
    def build_limit_order(self, symbol: str, side: OrderSide, size: int, price: float):
        return type("Order", (), {"id": f"{symbol}-{side.value}-{size}-{price}"})()

    def build_market_order(self, symbol: str, side: OrderSide, size: int):
        return type("Order", (), {"id": f"{symbol}-{side.value}-{size}-mkt"})()

    def build_cancel_order(self, live_order):
        return type("Order", (), {"id": live_order.order_id})()


class _FailingTrader:
    def submit_order(self, order) -> None:
        del order
        raise RuntimeError("submit failed")

    def submit_cancellation(self, order) -> None:
        del order
        raise RuntimeError("cancel failed")


class FailureInjectionTest(unittest.TestCase):
    def test_router_does_not_mutate_ledger_on_submit_failure(self) -> None:
        order_ledger = OrderLedger()
        router = OrderRouter(
            _FailingTrader(),
            _FailingOrderFactory(),
            order_ledger,
            risk_limits=RiskLimits(
                RiskLimitsConfig(max_position_lots_per_symbol=5, max_gross_position_lots=10),
                portfolio_ledger=PortfolioLedger(),
                order_ledger=order_ledger,
            ),
        )

        router.apply(
            OrderCommand(
                action=OrderIntentAction.SUBMIT,
                symbol="AAPL",
                side=OrderSide.BID,
                price=100.0,
                size=1,
                reason="failure_injection",
            )
        )

        self.assertFalse(order_ledger.live_orders())
        self.assertEqual(order_ledger.audits_by_order_id, {})


class DryRunTelemetryValidationTest(unittest.TestCase):
    def test_validate_dry_run_log_passes_for_cross_free_tick_aligned_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = Path(tmpdir) / "events.jsonl"
            _write_dry_run_events(
                event_path,
                bid_px=100.0,
                ask_px=100.02,
                bid_size=1,
                ask_size=0,
                enable_bid=True,
                enable_ask=False,
            )

            report = validate_dry_run_log(event_path, tick_size=0.01)

            self.assertTrue(report.passed, report.as_text())

    def test_validate_dry_run_log_rejects_crossed_or_disabled_nonzero_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = Path(tmpdir) / "events.jsonl"
            _write_dry_run_events(
                event_path,
                bid_px=100.02,
                ask_px=100.00,
                bid_size=1,
                ask_size=1,
                enable_bid=False,
                enable_ask=False,
            )

            report = validate_dry_run_log(event_path, tick_size=0.01)

            self.assertFalse(report.passed)
            failures = {check.name for check in report.checks if not check.passed}
            self.assertIn("strategy_targets_cross_free", failures)
            self.assertIn("disabled_sides_have_zero_size", failures)

    def test_summarize_dry_run_log_reports_feature_ranges_and_sign_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = Path(tmpdir) / "events.jsonl"
            _write_dry_run_events(
                event_path,
                bid_px=100.0,
                ask_px=100.02,
                bid_size=1,
                ask_size=1,
                enable_bid=True,
                enable_ask=True,
            )

            report = summarize_dry_run_log(event_path)
            text = report.as_text()

            self.assertEqual(report.event_counts["strategy_target"], 1)
            self.assertEqual(report.target_count, 1)
            self.assertEqual(report.two_sided_target_count, 1)
            self.assertEqual(report.alpha_bias_local_agreement_count, 1)
            self.assertEqual(report.drift_bias_global_agreement_count, 1)
            self.assertIn("queue_fill_support:", text)
            self.assertIn("sign_agreement=", text)

    def test_calibrate_ladder_from_log_reports_per_level_fill_and_slippage_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            event_path = Path(tmpdir) / "events.jsonl"
            events = [
                {
                    "kind": "strategy_trace",
                    "ts_ns": 1,
                    "payload": {
                        "quote_target": {
                            "symbol": "AAPL",
                            "bid_levels": [
                                {
                                    "level_index": 0,
                                    "price": 100.0,
                                    "size": 1,
                                    "queue_share": 0.20,
                                    "enabled": True,
                                },
                                {
                                    "level_index": 1,
                                    "price": 99.99,
                                    "size": 2,
                                    "queue_share": 0.10,
                                    "enabled": True,
                                },
                            ],
                            "ask_levels": [],
                        },
                    },
                },
                {
                    "kind": "order_fill",
                    "ts_ns": 2,
                    "payload": {
                        "level_index": 1,
                        "liquidity": "limit",
                        "fill": {
                            "order_id": "order-1",
                            "symbol": "AAPL",
                            "side": "bid",
                            "executed_size": 2,
                            "executed_price": 99.99,
                            "status": "filled",
                        },
                        "slippage": {
                            "realized_arrival_slippage_ticks": -0.2,
                            "realized_decision_slippage_ticks": -0.1,
                            "arrival_implementation_shortfall": -4.0,
                            "decision_implementation_shortfall": -2.0,
                        },
                    },
                },
            ]
            event_path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )

            report = calibrate_ladder_from_log(event_path)
            text = report.as_text()
            level_1_stats = report.stats_by_key[("AAPL", "bid", 1)]

            self.assertEqual(level_1_stats.quote_samples, 1)
            self.assertEqual(level_1_stats.enabled_samples, 1)
            self.assertEqual(level_1_stats.fill_count, 1)
            self.assertEqual(level_1_stats.fill_size_lots, 2)
            self.assertAlmostEqual(level_1_stats.avg_size, 2.0, places=8)
            self.assertAlmostEqual(level_1_stats.avg_queue_share, 0.10, places=8)
            self.assertAlmostEqual(
                level_1_stats.avg_arrival_slip_ticks,
                -0.2,
                places=8,
            )
            self.assertIn("SYMBOL SIDE LVL", text)
            self.assertIn("AAPL", text)


class FakeLoadProfileTest(unittest.TestCase):
    def test_profile_fake_load_returns_bounded_latency_and_memory_metrics(self) -> None:
        profile = run_profile(
            symbols=("AAPL", "XOM"),
            cycles=50,
            update_interval_ms=50,
        )

        self.assertEqual(profile["symbols"], ["AAPL", "XOM"])
        self.assertEqual(profile["cycles"], 50)
        self.assertEqual(profile["update_interval_ms"], 50)
        self.assertGreater(profile["latency_us_mean"], 0.0)
        self.assertGreater(profile["latency_us_p50"], 0.0)
        self.assertGreater(profile["latency_us_p95"], 0.0)
        self.assertGreater(profile["latency_us_max"], 0.0)
        self.assertGreater(profile["peak_memory_kb"], 0.0)
        self.assertLess(profile["latency_us_mean"], 50_000.0)
        self.assertLess(profile["latency_us_p95"], 50_000.0)


class TerminalDashboardTest(unittest.TestCase):
    def test_terminal_dashboard_renders_positions_and_percent_markout(self) -> None:
        market_state = _build_market_state()
        portfolio_ledger = PortfolioLedger()
        portfolio_ledger.update_position(
            "AAPL",
            long_shares=200,
            short_shares=0,
            long_price=99.90,
            short_price=0.0,
            realized_pl=12.5,
            ts_ns=1_000,
        )
        portfolio_ledger.summary.total_bp = 500_000.0
        portfolio_ledger.summary.total_shares = 200
        portfolio_ledger.summary.total_realized_pl = 12.5
        stream = io.StringIO()
        dashboard = TerminalDashboard(
            config=TerminalDashboardConfig(
                enabled=True,
                redraw_min_interval_ms=0,
                stream=stream,
            )
        )
        trace = StrategyDecisionTrace(
            symbol="AAPL",
            strategy_name="top_of_book_market_maker",
            diagnostics=StrategyDiagnostics(
                allocation_weight=1.2,
                pace_multiplier=1.1,
                participation_mode="passive_two_sided",
                extra={"toxicity_markout_pct": 0.42},
            ),
        )

        dashboard.render(
            market_state=market_state,
            portfolio_ledger=portfolio_ledger,
            traces=(trace,),
            session_metrics=build_session_metrics(OrderLedger()),
        )

        output = stream.getvalue()
        self.assertIn("ACCOUNT", output)
        self.assertIn("SYMBOL", output)
        self.assertIn("AAPL", output)
        self.assertIn("POSL", output)
        self.assertIn("VPOSL", output)
        self.assertIn("MID", output)
        self.assertIn("RealPnL", output)
        self.assertIn("UnrealPnL", output)
        self.assertIn("TotalPnL", output)
        self.assertIn("PnL30s", output)
        self.assertIn("PnL300s", output)
        self.assertIn("0.420", output)
        self.assertIn("       2", output)
        self.assertIn("   0.00%", output)


def _write_dry_run_events(
    event_path: Path,
    *,
    bid_px: float,
    ask_px: float,
    bid_size: int,
    ask_size: int,
    enable_bid: bool,
    enable_ask: bool,
) -> None:
    events = [
        {"kind": "app_started", "ts_ns": 1, "payload": {"session_dir": str(event_path.parent)}},
        {"kind": "bootstrap_complete", "ts_ns": 2, "payload": {"symbols": ["AAPL"]}},
        {
            "kind": "strategy_trace",
            "ts_ns": 3,
            "payload": {
                "trace": {
                    "symbol": "AAPL",
                    "diagnostics": {
                        "fair_value_anchor": 100.01,
                        "fair_value_center": 100.01,
                        "quote_width": 0.02,
                        "inventory_skew": 0.0,
                        "alpha_bias": 0.01,
                        "toxicity_score": 0.0,
                        "allocation_weight": 1.0,
                        "pace_multiplier": 1.0,
                        "extra": {
                            "spread_mean_ticks": 2.0,
                            "spread_std_ticks": 0.1,
                            "spread_p90_ticks": 2.0,
                            "spread_zscore": 0.0,
                            "local_depth_imbalance": 0.5,
                            "global_l1_imbalance": 0.2,
                            "global_mid_drift": 0.0002,
                            "global_drift_shift_ticks": 0.2,
                            "drift_inventory_bias_ticks": 0.1,
                            "local_imbalance_zscore": 0.2,
                            "global_drift_zscore": 0.1,
                            "quote_age_mean_ms": 0.0,
                            "quote_age_p90_ms": 0.0,
                            "queue_fill_support": 0.0,
                            "bid_queue_share": 0.0,
                            "ask_queue_share": 0.0,
                            "toxicity_score": 0.0,
                            "toxicity_width_multiplier": 1.0,
                            "toxicity_size_multiplier": 1.0,
                            "slippage_quality_score": 1.0,
                            "passive_fill_probability": 0.4,
                        },
                    },
                },
            },
        },
        {
            "kind": "strategy_target",
            "ts_ns": 4,
            "payload": {
                "target": {
                    "symbol": "AAPL",
                    "bid_px": bid_px,
                    "ask_px": ask_px,
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                    "enable_bid": enable_bid,
                    "enable_ask": enable_ask,
                    "flatten_mode": True,
                },
            },
        },
        {"kind": "strategy_cycle_complete", "ts_ns": 5, "payload": {"targets": 1}},
        {
            "kind": "session_metrics",
            "ts_ns": 6,
            "payload": {
                "metrics": {
                    "estimated_fees": 0.0,
                    "estimated_rebates": 0.0,
                    "estimated_net_fees": 0.0,
                    "fill_vwap": 0.0,
                    "fill_twap": 0.0,
                    "arrival_shortfall": 0.0,
                    "decision_shortfall": 0.0,
                    "passive_fill_ratio": 0.0,
                },
            },
        },
        {"kind": "app_stopping", "ts_ns": 7, "payload": {"session_dir": str(event_path.parent)}},
    ]
    event_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
