"""Builds the scoring engine the live scorer and the offline evaluation share."""

from __future__ import annotations

from .engine import ScoringEngine
from .features import FeatureStore
from .model import HeuristicScorer

SCORERS = ("heuristic", "lightgbm")

HEURISTIC_BLOCK = 0.85
HEURISTIC_REVIEW = 0.5


def connect_redis(url: str):
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError('the Redis feature store needs the client: pip install -e ".[redis]"') from exc
    return redis.Redis.from_url(url, decode_responses=True)


def build_engine(
    scorer: str = "heuristic",
    block: float | None = None,
    review: float | None = None,
    amount_scale: float | None = None,
    redis_url: str | None = None,
    namespace: str = "ps",
) -> ScoringEngine:
    """`amount_scale` only matters to the LightGBM scorer: None keeps the simulator's calibration, 1.0 means
    the amounts are already in PaySim's units (a replay of the real log)."""
    if scorer == "heuristic":
        if redis_url:
            raise ValueError("the heuristic scorer keeps its state in memory; FEATURE_STORE=redis needs SCORER=lightgbm")
        return ScoringEngine(
            FeatureStore(),
            HeuristicScorer(),
            HEURISTIC_BLOCK if block is None else block,
            HEURISTIC_REVIEW if review is None else review,
        )
    if scorer == "lightgbm":
        from .lgbm import load_lightgbm_engine

        extra = {} if amount_scale is None else {"amount_scale": amount_scale}
        if redis_url:
            extra["redis_client"] = connect_redis(redis_url)
            extra["namespace"] = namespace
        return load_lightgbm_engine(block=block, review=review, **extra)
    raise ValueError(f"unknown scorer {scorer!r}; choose one of {SCORERS}")
