from __future__ import annotations

from .features import FeatureStore
from .model import Scorer, decide
from .schemas import Decision, Transaction


class ScoringEngine:
    """Features -> score -> action, in strict point-in-time order."""

    def __init__(
        self,
        store: FeatureStore,
        scorer: Scorer,
        block_threshold: float = 0.85,
        review_threshold: float = 0.5,
    ) -> None:
        self.store = store
        self.scorer = scorer
        self.block_threshold = block_threshold
        self.review_threshold = review_threshold

    def handle(self, tx: Transaction, now_ms: int) -> Decision:
        features = self.store.features(tx)  # state *before* this transaction
        score, reasons = self.scorer.score(features)
        action = decide(score, self.block_threshold, self.review_threshold)
        if action == "block":
            self.store.flag(tx.src, tx.ts)
        self.store.update(tx)  # only now does the transaction enter the state
        return Decision(
            tx_id=tx.tx_id,
            ts=tx.ts,
            src=tx.src,
            dst=tx.dst,
            amount=tx.amount,
            src_city=tx.src_city,
            dst_city=tx.dst_city,
            score=round(score, 4),
            action=action,
            scored_ts=now_ms,
            latency_ms=float(now_ms - tx.ts),
            reasons=reasons if action != "allow" else [],
            truth=tx.truth,
            src_district=tx.src_district,
            dst_district=tx.dst_district,
        )
