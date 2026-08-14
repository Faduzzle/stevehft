from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(slots=True)
class CalibrationRow:
    """One per-symbol snapshot emitted every quoting cycle.

    Fields are grouped into three categories:
    - inputs:   raw market features the model consumes
    - state:    online estimator state at the time of the decision
    - outcomes: what actually happened (filled in one cycle later by the next row,
                or by the fill-markout consumer); used as training targets

    To reconstruct training data offline:
      1. Sort by (symbol, ts_ns).
      2. For each row n, the target mid_move_ticks is row[n+1].mid_price - row[n].mid_price
         divided by tick_size.
      3. The realized_toxicity_markout_pct already reflects the mark-to-market
         of fills initiated around the same timestamp.
    """

    symbol: str
    ts_ns: int
    updates_seen: int

    # --- raw market inputs ---
    mid_price: float
    spread_ticks: float
    book_depth_lk: float
    signed_imbalance: float
    abs_imbalance: float
    local_voi: float
    global_imbalance: float
    global_voi: float
    global_mid_drift_ticks: float
    depth_entropy: float
    flow_entropy: float
    local_trade_linked_voi: float
    trade_signed_volume: float
    book_age_ms: float
    quote_age_ms: float

    # --- RLS feature vector (6 elements) ---
    rls_f0_bias: float
    rls_f1_imbalance: float
    rls_f2_local_voi: float
    rls_f3_global_imbalance: float
    rls_f4_global_voi: float
    rls_f5_trade_linked_voi: float

    # --- RLS prediction and confidence ---
    rls_drift_prediction_ticks: float
    rls_cosine_similarity: float
    rls_prediction_variance: float
    rls_confidence: float

    # --- EWMA estimator state (for alpha calibration) ---
    ewma_spread_ticks: float
    ewma_signed_imbalance: float
    ewma_local_voi: float
    ewma_depth_lk: float
    ewma_global_imbalance: float
    ewma_global_voi: float
    ewma_global_mid_drift: float
    ewma_trade_linked_voi: float
    ewma_toxicity_score: float
    ewma_fast_velocity_ticks: float
    ewma_slow_velocity_ticks: float
    ewma_queue_velocity: float

    # --- Welford distributional state (for breakpoint calibration) ---
    spread_mean_ticks: float
    spread_std_ticks: float
    spread_p90_ticks: float
    depth_p50_lk: float
    quote_age_mean_ms: float
    quote_age_p90_ms: float

    # --- derived composite scores (model outputs) ---
    liquidity_mass_norm: float
    impact_mass_norm: float

    # --- quote decision context (for outcome attribution) ---
    inventory_lots: int
    toxicity_score: float
    toxicity_markout_pct: float
    fill_rate: float
    cancel_rate: float
    passive_fill_arrival_ms: float

    # --- session context ---
    session_elapsed_fraction: float
    session_minutes_to_close: float

    # --- sweep-reversion overlay state ---
    sweep_deviation_ticks: float
    sweep_short_ma_price: float
    sweep_long_ma_price: float
    sweep_detected: bool
    sweep_reversion_mode: bool
    sweep_exit_mode: bool
    sweep_exit_trigger: str


