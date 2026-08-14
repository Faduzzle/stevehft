from __future__ import annotations

import math
from dataclasses import dataclass, field


STRATEGY_SLEEVES: tuple[str, ...] = (
    "BASE_MM",
    "LEAD_LAG",
    "PAIR_SPREAD",
    "INV_HEDGE",
    "TAKER_EXIT",
    "NOISE_FADE",
    "CASH",  # do-nothing sleeve: zero signal, zero variance; hedges with cash
)


@dataclass(slots=True)
class StrategySleeveWeights:
    base_mm: float = 1.0
    lead_lag: float = 0.0
    pair_spread: float = 0.0
    inv_hedge: float = 0.0
    taker_exit: float = 0.0
    noise_fade: float = 0.0
    cash: float = 0.0  # do-nothing sleeve

    def as_dict(self) -> dict[str, float]:
        return {
            "BASE_MM": self.base_mm,
            "LEAD_LAG": self.lead_lag,
            "PAIR_SPREAD": self.pair_spread,
            "INV_HEDGE": self.inv_hedge,
            "TAKER_EXIT": self.taker_exit,
            "NOISE_FADE": self.noise_fade,
            "CASH": self.cash,
        }


@dataclass(slots=True)
class OnlineStrategyAllocatorConfig:
    learning_rate: float = 0.20
    reward_learning_rate: float = 0.35
    exploration_floor: float = 0.04
    min_base_weight: float = 0.0
    max_logit_abs: float = 8.0


@dataclass(slots=True)
class OnlineStrategyAllocator:
    config: OnlineStrategyAllocatorConfig = field(
        default_factory=OnlineStrategyAllocatorConfig
    )
    _logits: dict[str, float] = field(default_factory=dict)
    _weights: StrategySleeveWeights = field(default_factory=StrategySleeveWeights)

    def update(
        self,
        sleeve_scores: dict[str, float],
        *,
        reward_multiplier: float,
        sleeve_rewards: dict[str, float] | None = None,
    ) -> StrategySleeveWeights:
        if not self._logits:
            self._logits = {sleeve: 0.0 for sleeve in STRATEGY_SLEEVES}

        current_weights = self._weights.as_dict()
        centered_score = sum(
            current_weights.get(sleeve, 0.0)
            * _clamp(float(sleeve_scores.get(sleeve, 0.0)), -1.0, 1.0)
            for sleeve in STRATEGY_SLEEVES
        )
        reward = _clamp(reward_multiplier, 0.25, 1.75)
        learning_rate = max(self.config.learning_rate, 0.0)
        reward_learning_rate = max(self.config.reward_learning_rate, 0.0)
        logit_cap = max(self.config.max_logit_abs, 1.0)

        for sleeve in STRATEGY_SLEEVES:
            sleeve_score = _clamp(
                float(sleeve_scores.get(sleeve, 0.0)),
                -1.0,
                1.0,
            )
            sleeve_reward = _clamp(
                float((sleeve_rewards or {}).get(sleeve, 0.0)),
                -1.0,
                1.0,
            )
            advantage = (
                reward * (sleeve_score - centered_score)
                + reward_learning_rate * sleeve_reward
            )
            self._logits[sleeve] = _clamp(
                self._logits.get(sleeve, 0.0) + learning_rate * advantage,
                -logit_cap,
                logit_cap,
            )

        max_logit = max(self._logits.values(), default=0.0)
        raw_weights = {
            sleeve: math.exp(self._logits[sleeve] - max_logit)
            for sleeve in STRATEGY_SLEEVES
        }
        total_raw = sum(raw_weights.values())
        if total_raw <= 0.0:
            normalized = {
                sleeve: 1.0 / len(STRATEGY_SLEEVES)
                for sleeve in STRATEGY_SLEEVES
            }
        else:
            normalized = {
                sleeve: raw_weights[sleeve] / total_raw
                for sleeve in STRATEGY_SLEEVES
            }

        floor = _clamp(
            self.config.exploration_floor,
            0.0,
            1.0 / len(STRATEGY_SLEEVES),
        )
        mixed = {
            sleeve: (1.0 - len(STRATEGY_SLEEVES) * floor) * normalized[sleeve]
            + floor
            for sleeve in STRATEGY_SLEEVES
        }

        total_mixed = sum(mixed.values())
        if total_mixed > 0.0:
            mixed = {
                sleeve: mixed[sleeve] / total_mixed
                for sleeve in STRATEGY_SLEEVES
            }

        self._weights = StrategySleeveWeights(
            base_mm=mixed["BASE_MM"],
            lead_lag=mixed["LEAD_LAG"],
            pair_spread=mixed["PAIR_SPREAD"],
            inv_hedge=mixed["INV_HEDGE"],
            taker_exit=mixed["TAKER_EXIT"],
            noise_fade=mixed["NOISE_FADE"],
            cash=mixed["CASH"],
        )
        return self._weights

    @property
    def weights(self) -> StrategySleeveWeights:
        return self._weights


def sleeve_scale(weight: float) -> float:
    return _clamp(len(STRATEGY_SLEEVES) * weight, 0.0, 2.0)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
