"""Scoring models.

`HeuristicScorer` is a hand-weighted logistic function over the streaming
features. The trained model (lgbm.py) has the same interface: features in,
probability and reasons out. Each scorer comes with its own feature store.
"""

from __future__ import annotations

import math
from typing import Any, Protocol

from .features import Features


class Scorer(Protocol):
    """Whatever the feature store returns in, a probability and reasons out."""

    def score(self, f: Any) -> tuple[float, list[str]]: ...


def decide(score: float, block: float, review: float) -> str:
    if score >= block:
        return "block"
    if score >= review:
        return "review"
    return "allow"


class HeuristicScorer:
    BIAS = -5.0
    EXPLAIN_MIN = 0.7  # a term must add at least this much logit to be quoted as a reason

    def _terms(self, f: Features) -> dict[str, float]:
        return {
            "new_account": 1.6 * f.new_account,
            "amount_z": 0.5 * f.amount_z,
            "velocity": 0.1 * min(f.velocity, 10.0),
            "shared_device": 0.7 * min(f.shared_device, 6.0),
            "young_fan_in": 0.8 * math.log1p(f.young_fan_in),
            "flagged_linked": 1.0 * min(f.flagged_linked, 3.0),
        }

    @staticmethod
    def _explain(name: str, f: Features) -> str:
        if name == "new_account":
            return "sender account is less than 7 days old"
        if name == "amount_z":
            return f"amount is {f.amount_z:.1f} sigma above this account's norm"
        if name == "velocity":
            return f"{int(f.velocity)} transactions from this account in the last minute"
        if name == "shared_device":
            return f"{int(f.shared_device)} other accounts used the same device"
        if name == "young_fan_in":
            return f"{int(f.young_fan_in)} other young accounts paid the same recipient in 60s"
        return f"linked to {int(f.flagged_linked)} recently blocked account(s)"

    def score(self, f: Features) -> tuple[float, list[str]]:
        terms = self._terms(f)
        logit = self.BIAS + sum(terms.values())
        p = 1.0 / (1.0 + math.exp(-logit))
        top = sorted(((v, k) for k, v in terms.items() if v >= self.EXPLAIN_MIN), reverse=True)
        return p, [self._explain(k, f) for _, k in top[:3]]
