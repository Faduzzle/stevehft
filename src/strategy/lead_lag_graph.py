from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _prepend_embedding_value(
    value: float,
    previous_embedding: Sequence[float],
    embedding_dim: int,
) -> tuple[float, ...]:
    bounded_dim = max(int(embedding_dim), 1)
    values = [float(value)]
    values.extend(float(item) for item in previous_embedding[: bounded_dim - 1])
    if len(values) < bounded_dim:
        values.extend(0.0 for _ in range(bounded_dim - len(values)))
    return tuple(values)


class LeadLagObservationLike(Protocol):
    symbol: str
    mid_price: float
    local_depth_imbalance: float
    depth_entropy: float
    local_multi_level_voi: float
    global_l1_voi: float
    book_age_ms: float
    spread_ticks: float


@dataclass(slots=True)
class OnlineLeadLagGraphConfig:
    alpha: float = 0.08
    delay_embedding_dim: int = 4
    min_samples: int = 12
    score_shrinkage_samples: float = 48.0
    min_abs_score: float = 0.05
    min_asymmetry: float = 0.02
    entropy_confidence_floor: float = 0.25
    max_leaders_per_follower: int = 3
    max_abs_signal: float = 2.0
    max_shift_ticks: float = 0.75
    max_spread_residual_ticks: float = 0.75
    max_book_age_ms: float = 500.0
    max_spread_ticks: float = 10.0


@dataclass(slots=True)
class LeadLagPairState:
    covariance_by_lag: list[float] = field(default_factory=list)
    leader_var_by_lag: list[float] = field(default_factory=list)
    follower_var_by_lag: list[float] = field(default_factory=list)
    mid_ratio_ewma: float = 0.0
    score: float = 0.0
    confidence: float = 0.0
    shrunk_score: float = 0.0
    spread_confidence: float = 0.0
    samples_seen: int = 0

    def update(
        self,
        *,
        leader_signal_embedding: Sequence[float],
        leader_mid: float,
        follower_return_embedding: Sequence[float],
        follower_mid: float,
        alpha: float,
        shrinkage_samples: float,
    ) -> None:
        bounded_alpha = _clamp(alpha, 0.0, 1.0)
        embedding_dim = min(
            len(leader_signal_embedding),
            len(follower_return_embedding),
        )
        if embedding_dim <= 0:
            return
        if len(self.covariance_by_lag) != embedding_dim:
            self.covariance_by_lag = [0.0] * embedding_dim
            self.leader_var_by_lag = [0.0] * embedding_dim
            self.follower_var_by_lag = [0.0] * embedding_dim

        current_ratio = (
            follower_mid / leader_mid
            if leader_mid > 0.0 and follower_mid > 0.0
            else self.mid_ratio_ewma
        )
        if self.samples_seen <= 0:
            for lag_index in range(embedding_dim):
                leader_value = leader_signal_embedding[lag_index]
                follower_value = follower_return_embedding[lag_index]
                self.covariance_by_lag[lag_index] = leader_value * follower_value
                self.leader_var_by_lag[lag_index] = leader_value * leader_value
                self.follower_var_by_lag[lag_index] = follower_value * follower_value
            self.mid_ratio_ewma = current_ratio
            self.samples_seen = 1
            self.score = self._score()
            self.shrunk_score = self._shrunk_score(shrinkage_samples)
            self.spread_confidence = self._spread_confidence(shrinkage_samples)
            return

        one_minus_alpha = 1.0 - bounded_alpha
        for lag_index in range(embedding_dim):
            leader_value = leader_signal_embedding[lag_index]
            follower_value = follower_return_embedding[lag_index]
            self.covariance_by_lag[lag_index] = (
                one_minus_alpha * self.covariance_by_lag[lag_index]
                + bounded_alpha * leader_value * follower_value
            )
            self.leader_var_by_lag[lag_index] = (
                one_minus_alpha * self.leader_var_by_lag[lag_index]
                + bounded_alpha * leader_value * leader_value
            )
            self.follower_var_by_lag[lag_index] = (
                one_minus_alpha * self.follower_var_by_lag[lag_index]
                + bounded_alpha * follower_value * follower_value
            )
        self.mid_ratio_ewma = (
            one_minus_alpha * self.mid_ratio_ewma
            + bounded_alpha * current_ratio
        )
        self.samples_seen += 1
        self.score = self._score()
        self.shrunk_score = self._shrunk_score(shrinkage_samples)
        self.spread_confidence = self._spread_confidence(shrinkage_samples)

    def _score(self) -> float:
        if not self.covariance_by_lag:
            return 0.0
        weighted_score = 0.0
        weight_sum = 0.0
        for lag_index, covariance in enumerate(self.covariance_by_lag):
            leader_var = self.leader_var_by_lag[lag_index]
            follower_var = self.follower_var_by_lag[lag_index]
            denom = max((leader_var * follower_var) ** 0.5, 1e-9)
            lag_weight = 1.0 / float(lag_index + 1)
            weighted_score += lag_weight * _clamp(covariance / denom, -1.0, 1.0)
            weight_sum += lag_weight
        return _clamp(weighted_score / max(weight_sum, 1e-9), -1.0, 1.0)

    def _shrunk_score(self, shrinkage_samples: float) -> float:
        shrinkage = self.samples_seen / max(
            self.samples_seen + max(shrinkage_samples, 0.0),
            1e-9,
        )
        return self.score * _clamp(shrinkage, 0.0, 1.0)

    def _spread_confidence(self, shrinkage_samples: float) -> float:
        shrinkage = self.samples_seen / max(
            self.samples_seen + max(shrinkage_samples, 0.0),
            1e-9,
        )
        return _clamp(abs(self.score) * shrinkage, 0.0, 1.0)


