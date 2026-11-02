from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    bootstrap: str
    topic_transactions: str
    topic_decisions: str
    topic_control: str
    tps: float
    n_accounts: int
    seed: int
    scorer: str  # "heuristic" or "lightgbm"
    block_threshold: float | None  # None: the scorer's own default
    review_threshold: float | None

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            bootstrap=env("KAFKA_BOOTSTRAP", "localhost:19092"),
            topic_transactions=env("TOPIC_TRANSACTIONS", "transactions"),
            topic_decisions=env("TOPIC_DECISIONS", "decisions"),
            topic_control=env("TOPIC_CONTROL", "control"),
            tps=float(env("SIM_TPS", "120")),
            n_accounts=int(env("SIM_ACCOUNTS", "2000")),
            seed=int(env("SIM_SEED", "7")),
            scorer=env("SCORER", "heuristic"),
            block_threshold=float(env("BLOCK_THRESHOLD")) if env("BLOCK_THRESHOLD") else None,
            review_threshold=float(env("REVIEW_THRESHOLD")) if env("REVIEW_THRESHOLD") else None,
        )
