"""Builds the scoring engine the live scorer and the offline evaluation share."""

from __future__ import annotations

from .engine import ScoringEngine
from .features import FeatureStore
from .model import HeuristicScorer

SCORERS = ("heuristic", "lightgbm")

HEURISTIC_BLOCK = 0.85
HEURISTIC_REVIEW = 0.5


def build_engine(
    scorer: str = "heuristic",
    block: float | None = None,
    review: float | None = None,
    amount_scale: float | None = None,
) -> ScoringEngine:
    """`amount_scale` only matters to the LightGBM scorer: None keeps the simulator's calibration, 1.0 means
    the amounts are already in PaySim's units (a replay of the real log)."""
    if scorer == "heuristic":
        return ScoringEngine(
            FeatureStore(),
            HeuristicScorer(),
            HEURISTIC_BLOCK if block is None else block,
            HEURISTIC_REVIEW if review is None else review,
        )
    if scorer == "lightgbm":
        from .lgbm import load_lightgbm_engine

        if amount_scale is None:
            return load_lightgbm_engine(block=block, review=review)
        return load_lightgbm_engine(block=block, review=review, amount_scale=amount_scale)
    raise ValueError(f"unknown scorer {scorer!r}; choose one of {SCORERS}")