@dataclass(slots=True)
class LeadLagFollowerOverlay:
    shift_ticks: float = 0.0
    confidence: float = 0.0
    leader_symbol: str = ""
    edge_score: float = 0.0


@dataclass(slots=True)
class LeadLagInventoryOverlay:
    hedge_multiplier: float = 1.0
    hedge_confidence: float = 0.0
    hedge_symbol: str = ""
    hedge_ratio: float = 0.0


@dataclass(slots=True)
class LeadLagSpreadOverlay:
    shift_ticks: float = 0.0
    confidence: float = 0.0
    peer_symbol: str = ""
    residual_ticks: float = 0.0


@dataclass(slots=True)
class OnlineLeadLagGraph:
    config: OnlineLeadLagGraphConfig = field(default_factory=OnlineLeadLagGraphConfig)
    prev_mid_by_symbol: dict[str, float] = field(default_factory=dict)
    prev_signal_by_symbol: dict[str, tuple[float, ...]] = field(default_factory=dict)
    current_signal_by_symbol: dict[str, float] = field(default_factory=dict)
    entropy_confidence_by_symbol: dict[str, float] = field(default_factory=dict)
    signal_embedding_by_symbol: dict[str, tuple[float, ...]] = field(default_factory=dict)
    return_embedding_by_symbol: dict[str, tuple[float, ...]] = field(default_factory=dict)
    pair_state_by_follower: dict[str, dict[str, LeadLagPairState]] = field(
        default_factory=dict
    )
    overlay_by_symbol: dict[str, LeadLagFollowerOverlay] = field(default_factory=dict)
    spread_overlay_by_symbol: dict[str, LeadLagSpreadOverlay] = field(
        default_factory=dict
    )

    def update(
        self,
        observations: Sequence[LeadLagObservationLike],
        *,
        tick_size: float,
    ) -> None:
        if not observations:
            return

        returns_by_symbol: dict[str, float] = {}
        current_signal_by_symbol: dict[str, float] = {}
        current_signal_embedding_by_symbol: dict[str, tuple[float, ...]] = {}
        current_return_embedding_by_symbol: dict[str, tuple[float, ...]] = {}
        entropy_confidence_by_symbol: dict[str, float] = {}
        tradable_by_symbol: dict[str, bool] = {}
        embedding_dim = max(int(self.config.delay_embedding_dim), 1)
        for obs in observations:
            prev_mid = self.prev_mid_by_symbol.get(obs.symbol, 0.0)
            if prev_mid > 0.0 and obs.mid_price > 0.0:
                return_ticks = (obs.mid_price - prev_mid) / max(tick_size, 1e-9)
            else:
                return_ticks = 0.0
            signal = _clamp(
                return_ticks
                + 0.35 * obs.local_multi_level_voi
                + 0.20 * obs.global_l1_voi
                + 0.20 * obs.local_depth_imbalance,
                -self.config.max_abs_signal,
                self.config.max_abs_signal,
            )
            returns_by_symbol[obs.symbol] = return_ticks
            current_signal_by_symbol[obs.symbol] = signal
            current_signal_embedding_by_symbol[obs.symbol] = _prepend_embedding_value(
                signal,
                self.signal_embedding_by_symbol.get(obs.symbol, ()),
                embedding_dim,
            )
            current_return_embedding_by_symbol[obs.symbol] = _prepend_embedding_value(
                return_ticks,
                self.return_embedding_by_symbol.get(obs.symbol, ()),
                embedding_dim,
            )
            entropy_confidence_by_symbol[obs.symbol] = max(
                self.config.entropy_confidence_floor,
                _clamp(
                    1.0
                    - 0.20 * obs.depth_entropy
                    - 0.35 * (1.0 - abs(signal) / self.config.max_abs_signal),
                    0.0,
                    1.0,
                ),
            )
            tradable_by_symbol[obs.symbol] = (
                obs.mid_price > 0.0
                and obs.book_age_ms <= self.config.max_book_age_ms
                and obs.spread_ticks <= self.config.max_spread_ticks
            )

        symbols = tuple(obs.symbol for obs in observations)
        current_mid_by_symbol = {
            obs.symbol: obs.mid_price
            for obs in observations
        }
        for follower_symbol in symbols:
            follower_edges = self.pair_state_by_follower.setdefault(
                follower_symbol,
                {},
            )
            follower_return_ticks = returns_by_symbol[follower_symbol]
            for leader_symbol in symbols:
                if leader_symbol == follower_symbol:
                    continue
                edge_state = follower_edges.setdefault(
                    leader_symbol,
                    LeadLagPairState(),
                )
                edge_state.update(
                    leader_signal_embedding=self.prev_signal_by_symbol.get(
                        leader_symbol,
                        (0.0,) * embedding_dim,
                    ),
                    leader_mid=current_mid_by_symbol.get(leader_symbol, 0.0),
                    follower_return_embedding=current_return_embedding_by_symbol.get(
                        follower_symbol,
                        (0.0,) * embedding_dim,
                    ),
                    follower_mid=current_mid_by_symbol.get(follower_symbol, 0.0),
                    alpha=self.config.alpha,
                    shrinkage_samples=self.config.score_shrinkage_samples,
                )

        self.current_signal_by_symbol = current_signal_by_symbol
        self.entropy_confidence_by_symbol = entropy_confidence_by_symbol
        self.overlay_by_symbol = {
            follower_symbol: self._build_overlay(
                follower_symbol=follower_symbol,
                follower_edges=follower_edges,
                tradable_by_symbol=tradable_by_symbol,
                entropy_confidence_by_symbol=entropy_confidence_by_symbol,
                current_signal_by_symbol=current_signal_by_symbol,
            )
            for follower_symbol, follower_edges in self.pair_state_by_follower.items()
        }
        self.spread_overlay_by_symbol = {
            follower_symbol: self._build_spread_overlay(
                follower_symbol=follower_symbol,
                follower_edges=follower_edges,
                tradable_by_symbol=tradable_by_symbol,
                entropy_confidence_by_symbol=entropy_confidence_by_symbol,
                mid_by_symbol=current_mid_by_symbol,
                tick_size=tick_size,
            )
            for follower_symbol, follower_edges in self.pair_state_by_follower.items()
        }
        self.prev_signal_by_symbol = dict(current_signal_embedding_by_symbol)
        self.signal_embedding_by_symbol = dict(current_signal_embedding_by_symbol)
        self.return_embedding_by_symbol = dict(current_return_embedding_by_symbol)
        self.prev_mid_by_symbol = {
            obs.symbol: obs.mid_price
            for obs in observations
            if obs.mid_price > 0.0
        }

    def overlay_for(self, symbol: str) -> LeadLagFollowerOverlay:
        return self.overlay_by_symbol.get(symbol, LeadLagFollowerOverlay())

    def spread_overlay_for(self, symbol: str) -> LeadLagSpreadOverlay:
        return self.spread_overlay_by_symbol.get(symbol, LeadLagSpreadOverlay())

    def pair_state(
        self,
        follower_symbol: str,
        leader_symbol: str,
    ) -> LeadLagPairState | None:
        return self.pair_state_by_follower.get(follower_symbol, {}).get(leader_symbol)

    def inventory_overlay_for(
        self,
        symbol: str,
        *,
        inventory_lots_by_symbol: Mapping[str, int],
    ) -> LeadLagInventoryOverlay:
        own_inventory_lots = inventory_lots_by_symbol.get(symbol, 0)
        if own_inventory_lots == 0:
            return LeadLagInventoryOverlay()

        best_symbol = ""
        best_confidence = 0.0
        signed_hedge_lots = 0.0
        abs_weight = 0.0
        for other_symbol, other_inventory_lots in inventory_lots_by_symbol.items():
            if other_symbol == symbol or other_inventory_lots == 0:
                continue
            forward = self.pair_state(symbol, other_symbol)
            reverse = self.pair_state(other_symbol, symbol)
            edge_weight = 0.0
            edge_score = 0.0
            if forward is not None and forward.confidence > 0.0:
                edge_weight += forward.confidence
                edge_score += forward.confidence * forward.shrunk_score
            if reverse is not None and reverse.confidence > 0.0:
                edge_weight += reverse.confidence
                edge_score += reverse.confidence * reverse.shrunk_score
            if edge_weight <= 0.0:
                continue
            avg_edge_score = edge_score / edge_weight
            signed_hedge_lots += avg_edge_score * edge_weight * other_inventory_lots
            abs_weight += edge_weight * abs(other_inventory_lots)
            if edge_weight > best_confidence:
                best_confidence = edge_weight
                best_symbol = other_symbol

        if abs_weight <= 0.0:
            return LeadLagInventoryOverlay()

        net_group_lots = own_inventory_lots + signed_hedge_lots
        hedge_ratio = _clamp(
            1.0 - abs(net_group_lots) / max(abs(own_inventory_lots), 1.0),
            0.0,
            1.0,
        )
        hedge_confidence = _clamp(best_confidence, 0.0, 1.0)
        hedge_multiplier = _clamp(
            1.0 - 0.60 * hedge_ratio * hedge_confidence,
            0.40,
            1.0,
        )
        return LeadLagInventoryOverlay(
            hedge_multiplier=hedge_multiplier,
            hedge_confidence=hedge_confidence,
            hedge_symbol=best_symbol,
            hedge_ratio=hedge_ratio,
        )

    def _build_overlay(
        self,
        *,
        follower_symbol: str,
        follower_edges: Mapping[str, LeadLagPairState],
        tradable_by_symbol: Mapping[str, bool],
        entropy_confidence_by_symbol: Mapping[str, float],
        current_signal_by_symbol: Mapping[str, float],
    ) -> LeadLagFollowerOverlay:
        candidates: list[tuple[float, str, LeadLagPairState]] = []
        for leader_symbol, edge_state in follower_edges.items():
            reverse_score = abs(
                self.pair_state_by_follower.get(leader_symbol, {})
                .get(follower_symbol, LeadLagPairState())
                .shrunk_score
            )
            entropy_confidence = min(
                entropy_confidence_by_symbol.get(leader_symbol, 1.0),
                entropy_confidence_by_symbol.get(follower_symbol, 1.0),
            )
            asymmetry = max(abs(edge_state.shrunk_score) - reverse_score, 0.0)
            sample_confidence = _clamp(
                edge_state.samples_seen / max(self.config.min_samples, 1),
                0.0,
                1.0,
            )
            edge_confidence = _clamp(
                sample_confidence * asymmetry * entropy_confidence,
                0.0,
                1.0,
            )
            if (
                edge_state.samples_seen < self.config.min_samples
                or abs(edge_state.shrunk_score) < self.config.min_abs_score
                or asymmetry < self.config.min_asymmetry
                or edge_confidence <= 0.0
                or not tradable_by_symbol.get(leader_symbol, False)
                or not tradable_by_symbol.get(follower_symbol, False)
            ):
                edge_state.confidence = 0.0
                continue
            edge_state.confidence = edge_confidence
            candidates.append((edge_confidence, leader_symbol, edge_state))

        if not candidates:
            return LeadLagFollowerOverlay()

        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = candidates[: max(self.config.max_leaders_per_follower, 1)]
        shift_ticks = 0.0
        confidence_sum = 0.0
        leader_symbol = selected[0][1]
        leader_edge_score = selected[0][2].shrunk_score
        for edge_confidence, selected_leader_symbol, edge_state in selected:
            leader_signal = current_signal_by_symbol.get(selected_leader_symbol, 0.0)
            shift_ticks += edge_confidence * edge_state.shrunk_score * leader_signal
            confidence_sum += edge_confidence

        return LeadLagFollowerOverlay(
            shift_ticks=_clamp(
                shift_ticks,
                -self.config.max_shift_ticks,
                self.config.max_shift_ticks,
            ),
            confidence=_clamp(confidence_sum / len(selected), 0.0, 1.0),
            leader_symbol=leader_symbol,
            edge_score=leader_edge_score,
        )

    def _build_spread_overlay(
        self,
        *,
        follower_symbol: str,
        follower_edges: Mapping[str, LeadLagPairState],
        tradable_by_symbol: Mapping[str, bool],
        entropy_confidence_by_symbol: Mapping[str, float],
        mid_by_symbol: Mapping[str, float],
        tick_size: float,
    ) -> LeadLagSpreadOverlay:
        follower_mid = mid_by_symbol.get(follower_symbol, 0.0)
        if follower_mid <= 0.0 or not tradable_by_symbol.get(follower_symbol, False):
            return LeadLagSpreadOverlay()

        weighted_synthetic_mid = 0.0
        weight_sum = 0.0
        best_symbol = ""
        best_confidence = 0.0
        best_residual_ticks = 0.0
        follower_entropy_confidence = entropy_confidence_by_symbol.get(
            follower_symbol,
            1.0,
        )
        for peer_symbol, edge_state in follower_edges.items():
            peer_mid = mid_by_symbol.get(peer_symbol, 0.0)
            if (
                edge_state.samples_seen < self.config.min_samples
                or edge_state.spread_confidence <= 0.0
                or edge_state.mid_ratio_ewma <= 0.0
                or peer_mid <= 0.0
                or not tradable_by_symbol.get(peer_symbol, False)
            ):
                continue
            spread_confidence = _clamp(
                edge_state.spread_confidence
                * min(
                    follower_entropy_confidence,
                    entropy_confidence_by_symbol.get(peer_symbol, 1.0),
                ),
                0.0,
                1.0,
            )
            if spread_confidence <= 0.0:
                continue
            synthetic_mid = edge_state.mid_ratio_ewma * peer_mid
            residual_ticks = (synthetic_mid - follower_mid) / max(tick_size, 1e-9)
            weighted_synthetic_mid += spread_confidence * synthetic_mid
            weight_sum += spread_confidence
            if spread_confidence > best_confidence:
                best_confidence = spread_confidence
                best_symbol = peer_symbol
                best_residual_ticks = residual_ticks

        if weight_sum <= 0.0:
            return LeadLagSpreadOverlay()

        synthetic_mid = weighted_synthetic_mid / weight_sum
        residual_ticks = (synthetic_mid - follower_mid) / max(tick_size, 1e-9)
        confidence = _clamp(weight_sum / max(len(follower_edges), 1), 0.0, 1.0)
        return LeadLagSpreadOverlay(
            shift_ticks=_clamp(
                confidence * residual_ticks,
                -self.config.max_spread_residual_ticks,
                self.config.max_spread_residual_ticks,
            ),
            confidence=confidence,
            peer_symbol=best_symbol,
            residual_ticks=_clamp(
                best_residual_ticks,
                -self.config.max_spread_residual_ticks,
                self.config.max_spread_residual_ticks,
            ),
        )
