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
    data_source: str  # "synthetic" or "paysim" (replay the real log)
    feature_store: str  # "memory" (in the scorer process) or "redis" (shared, survives restarts)
    redis_url: str
    lgbm_threads: int  # threads LightGBM may use per batch; 0 = all cores
    paysim_csv: str  # the PaySim file, or the folder that holds it
    paysim_start_step: int | None  # None: the first hour after the model's validation part
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
            data_source=env("DATA_SOURCE", "synthetic"),
            feature_store=env("FEATURE_STORE", "memory"),
            redis_url=env("REDIS_URL", "redis://redis:6379/0"),
            lgbm_threads=int(env("LGBM_THREADS", "1")),
            paysim_csv=env("PAYSIM_CSV", "/data"),
            paysim_start_step=int(env("PAYSIM_START_STEP")) if env("PAYSIM_START_STEP") else None,
            block_threshold=float(env("BLOCK_THRESHOLD")) if env("BLOCK_THRESHOLD") else None,
            review_threshold=float(env("REVIEW_THRESHOLD")) if env("REVIEW_THRESHOLD") else None,
        )
