from __future__ import annotations

from dataclasses import dataclass, field


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


@dataclass(slots=True)
class DecayingWelford:
    decay: float = 0.95
    weight: float = 0.0
    mean: float = 0.0
    m2: float = 0.0
    initialized: bool = False

    def update(self, value: float) -> None:
        bounded_decay = _clamp(self.decay, 0.0, 1.0)
        if not self.initialized:
            self.weight = 1.0
            self.mean = value
            self.m2 = 0.0
            self.initialized = True
            return

        decayed_weight = bounded_decay * self.weight
        new_weight = decayed_weight + 1.0
        delta = value - self.mean
        new_mean = self.mean + delta / max(new_weight, 1e-12)

        self.m2 = bounded_decay * self.m2 + decayed_weight * delta * (value - new_mean)
        self.weight = new_weight
        self.mean = new_mean

    @property
    def variance(self) -> float:
        if not self.initialized or self.weight <= 0.0:
            return 0.0
        return max(self.m2 / self.weight, 0.0)

    @property
    def std(self) -> float:
        return self.variance ** 0.5

    def zscore(self, value: float, *, min_std: float = 1e-9) -> float:
        if not self.initialized:
            return 0.0
        return (value - self.mean) / max(self.std, min_std)


@dataclass(slots=True)
class CusumDrift:
    threshold: float = 2.5
    drift: float = 0.0
    positive_sum: float = 0.0
    negative_sum: float = 0.0
    last_signal: int = 0

    def update(self, value: float) -> int:
        centered = value - self.drift
        self.positive_sum = max(0.0, self.positive_sum + centered)
        self.negative_sum = min(0.0, self.negative_sum + centered)
        if self.positive_sum >= self.threshold:
            self.positive_sum = 0.0
            self.negative_sum = 0.0
            self.last_signal = 1
            return 1
        if self.negative_sum <= -self.threshold:
            self.positive_sum = 0.0
            self.negative_sum = 0.0
            self.last_signal = -1
            return -1
        self.last_signal = 0
        return 0


@dataclass(slots=True)
class PageHinkleyDrift:
    threshold: float = 1.5
    alpha: float = 0.05
    delta: float = 0.0
    mean: float = 0.0
    cumulative: float = 0.0
    min_cumulative: float = 0.0
    max_cumulative: float = 0.0
    initialized: bool = False
    last_signal: int = 0

    def update(self, value: float) -> int:
        if not self.initialized:
            self.mean = value
            self.cumulative = 0.0
            self.min_cumulative = 0.0
            self.max_cumulative = 0.0
            self.initialized = True
            self.last_signal = 0
            return 0

        bounded_alpha = _clamp(self.alpha, 0.0, 1.0)
        self.mean = (1.0 - bounded_alpha) * self.mean + bounded_alpha * value
        self.cumulative += value - self.mean - self.delta
        self.min_cumulative = min(self.min_cumulative, self.cumulative)
        self.max_cumulative = max(self.max_cumulative, self.cumulative)

        if self.cumulative - self.min_cumulative >= self.threshold:
            self._reset_after_signal(value)
            self.last_signal = 1
            return 1
        if self.max_cumulative - self.cumulative >= self.threshold:
            self._reset_after_signal(value)
            self.last_signal = -1
            return -1
        self.last_signal = 0
        return 0

    def _reset_after_signal(self, value: float) -> None:
        self.mean = value
        self.cumulative = 0.0
        self.min_cumulative = 0.0
        self.max_cumulative = 0.0


