from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .model import Scorer, decide
from .schemas import Decision, Transaction


class FeatureSource(Protocol):
    """What the engine needs from a feature store; what `features` returns is the scorer's business."""

    def features(self, tx: Transaction) -> Any: ...
    def update(self, tx: Transaction) -> None: ...
    def flag(self, account: str, ts: int) -> None: ...


class ScoringEngine:
    """Features -> score -> action, in strict point-in-time order."""

    def __init__(
        self,
        store: FeatureSource,
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
        decision = self._decision(tx, score, reasons, now_ms)
        if decision.action == "block":
            self.store.flag(tx.src, tx.ts)
        self.store.update(tx)  # only now does the transaction enter the state
        return decision

    def handle_many(self, txs: Sequence[Transaction], now_ms: int) -> list[Decision]:
        """The same decisions as `handle` one by one, in one round trip to the store and one model call.

        Only for a store that can fold a batch in order (`features_and_update_many`) and a scorer that can score
        one (`score_many`); anything else is handled one payment at a time.
        """
        fused = getattr(self.store, "features_and_update_many", None)
        batch_scorer = getattr(self.scorer, "score_many", None)
        if fused is None or batch_scorer is None:
            return [self.handle(tx, now_ms) for tx in txs]
        if not txs:
            return []
        scored = batch_scorer(fused(txs))
        decisions = [self._decision(tx, score, reasons, now_ms) for tx, (score, reasons) in zip(txs, scored)]
        for tx, d in zip(txs, decisions):
            if d.action == "block":
                self.store.flag(tx.src, tx.ts)
        return decisions

    def _decision(self, tx: Transaction, score: float, reasons: list[str], now_ms: int) -> Decision:
        action = decide(score, self.block_threshold, self.review_threshold)
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
