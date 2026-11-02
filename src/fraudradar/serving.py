"""Builds the scoring engine the live scorer and the offline evaluation share."""

from __future__ import annotations

from .engine import ScoringEngine
from .features import FeatureStore
from .model import HeuristicScorer

SCORERS = ("heuristic", "lightgbm")

HEURISTIC_BLOCK = 0.85
HEURISTIC_REVIEW = 0.5


def build_engine(
    scorer: str = "heuristic", block: float | None = None, review: float | None = None
) -> ScoringEngine:
    if scorer == "heuristic":
        return ScoringEngine(
            FeatureStore(),
            HeuristicScorer(),
            HEURISTIC_BLOCK if block is None else block,
            HEURISTIC_REVIEW if review is None else review,
        )
    if scorer == "lightgbm":
        from .lgbm import load_lightgbm_engine

        return load_lightgbm_engine(block=block, review=review)
    raise ValueError(f"unknown scorer {scorer!r}; choose one of {SCORERS}")