class CalibrationLogger:
    """Writes CalibrationRow records to a JSONL file for offline model fitting.

    Architecture:
    - The main thread calls `log_row()` which is non-blocking (puts into a queue).
    - A background daemon thread drains the queue and writes to disk.
    - On `stop()` all queued rows are flushed before the thread exits.

    Usage:
        logger = CalibrationLogger("logs/calibration.jsonl")
        logger.start()
        ...
        logger.log_row(row)
        ...
        logger.stop()
    """

    def __init__(
        self,
        path: str | Path,
        *,
        flush_every: int = 128,
        max_queue_size: int = 100_000,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every = max(1, flush_every)
        self._queue: queue.Queue[Optional[CalibrationRow]] = queue.Queue(
            maxsize=max_queue_size
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="calibration-logger", daemon=True
        )
        self._dropped_rows: int = 0
        self._started: bool = False
        self._lifecycle_lock = threading.Lock()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="calibration-logger", daemon=True
            )
            self._started = True
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            self._stop.set()
            if self._thread.is_alive():
                try:
                    self._queue.put(None, timeout=timeout)
                except queue.Full:
                    self._dropped_rows += 1
            self._thread.join(timeout=timeout)
            self._started = False

    def log_row(self, row: CalibrationRow) -> None:
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self._dropped_rows += 1

    @property
    def dropped_rows(self) -> int:
        return self._dropped_rows

    def _run(self) -> None:
        pending: list[str] = []
        with self._path.open("a", encoding="utf-8") as fh:
            while not self._stop.is_set():
                item = self._queue.get()
                if item is None:
                    break
                pending.append(self._serialize(item))
                if len(pending) >= self._flush_every:
                    fh.write("\n".join(pending) + "\n")
                    fh.flush()
                    pending.clear()

            # drain remainder
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    pending.append(self._serialize(item))

            if pending:
                fh.write("\n".join(pending) + "\n")
                fh.flush()

    @staticmethod
    def _serialize(row: CalibrationRow) -> str:
        return json.dumps(
            {
                "symbol": row.symbol,
                "ts_ns": row.ts_ns,
                "updates_seen": row.updates_seen,
                "mid_price": row.mid_price,
                "spread_ticks": row.spread_ticks,
                "book_depth_lk": row.book_depth_lk,
                "signed_imbalance": row.signed_imbalance,
                "abs_imbalance": row.abs_imbalance,
                "local_voi": row.local_voi,
                "global_imbalance": row.global_imbalance,
                "global_voi": row.global_voi,
                "global_mid_drift_ticks": row.global_mid_drift_ticks,
                "depth_entropy": row.depth_entropy,
                "flow_entropy": row.flow_entropy,
                "local_trade_linked_voi": row.local_trade_linked_voi,
                "trade_signed_volume": row.trade_signed_volume,
                "book_age_ms": row.book_age_ms,
                "quote_age_ms": row.quote_age_ms,
                "rls_f0_bias": row.rls_f0_bias,
                "rls_f1_imbalance": row.rls_f1_imbalance,
                "rls_f2_local_voi": row.rls_f2_local_voi,
                "rls_f3_global_imbalance": row.rls_f3_global_imbalance,
                "rls_f4_global_voi": row.rls_f4_global_voi,
                "rls_f5_trade_linked_voi": row.rls_f5_trade_linked_voi,
                "rls_drift_prediction_ticks": row.rls_drift_prediction_ticks,
                "rls_cosine_similarity": row.rls_cosine_similarity,
                "rls_prediction_variance": row.rls_prediction_variance,
                "rls_confidence": row.rls_confidence,
                "ewma_spread_ticks": row.ewma_spread_ticks,
                "ewma_signed_imbalance": row.ewma_signed_imbalance,
                "ewma_local_voi": row.ewma_local_voi,
                "ewma_depth_lk": row.ewma_depth_lk,
                "ewma_global_imbalance": row.ewma_global_imbalance,
                "ewma_global_voi": row.ewma_global_voi,
                "ewma_global_mid_drift": row.ewma_global_mid_drift,
                "ewma_trade_linked_voi": row.ewma_trade_linked_voi,
                "ewma_toxicity_score": row.ewma_toxicity_score,
                "ewma_fast_velocity_ticks": row.ewma_fast_velocity_ticks,
                "ewma_slow_velocity_ticks": row.ewma_slow_velocity_ticks,
                "ewma_queue_velocity": row.ewma_queue_velocity,
                "spread_mean_ticks": row.spread_mean_ticks,
                "spread_std_ticks": row.spread_std_ticks,
                "spread_p90_ticks": row.spread_p90_ticks,
                "depth_p50_lk": row.depth_p50_lk,
                "quote_age_mean_ms": row.quote_age_mean_ms,
                "quote_age_p90_ms": row.quote_age_p90_ms,
                "liquidity_mass_norm": row.liquidity_mass_norm,
                "impact_mass_norm": row.impact_mass_norm,
                "inventory_lots": row.inventory_lots,
                "toxicity_score": row.toxicity_score,
                "toxicity_markout_pct": row.toxicity_markout_pct,
                "fill_rate": row.fill_rate,
                "cancel_rate": row.cancel_rate,
                "passive_fill_arrival_ms": row.passive_fill_arrival_ms,
                "session_elapsed_fraction": row.session_elapsed_fraction,
                "session_minutes_to_close": row.session_minutes_to_close,
                "sweep_deviation_ticks": row.sweep_deviation_ticks,
                "sweep_short_ma_price": row.sweep_short_ma_price,
                "sweep_long_ma_price": row.sweep_long_ma_price,
                "sweep_detected": row.sweep_detected,
                "sweep_reversion_mode": row.sweep_reversion_mode,
                "sweep_exit_mode": row.sweep_exit_mode,
                "sweep_exit_trigger": row.sweep_exit_trigger,
            },
            separators=(",", ":"),
        )


