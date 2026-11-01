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
    block_threshold: float
    review_threshold: float

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
            block_threshold=float(env("BLOCK_THRESHOLD", "0.85")),
            review_threshold=float(env("REVIEW_THRESHOLD", "0.5")),
        )