@dataclass(slots=True)
class PSquareQuantile:
    probability: float
    marker_heights: list[float] = field(default_factory=list)
    marker_positions: list[int] = field(default_factory=list)
    desired_positions: list[float] = field(default_factory=list)
    position_increments: list[float] = field(default_factory=list)
    sample_count: int = 0

    def __post_init__(self) -> None:
        self.probability = _clamp(self.probability, 0.0, 1.0)
        self.position_increments = [
            0.0,
            0.5 * self.probability,
            self.probability,
            0.5 * (1.0 + self.probability),
            1.0,
        ]

    def update(self, value: float) -> None:
        if self.sample_count < 5:
            self.marker_heights.append(value)
            self.sample_count += 1
            if self.sample_count == 5:
                self.marker_heights.sort()
                self.marker_positions = [1, 2, 3, 4, 5]
                self.desired_positions = [
                    1.0,
                    1.0 + 2.0 * self.probability,
                    1.0 + 4.0 * self.probability,
                    3.0 + 2.0 * self.probability,
                    5.0,
                ]
            return

        self.sample_count += 1
        marker_index = self._find_marker_cell(value)
        if value < self.marker_heights[0]:
            self.marker_heights[0] = value
            marker_index = 0
        elif value > self.marker_heights[4]:
            self.marker_heights[4] = value
            marker_index = 3

        for idx in range(marker_index + 1, 5):
            self.marker_positions[idx] += 1
        for idx in range(5):
            self.desired_positions[idx] += self.position_increments[idx]

        for idx in range(1, 4):
            desired_delta = self.desired_positions[idx] - self.marker_positions[idx]
            if (
                desired_delta >= 1.0
                and self.marker_positions[idx + 1] - self.marker_positions[idx] > 1
            ) or (
                desired_delta <= -1.0
                and self.marker_positions[idx - 1] - self.marker_positions[idx] < -1
            ):
                step = 1 if desired_delta > 0.0 else -1
                candidate = self._parabolic_height(idx, step)
                if self.marker_heights[idx - 1] < candidate < self.marker_heights[idx + 1]:
                    self.marker_heights[idx] = candidate
                else:
                    self.marker_heights[idx] = self._linear_height(idx, step)
                self.marker_positions[idx] += step

    @property
    def estimate(self) -> float:
        if self.sample_count <= 0:
            return 0.0
        if self.sample_count < 5:
            sorted_samples = sorted(self.marker_heights)
            if len(sorted_samples) == 1:
                return sorted_samples[0]
            rank = self.probability * (len(sorted_samples) - 1)
            lower_index = int(rank)
            upper_index = min(lower_index + 1, len(sorted_samples) - 1)
            blend = rank - lower_index
            return (
                (1.0 - blend) * sorted_samples[lower_index]
                + blend * sorted_samples[upper_index]
            )
        return self.marker_heights[2]

    def _find_marker_cell(self, value: float) -> int:
        for idx in range(4):
            if self.marker_heights[idx] <= value < self.marker_heights[idx + 1]:
                return idx
        return 3

    def _parabolic_height(self, idx: int, step: int) -> float:
        prev_pos = self.marker_positions[idx - 1]
        curr_pos = self.marker_positions[idx]
        next_pos = self.marker_positions[idx + 1]
        prev_height = self.marker_heights[idx - 1]
        curr_height = self.marker_heights[idx]
        next_height = self.marker_heights[idx + 1]

        left_width = curr_pos - prev_pos
        right_width = next_pos - curr_pos
        span = next_pos - prev_pos
        if left_width <= 0 or right_width <= 0 or span <= 0:
            return curr_height

        return curr_height + (step / span) * (
            (curr_pos - prev_pos + step)
            * (next_height - curr_height)
            / right_width
            + (next_pos - curr_pos - step)
            * (curr_height - prev_height)
            / left_width
        )

    def _linear_height(self, idx: int, step: int) -> float:
        next_idx = idx + step
        position_delta = self.marker_positions[next_idx] - self.marker_positions[idx]
        if position_delta == 0:
            return self.marker_heights[idx]
        return self.marker_heights[idx] + step * (
            self.marker_heights[next_idx] - self.marker_heights[idx]
        ) / position_delta