def build_calibration_row(
    *,
    symbol: str,
    ts_ns: int,
    adaptive_state: Any,
    spread_ticks: float,
    book_depth_lk: float,
    signed_imbalance: float,
    local_voi: float,
    global_imbalance: float,
    global_voi: float,
    global_mid_drift_ticks: float,
    depth_entropy: float,
    local_trade_linked_voi: float,
    trade_signed_volume: float,
    book_age_ms: float,
    quote_age_ms: float,
    rls_features: list[float],
    inventory_lots: int,
    toxicity_markout_pct: float,
    session_elapsed_fraction: float,
    session_minutes_to_close: float,
    sweep_deviation_ticks: float = 0.0,
    sweep_short_ma_price: float = 0.0,
    sweep_long_ma_price: float = 0.0,
    sweep_detected: bool = False,
    sweep_reversion_mode: bool = False,
    sweep_exit_mode: bool = False,
    sweep_exit_trigger: str = "",
) -> CalibrationRow:
    """Build a CalibrationRow from the observer context and live features.

    Call this at the end of observe_symbol_state, after all state has been
    updated, so the row captures the model's state at decision time.
    """
    feat = rls_features + [0.0] * max(0, 6 - len(rls_features))
    return CalibrationRow(
        symbol=symbol,
        ts_ns=ts_ns,
        updates_seen=adaptive_state.updates_seen,
        mid_price=adaptive_state.last_mid_price,
        spread_ticks=spread_ticks,
        book_depth_lk=book_depth_lk,
        signed_imbalance=signed_imbalance,
        abs_imbalance=abs(signed_imbalance),
        local_voi=local_voi,
        global_imbalance=global_imbalance,
        global_voi=global_voi,
        global_mid_drift_ticks=global_mid_drift_ticks,
        depth_entropy=depth_entropy,
        flow_entropy=adaptive_state.ewma_flow_entropy,
        local_trade_linked_voi=local_trade_linked_voi,
        trade_signed_volume=trade_signed_volume,
        book_age_ms=book_age_ms,
        quote_age_ms=quote_age_ms,
        rls_f0_bias=feat[0],
        rls_f1_imbalance=feat[1],
        rls_f2_local_voi=feat[2],
        rls_f3_global_imbalance=feat[3],
        rls_f4_global_voi=feat[4],
        rls_f5_trade_linked_voi=feat[5],
        rls_drift_prediction_ticks=adaptive_state.rls_drift_prediction_ticks,
        rls_cosine_similarity=adaptive_state.rls_cosine_similarity,
        rls_prediction_variance=adaptive_state.rls_prediction_variance,
        rls_confidence=adaptive_state.rls_confidence,
        ewma_spread_ticks=adaptive_state.ewma_spread_ticks,
        ewma_signed_imbalance=adaptive_state.ewma_signed_imbalance,
        ewma_local_voi=adaptive_state.ewma_local_voi,
        ewma_depth_lk=adaptive_state.ewma_depth_lk,
        ewma_global_imbalance=adaptive_state.ewma_global_imbalance,
        ewma_global_voi=adaptive_state.ewma_global_voi,
        ewma_global_mid_drift=adaptive_state.ewma_global_mid_drift,
        ewma_trade_linked_voi=adaptive_state.ewma_trade_linked_voi,
        ewma_toxicity_score=adaptive_state.ewma_toxicity_score,
        ewma_fast_velocity_ticks=adaptive_state.ewma_fast_velocity_ticks,
        ewma_slow_velocity_ticks=adaptive_state.ewma_slow_velocity_ticks,
        ewma_queue_velocity=adaptive_state.ewma_queue_velocity,
        spread_mean_ticks=adaptive_state.spread_welford.mean,
        spread_std_ticks=adaptive_state.spread_welford.std,
        spread_p90_ticks=adaptive_state.spread_p90.estimate,
        depth_p50_lk=adaptive_state.depth_p50.estimate,
        quote_age_mean_ms=adaptive_state.quote_age_welford.mean,
        quote_age_p90_ms=adaptive_state.quote_age_p90.estimate,
        liquidity_mass_norm=adaptive_state.liquidity_mass_norm,
        impact_mass_norm=adaptive_state.impact_mass_norm,
        inventory_lots=inventory_lots,
        toxicity_score=adaptive_state.ewma_toxicity_score,
        toxicity_markout_pct=toxicity_markout_pct,
        fill_rate=adaptive_state.ewma_fill_rate,
        cancel_rate=adaptive_state.ewma_cancel_rate,
        passive_fill_arrival_ms=adaptive_state.ewma_passive_fill_arrival_ms,
        session_elapsed_fraction=session_elapsed_fraction,
        session_minutes_to_close=session_minutes_to_close,
        sweep_deviation_ticks=sweep_deviation_ticks,
        sweep_short_ma_price=sweep_short_ma_price,
        sweep_long_ma_price=sweep_long_ma_price,
        sweep_detected=sweep_detected,
        sweep_reversion_mode=sweep_reversion_mode,
        sweep_exit_mode=sweep_exit_mode,
        sweep_exit_trigger=sweep_exit_trigger,
    )
