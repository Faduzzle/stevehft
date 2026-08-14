from __future__ import annotations

import math
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Sequence, TextIO

from src.core.session_clock import SessionProgress
from src.core.units import NS_PER_MS, NS_PER_S
from src.data.state import MarketState, SymbolState
from src.risk.inventory import PortfolioLedger, Position
from src.strategy.base import StrategyDecisionTrace
from src.telemetry.metrics import SessionMetrics


@dataclass(slots=True)
class SymbolPnlSample:
    ts_ns: int
    total_pnl: float


@dataclass(slots=True)
class TerminalDashboardConfig:
    enabled: bool = False
    rolling_windows_s: tuple[int, ...] = (30, 300)
    redraw_min_interval_ms: int = 200
    stream: TextIO = sys.stdout


@dataclass(slots=True)
class TerminalDashboard:
    config: TerminalDashboardConfig = field(default_factory=TerminalDashboardConfig)
    _last_render_ns: int = 0
    _last_render_line_count: int = 0
    _initial_bp: float = 0.0
    _last_traces: list[StrategyDecisionTrace] = field(default_factory=list)
    _last_session_metrics: SessionMetrics = field(default_factory=SessionMetrics)
    _vpos_ewma_by_symbol: dict[str, float] = field(default_factory=dict)
    _pnl_history_by_symbol: dict[str, Deque[SymbolPnlSample]] = field(
        default_factory=lambda: defaultdict(deque)
    )

    def reset(self) -> None:
        self._last_render_ns = 0
        self._last_render_line_count = 0
        self._initial_bp = 0.0
        self._last_traces.clear()
        self._last_session_metrics = SessionMetrics()
        self._vpos_ewma_by_symbol.clear()
        self._pnl_history_by_symbol.clear()

    def render(
        self,
        *,
        market_state: MarketState | None,
        portfolio_ledger: PortfolioLedger | None,
        traces: Sequence[StrategyDecisionTrace],
        session_metrics: SessionMetrics,
        session_progress: SessionProgress | None = None,
    ) -> None:
        if not self.config.enabled or portfolio_ledger is None:
            return

        now_ns = time.monotonic_ns()
        min_interval_ns = max(self.config.redraw_min_interval_ms, 0) * NS_PER_MS
        if (
            min_interval_ns > 0
            and self._last_render_ns > 0
            and now_ns - self._last_render_ns < min_interval_ns
        ):
            return
        self._last_render_ns = now_ns

        if traces:
            self._last_traces = list(traces)
            self._last_session_metrics = session_metrics
        elif self._last_traces:
            traces = self._last_traces
            session_metrics = self._last_session_metrics

        trace_by_symbol = {trace.symbol: trace for trace in traces}
        lines = [
            self._format_session_line(session_progress, traces),
            *self._format_account_block(
                market_state,
                portfolio_ledger,
                traces,
                session_metrics,
            ),
            "",
            self._format_symbol_header(),
        ]

        symbols = sorted(
            set(market_state.by_symbol if market_state is not None else ())
            | set(portfolio_ledger.positions)
            | set(trace_by_symbol)
        )
        if not symbols:
            lines.append(
                "WAITING_FOR_BOOKS  "
                "market-data stream is connected but no symbol snapshots have been "
                "received yet"
            )
        for symbol in symbols:
            state = (
                market_state.by_symbol.get(symbol)
                if market_state is not None
                else None
            )
            position = portfolio_ledger.positions.get(symbol)
            trace = trace_by_symbol.get(symbol)
            lines.append(
                self._format_symbol_row(
                    symbol=symbol,
                    state=state,
                    position=position,
                    trace=trace,
                    now_ns=now_ns,
                )
            )

        stream = self.config.stream or sys.stdout
        if self._last_render_line_count <= 0:
            stream.write("\033[2J\033[H")
        else:
            stream.write("\033[H\033[J")
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        self._last_render_line_count = len(lines)

    @staticmethod
    def _format_session_line(
        session_progress: SessionProgress | None,
        traces: Sequence[StrategyDecisionTrace],
    ) -> str:
        if session_progress is None:
            return "SESSION  no clock"

        now = session_progress.now_local
        open_t = session_progress.session_open_local
        close_t = session_progress.session_close_local
        mins_to_close = session_progress.minutes_to_close
        mins_since_open = session_progress.minutes_since_open
        warmup_mins = session_progress.warmup_minutes

        now_str = now.strftime("%H:%M:%S")
        open_str = open_t.strftime("%H:%M")
        close_str = close_t.strftime("%H:%M")

        if not session_progress.is_session_open:
            state_str = "CLOSED"
        elif session_progress.in_warmup:
            warmup_elapsed = int(mins_since_open)
            state_str = f"WARMUP  {warmup_elapsed}/{warmup_mins}min"
        elif session_progress.flatten_only_mode:
            state_str = f"FLATTEN  {mins_to_close:.1f}min left"
        else:
            state_str = "OPEN"

        # Pace info from first trace
        pace_ratio = 0.0
        observed = 0
        expected = 0.0
        if traces:
            extra = traces[0].diagnostics.extra
            observed = int(extra.get("observed_trade_count", 0))
            expected = float(extra.get("expected_trade_count", 0.0))
            pace_ratio = observed / expected if expected > 1e-9 else 0.0

        return (
            f"SESSION  "
            f"Now={now_str}  "
            f"Open={open_str}  "
            f"Close={close_str}  "
            f"ToClose={mins_to_close:.1f}min  "
            f"Elapsed={mins_since_open:.1f}min  "
            f"State={state_str}  "
            f"Pace={pace_ratio:.2f}x ({observed}/{expected:.0f})"
        )

    def _format_account_block(
        self,
        market_state: MarketState | None,
        portfolio_ledger: PortfolioLedger,
        traces: Sequence[StrategyDecisionTrace],
        session_metrics: SessionMetrics,
    ) -> list[str]:
        summary = portfolio_ledger.summary
        if self._initial_bp <= 0.0 and summary.total_bp > 0.0:
            self._initial_bp = summary.total_bp
        total_abs_lots = sum(
            abs(position.net_lots)
            for position in portfolio_ledger.positions.values()
        )
        open_symbols = sum(
            1
            for position in portfolio_ledger.positions.values()
            if position.net_shares != 0
        )
        unrealized_pnl = sum(
            self._symbol_unrealized_pnl(
                position,
                (
                    market_state.by_symbol.get(symbol)
                    if market_state is not None
                    else None
                ),
            )
            for symbol, position in portfolio_ledger.positions.items()
        )
        total_pnl = summary.total_realized_pl + unrealized_pnl
        pnl_denominator = self._initial_bp if self._initial_bp > 0.0 else summary.total_bp
        total_pnl_pct = (
            100.0 * total_pnl / pnl_denominator
            if pnl_denominator > 0.0
            else 0.0
        )
        pace_ratio, observed_trades, expected_trades = self._account_pace_metrics(traces)
        avg_markout_pct = self._average_markout_pct(traces)
        markout_pnl = self._markout_pnl_estimate(
            market_state=market_state,
            portfolio_ledger=portfolio_ledger,
            traces=traces,
        )
        signal_pnl = -(
            session_metrics.decision_shortfall
            + session_metrics.estimated_net_fees
        )
        return [
            (
                f"{'ACCOUNT':<10}"
                f"{'BP':<6}{summary.total_bp:>14,.2f}  "
                f"{'PosLots':<8}{int(summary.total_shares / 100):>10,d}  "
                f"{'GrossLots':<11}{total_abs_lots:>8,d}  "
                f"{'OpenSyms':<9}{open_symbols:>6d}  "
                f"{'RealPnL':<9}{summary.total_realized_pl:>12,.2f}  "
                f"{'UnrealPnL':<10}{unrealized_pnl:>12,.2f}  "
                f"{'TotalPnL':<10}{total_pnl:>12,.2f}  "
                f"{'TotalPnL%':<10}{total_pnl_pct:>9.2f}%"
            ),
            (
                f"{'TRADING':<10}"
                f"{'Pace':<6}{pace_ratio:>8.2f}x  "
                f"{'Trades':<8}{observed_trades:>6d}/{expected_trades:>6.1f}  "
                f"{'Passive':<9}{session_metrics.passive_fill_ratio:>8.2%}  "
                f"{'Fees':<7}{session_metrics.estimated_net_fees:>10,.2f}  "
                f"{'SlipArr':<9}{session_metrics.arrival_shortfall:>10,.2f}  "
                f"{'SlipSig':<9}{session_metrics.decision_shortfall:>10,.2f}"
            ),
            self._format_strategy_header(),
            (
                self._format_strategy_row(
                    name="BASE_MM",
                    weight=self._average_extra(traces, "sleeve_weight_base_mm", 1.0),
                    signal=signal_pnl,
                    confidence=1.0,
                    active_count=len(traces),
                    pnl=self._strategy_sleeve_pnl(
                        sleeve_credit_key="sleeve_credit_base_mm",
                        market_state=market_state,
                        portfolio_ledger=portfolio_ledger,
                        traces=traces,
                    ),
                    detail=(
                        f"Markout%={avg_markout_pct:.3f}% "
                        f"Mode={self._account_mode(traces)}"
                    ),
                )
            ),
            *self._format_overlay_strategy_rows(
                traces,
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
            ),
        ]

    @staticmethod
    def _format_strategy_header() -> str:
        return (
            f"{'STRATEGY':<14}"
            f"{'WEIGHT':>8}"
            f"{'SIGNAL':>12}"
            f"{'CONF':>8}"
            f"{'ACTIVE':>8}"
            f"{'PnL':>12}  "
            "DETAIL"
        )

    @staticmethod
    def _format_strategy_row(
        *,
        name: str,
        weight: float,
        signal: float,
        confidence: float,
        active_count: int,
        pnl: float,
        detail: str,
    ) -> str:
        return (
            f"{name:<14}"
            f"{weight:>8.2f}"
            f"{signal:>12.3f}"
            f"{confidence:>8.3f}"
            f"{active_count:>8d}"
            f"{pnl:>12,.2f}  "
            f"{detail}"
        )

    def _format_overlay_strategy_rows(
        self,
        traces: Sequence[StrategyDecisionTrace],
        *,
        market_state: MarketState | None,
        portfolio_ledger: PortfolioLedger | None,
    ) -> list[str]:
        if not traces:
            return [
                self._format_strategy_row(
                    name=name,
                    weight=0.0,
                    signal=0.0,
                    confidence=0.0,
                    active_count=0,
                    pnl=0.0,
                    detail="waiting",
                )
                for name in (
                    "LEAD_LAG",
                    "PAIR_SPREAD",
                    "INV_HEDGE",
                    "TAKER_EXIT",
                    "NOISE_FADE",
                )
            ]

        return [
            self._format_lead_lag_row(
                traces,
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
            ),
            self._format_pair_spread_row(
                traces,
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
            ),
            self._format_inventory_hedge_row(
                traces,
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
            ),
            self._format_taker_overlay_row(
                traces,
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
            ),
            self._format_noise_fade_row(
                traces,
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
            ),
        ]

    def _format_lead_lag_row(
        self,
        traces: Sequence[StrategyDecisionTrace],
        *,
        market_state: MarketState | None = None,
        portfolio_ledger: PortfolioLedger | None = None,
    ) -> str:
        shifts = [
            float(trace.diagnostics.extra.get("lead_lag_shift_ticks", 0.0))
            for trace in traces
        ]
        confidences = [
            float(trace.diagnostics.extra.get("lead_lag_confidence", 0.0))
            for trace in traces
        ]
        active_count = sum(
            1
            for shift, confidence in zip(shifts, confidences)
            if abs(shift) > 1e-9 and confidence > 0.0
        )
        top_leader = self._most_common_nonempty_extra(traces, "lead_lag_leader_symbol")
        return self._format_strategy_row(
            name="LEAD_LAG",
            weight=self._average_extra(traces, "sleeve_weight_lead_lag", 0.0),
            signal=self._average(shifts),
            confidence=self._average(confidences),
            active_count=active_count,
            pnl=self._strategy_sleeve_pnl(
                sleeve_credit_key="sleeve_credit_lead_lag",
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
                traces=traces,
            ),
            detail=f"TopLeader={top_leader or 'n/a'}",
        )

    def _format_pair_spread_row(
        self,
        traces: Sequence[StrategyDecisionTrace],
        *,
        market_state: MarketState | None = None,
        portfolio_ledger: PortfolioLedger | None = None,
    ) -> str:
        shifts = [
            float(trace.diagnostics.extra.get("pair_spread_shift_ticks", 0.0))
            for trace in traces
        ]
        confidences = [
            float(trace.diagnostics.extra.get("pair_spread_confidence", 0.0))
            for trace in traces
        ]
        residuals = [
            float(trace.diagnostics.extra.get("pair_spread_residual_ticks", 0.0))
            for trace in traces
        ]
        active_count = sum(
            1
            for shift, confidence in zip(shifts, confidences)
            if abs(shift) > 1e-9 and confidence > 0.0
        )
        top_peer = self._most_common_nonempty_extra(traces, "pair_spread_peer_symbol")
        return self._format_strategy_row(
            name="PAIR_SPREAD",
            weight=self._average_extra(traces, "sleeve_weight_pair_spread", 0.0),
            signal=self._average(residuals),
            confidence=self._average(confidences),
            active_count=active_count,
            pnl=self._strategy_sleeve_pnl(
                sleeve_credit_key="sleeve_credit_pair_spread",
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
                traces=traces,
            ),
            detail=f"TopPeer={top_peer or 'n/a'}",
        )

    def _format_inventory_hedge_row(
        self,
        traces: Sequence[StrategyDecisionTrace],
        *,
        market_state: MarketState | None = None,
        portfolio_ledger: PortfolioLedger | None = None,
    ) -> str:
        multipliers = [
            float(trace.diagnostics.extra.get("inventory_hedge_multiplier", 1.0))
            for trace in traces
        ]
        confidences = [
            float(trace.diagnostics.extra.get("inventory_hedge_confidence", 0.0))
            for trace in traces
        ]
        ratios = [
            float(trace.diagnostics.extra.get("inventory_hedge_ratio", 0.0))
            for trace in traces
        ]
        active_count = sum(
            1
            for multiplier, confidence in zip(multipliers, confidences)
            if multiplier < 0.999 and confidence > 0.0
        )
        hedge_symbol = self._most_common_nonempty_extra(traces, "inventory_hedge_symbol")
        return self._format_strategy_row(
            name="INV_HEDGE",
            weight=self._average_extra(traces, "sleeve_weight_inv_hedge", 0.0),
            signal=self._average(ratios),
            confidence=self._average(confidences),
            active_count=active_count,
            pnl=self._strategy_sleeve_pnl(
                sleeve_credit_key="sleeve_credit_inv_hedge",
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
                traces=traces,
            ),
            detail=f"Hedge={hedge_symbol or 'n/a'}",
        )

    def _format_taker_overlay_row(
        self,
        traces: Sequence[StrategyDecisionTrace],
        *,
        market_state: MarketState | None = None,
        portfolio_ledger: PortfolioLedger | None = None,
    ) -> str:
        edge_ticks = [
            float(trace.diagnostics.extra.get("taker_overlay_edge_ticks", 0.0))
            for trace in traces
        ]
        sizes = [
            float(trace.diagnostics.extra.get("taker_overlay_size_lots", 0.0))
            for trace in traces
        ]
        active_count = sum(
            1
            for trace in traces
            if bool(trace.diagnostics.extra.get("taker_overlay_mode", False))
        )
        return self._format_strategy_row(
            name="TAKER_EXIT",
            weight=self._average_extra(traces, "sleeve_weight_taker_exit", 0.0),
            signal=self._average(edge_ticks),
            confidence=active_count / len(traces),
            active_count=active_count,
            pnl=self._strategy_sleeve_pnl(
                sleeve_credit_key="sleeve_credit_taker_exit",
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
                traces=traces,
            ),
            detail="inventory-reducing",
        )

    def _format_noise_fade_row(
        self,
        traces: Sequence[StrategyDecisionTrace],
        *,
        market_state: MarketState | None = None,
        portfolio_ledger: PortfolioLedger | None = None,
    ) -> str:
        shifts = [
            float(trace.diagnostics.extra.get("noise_fade_shift_ticks", 0.0))
            for trace in traces
        ]
        mean_reversion = [
            float(trace.diagnostics.extra.get("mean_reversion_score", 0.0))
            for trace in traces
        ]
        active_count = sum(
            1
            for trace in traces
            if bool(trace.diagnostics.extra.get("noise_fade_taker_mode", False))
        )
        return self._format_strategy_row(
            name="NOISE_FADE",
            weight=self._average_extra(traces, "sleeve_weight_noise_fade", 0.0),
            signal=self._average(shifts),
            confidence=self._average(mean_reversion),
            active_count=active_count,
            pnl=self._strategy_sleeve_pnl(
                sleeve_credit_key="sleeve_credit_noise_fade",
                market_state=market_state,
                portfolio_ledger=portfolio_ledger,
                traces=traces,
            ),
            detail="short-MA snapback",
        )

    def _format_symbol_header(self) -> str:
        windows = "".join(
            f"{('PnL' + str(window) + 's'):>12}"
            for window in self.config.rolling_windows_s
        )
        return (
            f"{'SYMBOL':<8}"
            f"{'POSL':>8}"
            f"{'VPOSL':>8}"
            f"{'TILT':>8}"
            f"{'MID':>10}"
            f"{'BID':>10}"
            f"{'ASK':>10}"
            f"{'SPRD':>8}"
            f"{windows}"
            f"{'WEIGHT':>10}"
            f"{'TOX%':>8}  "
            "MODE"
        )

    def _format_symbol_row(
        self,
        *,
        symbol: str,
        state: SymbolState | None,
        position: Position | None,
        trace: StrategyDecisionTrace | None,
        now_ns: int,
    ) -> str:
        pos_lots = position.net_lots if position is not None else 0
        mid_px = state.mid_price if state is not None else 0.0
        bid_px = state.best_price.best_bid_px if state is not None else 0.0
        ask_px = state.best_price.best_ask_px if state is not None else 0.0
        spread = state.spread if state is not None else 0.0
        vpos_lots, depth_tilt = self._depth_virtual_position(state)
        realized_pnl = position.realized_pl if position is not None else 0.0
        unrealized_pnl = self._symbol_unrealized_pnl(position, state)
        total_pnl = realized_pnl + unrealized_pnl
        rolling_pnl = self._rolling_symbol_pnl(
            symbol=symbol,
            total_pnl=total_pnl,
            now_ns=now_ns,
        )

        allocation_weight = 0.0
        toxicity_markout_pct = 0.0
        mode = "n/a"
        if trace is not None:
            allocation_weight = trace.diagnostics.allocation_weight
            toxicity_markout_pct = float(
                trace.diagnostics.extra.get("toxicity_markout_pct", 0.0)
            )
            mode = trace.diagnostics.participation_mode or trace.decision_reason

        rolling_text = "".join(f"{value:>12.2f}" for value in rolling_pnl)
        return (
            f"{symbol:<8}"
            f"{pos_lots:>8d}"
            f"{vpos_lots:>8d}"
            f"{depth_tilt:>8.2f}"
            f"{mid_px:>10.2f}"
            f"{bid_px:>10.2f}"
            f"{ask_px:>10.2f}"
            f"{spread:>8.2f}"
            f"{rolling_text}"
            f"{allocation_weight:>10.2f}"
            f"{toxicity_markout_pct:>8.3f}  "
            f"{mode}"
        )

    def _depth_virtual_position(self, state: SymbolState | None) -> tuple[int, float]:
        if state is None:
            return 0, 0.0
        bid_depth = state.cumulative_bid_depth(levels=3, local=True)
        ask_depth = state.cumulative_ask_depth(levels=3, local=True)
        raw_vpos_lots = bid_depth - ask_depth
        previous = self._vpos_ewma_by_symbol.get(state.symbol)
        if previous is None:
            smoothed_vpos_lots = float(raw_vpos_lots)
        else:
            smoothed_vpos_lots = 0.35 * raw_vpos_lots + 0.65 * previous
        self._vpos_ewma_by_symbol[state.symbol] = smoothed_vpos_lots
        total = bid_depth + ask_depth
        tilt = (raw_vpos_lots / total) if total > 0 else 0.0
        return int(round(smoothed_vpos_lots)), tilt

    def _rolling_symbol_pnl(
        self,
        *,
        symbol: str,
        total_pnl: float,
        now_ns: int,
    ) -> list[float]:
        history = self._pnl_history_by_symbol[symbol]
        history.append(SymbolPnlSample(ts_ns=now_ns, total_pnl=total_pnl))
        max_window_ns = max(self.config.rolling_windows_s, default=0) * NS_PER_S
        min_keep_ns = now_ns - max_window_ns
        while len(history) > 1 and history[0].ts_ns < min_keep_ns:
            history.popleft()

        pnl_changes: list[float] = []
        for window_s in self.config.rolling_windows_s:
            cutoff_ns = now_ns - window_s * NS_PER_S
            baseline = history[0].total_pnl
            for sample in history:
                if sample.ts_ns >= cutoff_ns:
                    baseline = sample.total_pnl
                    break
            pnl_changes.append(total_pnl - baseline)
        return pnl_changes

    @staticmethod
    def _symbol_unrealized_pnl(
        position: Position | None,
        state: SymbolState | None,
    ) -> float:
        if position is None:
            return 0.0
        pnl = 0.0
        mark_price = state.mid_price if state is not None else 0.0
        if position.long_shares > 0 and mark_price > 0.0:
            entry_price = position.long_price if position.long_price > 0.0 else mark_price
            pnl += position.long_shares * (mark_price - entry_price)
        if position.short_shares > 0 and mark_price > 0.0:
            entry_price = position.short_price if position.short_price > 0.0 else mark_price
            pnl += position.short_shares * (entry_price - mark_price)
        if not math.isfinite(pnl):
            return 0.0
        return pnl

    @staticmethod
    def _symbol_reference_notional(position: Position | None) -> float:
        if position is None:
            return 0.0
        long_ref = position.long_price if position.long_price > 0.0 else 0.0
        short_ref = position.short_price if position.short_price > 0.0 else 0.0
        return (
            position.long_shares * long_ref
            + position.short_shares * short_ref
        )

    @staticmethod
    def _account_pace_metrics(
        traces: Sequence[StrategyDecisionTrace],
    ) -> tuple[float, int, float]:
        if not traces:
            return 0.0, 0, 0.0
        extra = traces[0].diagnostics.extra
        observed_trades = int(extra.get("observed_trade_count", 0))
        expected_trades = float(extra.get("expected_trade_count", 0.0))
        pace_ratio = (
            observed_trades / expected_trades
            if expected_trades > 1e-9
            else 0.0
        )
        return pace_ratio, observed_trades, expected_trades

    @staticmethod
    def _average_allocation_weight(traces: Sequence[StrategyDecisionTrace]) -> float:
        if not traces:
            return 0.0
        return sum(trace.diagnostics.allocation_weight for trace in traces) / len(traces)

    @staticmethod
    def _average_markout_pct(traces: Sequence[StrategyDecisionTrace]) -> float:
        if not traces:
            return 0.0
        markouts = [
            float(trace.diagnostics.extra.get("toxicity_markout_pct", 0.0))
            for trace in traces
        ]
        return sum(
            markout_pct
            for markout_pct in markouts
            if math.isfinite(markout_pct) and 0.0 <= markout_pct <= 10.0
        ) / len(markouts)

    def _markout_pnl_estimate(
        self,
        *,
        market_state: MarketState | None,
        portfolio_ledger: PortfolioLedger,
        traces: Sequence[StrategyDecisionTrace],
    ) -> float:
        trace_by_symbol = {trace.symbol: trace for trace in traces}
        total_markout = 0.0
        for symbol, position in portfolio_ledger.positions.items():
            trace = trace_by_symbol.get(symbol)
            if trace is None:
                continue
            state = (
                market_state.by_symbol.get(symbol)
                if market_state is not None
                else None
            )
            reference_notional = self._symbol_reference_notional(position)
            if reference_notional <= 0.0 and state is not None:
                reference_notional = abs(position.net_shares) * state.mid_price
            markout_pct = float(trace.diagnostics.extra.get("toxicity_markout_pct", 0.0))
            if not math.isfinite(markout_pct) or markout_pct < 0.0:
                markout_pct = 0.0
            markout_pct = min(markout_pct, 10.0)
            total_markout -= reference_notional * markout_pct / 100.0
        return total_markout

    def _strategy_sleeve_pnl(
        self,
        *,
        sleeve_credit_key: str,
        market_state: MarketState | None,
        portfolio_ledger: PortfolioLedger | None,
        traces: Sequence[StrategyDecisionTrace],
    ) -> float:
        if portfolio_ledger is None or not traces:
            return 0.0

        total_sleeve_pnl = 0.0
        for trace in traces:
            credit = float(trace.diagnostics.extra.get(sleeve_credit_key, 0.0))
            if credit <= 0.0:
                continue
            position = portfolio_ledger.positions.get(trace.symbol)
            symbol_state = (
                market_state.by_symbol.get(trace.symbol)
                if market_state is not None
                else None
            )
            realized_pnl = position.realized_pl if position is not None else 0.0
            unrealized_pnl = self._symbol_unrealized_pnl(position, symbol_state)
            reference_notional = self._symbol_reference_notional(position)
            if reference_notional <= 0.0 and symbol_state is not None and position is not None:
                reference_notional = abs(position.net_shares) * symbol_state.mid_price
            markout_pct = float(
                trace.diagnostics.extra.get("toxicity_markout_pct", 0.0)
            )
            if not math.isfinite(markout_pct) or markout_pct < 0.0:
                markout_pct = 0.0
            symbol_markout_pnl = -reference_notional * min(markout_pct, 10.0) / 100.0
            total_sleeve_pnl += credit * (
                realized_pnl + unrealized_pnl + symbol_markout_pnl
            )
        return total_sleeve_pnl

    @staticmethod
    def _account_mode(traces: Sequence[StrategyDecisionTrace]) -> str:
        if not traces:
            return "waiting"
        mode_counts: dict[str, int] = {}
        for trace in traces:
            mode = trace.diagnostics.participation_mode or trace.decision_reason or "n/a"
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
        return max(mode_counts, key=lambda mode: (mode_counts[mode], mode))

    @staticmethod
    def _average(values: Sequence[float]) -> float:
        if not values:
            return 0.0
        finite_values = [value for value in values if math.isfinite(value)]
        if not finite_values:
            return 0.0
        return sum(finite_values) / len(finite_values)

    @classmethod
    def _average_abs(cls, values: Sequence[float]) -> float:
        return cls._average([abs(value) for value in values])

    @classmethod
    def _average_extra(
        cls,
        traces: Sequence[StrategyDecisionTrace],
        key: str,
        default: float,
    ) -> float:
        return cls._average(
            [
                float(trace.diagnostics.extra.get(key, default))
                for trace in traces
            ]
        )

    @staticmethod
    def _most_common_nonempty_extra(
        traces: Sequence[StrategyDecisionTrace],
        key: str,
    ) -> str:
        counts: dict[str, int] = {}
        for trace in traces:
            value = str(trace.diagnostics.extra.get(key, "") or "")
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        if not counts:
            return ""
        return max(counts, key=lambda value: (counts[value], value))
