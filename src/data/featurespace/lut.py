from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class LookupPoint:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class PiecewiseLinearLut:
    points: tuple[LookupPoint, ...]
    x_values: tuple[float, ...] = field(init=False, repr=False)

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[float, float]]) -> PiecewiseLinearLut:
        points = tuple(LookupPoint(float(x), float(y)) for x, y in pairs)
        if len(points) < 2:
            raise ValueError("PiecewiseLinearLut requires at least two points")
        for left, right in zip(points, points[1:]):
            if right.x <= left.x:
                raise ValueError("PiecewiseLinearLut x values must be strictly increasing")
        return cls(points=points)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "x_values",
            tuple(point.x for point in self.points),
        )

    def evaluate(self, x: float) -> float:
        query = float(x)
        if query <= self.points[0].x:
            return self.points[0].y
        if query >= self.points[-1].x:
            return self.points[-1].y

        right_index = bisect_right(self.x_values, query)
        left = self.points[right_index - 1]
        right = self.points[right_index]
        span = right.x - left.x
        if span <= 0.0:
            return right.y
        weight = (query - left.x) / span
        return (1.0 - weight) * left.y + weight * right.y


@dataclass(slots=True)
class OnlineLutEstimator:
    """Incrementally refines a piecewise-linear LUT from live (x, y) observations.

    Maintains a per-bucket exponential weighted moving average of y values.
    The x breakpoints are fixed at construction (same grid as the static LUT).
    After `min_samples_per_bucket` observations have hit each bucket, the
    live estimate is considered warm and `DualLut` blends it in.
    """

    x_breakpoints: tuple[float, ...]
    alpha: float = 0.05
    min_samples_per_bucket: int = 30
    _bucket_y: list[float] = field(init=False, repr=False)
    _bucket_count: list[int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        n = max(len(self.x_breakpoints), 1)
        self._bucket_y = [0.0] * n
        self._bucket_count = [0] * n

    def update(self, x: float, y: float) -> None:
        idx = self._bucket_index(float(x))
        if self._bucket_count[idx] == 0:
            self._bucket_y[idx] = float(y)
        else:
            self._bucket_y[idx] = (1.0 - self.alpha) * self._bucket_y[idx] + self.alpha * float(y)
        self._bucket_count[idx] += 1

    @property
    def warmup_weight(self) -> float:
        """0.0 when cold, 1.0 when every bucket has >= min_samples_per_bucket."""
        if not self._bucket_count:
            return 0.0
        min_count = min(self._bucket_count)
        return min(float(min_count) / max(self.min_samples_per_bucket, 1), 1.0)

    def to_lut(self, static_lut: PiecewiseLinearLut) -> PiecewiseLinearLut:
        """Build a PiecewiseLinearLut from live estimates, falling back to static."""
        pairs: list[tuple[float, float]] = []
        for idx, x in enumerate(self.x_breakpoints):
            if self._bucket_count[idx] > 0:
                pairs.append((x, self._bucket_y[idx]))
            else:
                pairs.append((x, static_lut.evaluate(x)))
        if len(pairs) < 2:
            return static_lut
        return PiecewiseLinearLut.from_pairs(pairs)

    def _bucket_index(self, x: float) -> int:
        xs = self.x_breakpoints
        for i in range(len(xs) - 1, -1, -1):
            if x >= xs[i]:
                return i
        return 0


@dataclass(slots=True)
class DualLut:
    """Blends a static backtest-calibrated LUT with a live-updating online LUT.

    When the online estimator is cold (few samples), the static LUT dominates.
    As it warms up, the blend shifts toward the live estimate.

    Usage:
        dual = DualLut.from_static(static_lut)
        dual.update(toxicity_score, actual_width_multiplier)
        value = dual.evaluate(toxicity_score)
    """

    static_lut: PiecewiseLinearLut
    online: OnlineLutEstimator

    @classmethod
    def from_static(
        cls,
        static_lut: PiecewiseLinearLut,
        *,
        alpha: float = 0.05,
        min_samples_per_bucket: int = 30,
    ) -> DualLut:
        x_breakpoints = tuple(p.x for p in static_lut.points)
        online = OnlineLutEstimator(
            x_breakpoints=x_breakpoints,
            alpha=alpha,
            min_samples_per_bucket=min_samples_per_bucket,
        )
        return cls(static_lut=static_lut, online=online)

    def update(self, x: float, y: float) -> None:
        self.online.update(x, y)

    def evaluate(self, x: float) -> float:
        live_weight = self.online.warmup_weight
        if live_weight <= 0.0:
            return self.static_lut.evaluate(x)
        live_lut = self.online.to_lut(self.static_lut)
        static_val = self.static_lut.evaluate(x)
        live_val = live_lut.evaluate(x)
        return (1.0 - live_weight) * static_val + live_weight * live_val