@dataclass(slots=True)
class RecursiveLeastSquares:
    feature_count: int
    forgetting_factor: float = 0.995
    initial_covariance: float = 25.0
    # Ridge coefficient δ added to each diagonal of P after every update.
    # Counteracts the unbounded covariance growth from the 1/λ divisor during
    # low-excitation (quiet market) periods.  Steady-state diagonal of P
    # converges to ~δ/(1-λ).  With λ=0.995, δ=1e-4 → diagonal ~0.02.
    # 0.0 = original behavior (no regularization).
    ridge_coefficient: float = 0.0
    weights: list[float] = field(default_factory=list)
    covariance: list[list[float]] = field(default_factory=list)
    samples_seen: int = 0

    def __post_init__(self) -> None:
        self.feature_count = max(int(self.feature_count), 1)
        if not self.weights:
            self.weights = [0.0 for _ in range(self.feature_count)]
        if not self.covariance:
            self.covariance = [
                [
                    self.initial_covariance if row_idx == col_idx else 0.0
                    for col_idx in range(self.feature_count)
                ]
                for row_idx in range(self.feature_count)
            ]

    def predict(self, features: list[float] | tuple[float, ...]) -> float:
        x_vec = self._coerce_features(features)
        return sum(weight * x_val for weight, x_val in zip(self.weights, x_vec))

    def update(self, features: list[float] | tuple[float, ...], target: float) -> float:
        x_vec = self._coerce_features(features)
        lambda_forgetting = _clamp(self.forgetting_factor, 1e-6, 1.0)
        p_x = [
            sum(
                self.covariance[row_idx][col_idx] * x_vec[col_idx]
                for col_idx in range(self.feature_count)
            )
            for row_idx in range(self.feature_count)
        ]
        innovation_denom = lambda_forgetting + sum(
            x_vec[idx] * p_x[idx] for idx in range(self.feature_count)
        )
        if innovation_denom <= 1e-12:
            return self.predict(x_vec)

        prediction = self.predict(x_vec)
        error = target - prediction
        gain = [p_x[idx] / innovation_denom for idx in range(self.feature_count)]
        self.weights = [
            self.weights[idx] + gain[idx] * error
            for idx in range(self.feature_count)
        ]

        x_p = [
            sum(
                x_vec[col_idx] * self.covariance[col_idx][row_idx]
                for col_idx in range(self.feature_count)
            )
            for row_idx in range(self.feature_count)
        ]
        ridge = max(self.ridge_coefficient, 0.0)
        self.covariance = [
            [
                (
                    self.covariance[row_idx][col_idx]
                    - gain[row_idx] * x_p[col_idx]
                )
                / lambda_forgetting
                + (ridge if row_idx == col_idx else 0.0)
                for col_idx in range(self.feature_count)
            ]
            for row_idx in range(self.feature_count)
        ]
        self.samples_seen += 1
        return prediction

    def prediction_variance(self, features: list[float] | tuple[float, ...]) -> float:
        """x^T P x — the posterior variance of the prediction at this input.

        Large when the model is uncertain (novel direction or insufficient data).
        Small when the model has converged on this kind of input.
        Normalized by initial_covariance so it lives roughly in [0, 1] early on
        and shrinks toward 0 as the model converges.
        """
        x_vec = self._coerce_features(features)
        p_x = [
            sum(
                self.covariance[row_idx][col_idx] * x_vec[col_idx]
                for col_idx in range(self.feature_count)
            )
            for row_idx in range(self.feature_count)
        ]
        raw_var = sum(x_vec[idx] * p_x[idx] for idx in range(self.feature_count))
        return _clamp(raw_var / max(self.initial_covariance, 1e-9), 0.0, 1.0)

    def cosine_similarity_to_weights(
        self, features: list[float] | tuple[float, ...]
    ) -> float:
        """Cosine similarity between current feature vector and learned weight vector.

        High (→1) when the current market microstructure is aligned with the
        regime the model was trained on.  Low (→0 or negative) when it is in a
        novel or opposite regime.  Returns 0.0 before the model has any weight
        signal (all-zero weights).
        """
        x_vec = self._coerce_features(features)
        x_norm_sq = sum(v * v for v in x_vec)
        w_norm_sq = sum(w * w for w in self.weights)
        denom = (x_norm_sq * w_norm_sq) ** 0.5
        if denom < 1e-12:
            return 0.0
        dot = sum(x_vec[i] * self.weights[i] for i in range(self.feature_count))
        return _clamp(dot / denom, -1.0, 1.0)

    def _coerce_features(
        self,
        features: list[float] | tuple[float, ...],
    ) -> list[float]:
        x_vec = [float(value) for value in features[: self.feature_count]]
        if len(x_vec) < self.feature_count:
            x_vec.extend(0.0 for _ in range(self.feature_count - len(x_vec)))
        return x_vec
