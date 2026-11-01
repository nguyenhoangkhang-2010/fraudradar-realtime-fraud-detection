"""Offline, Kafka-free evaluation of the scoring pipeline.

    python -m fraudradar.evaluate

Replays synthetic traffic with fraud rings through the same ScoringEngine the
live scorer uses, in event time, and reports what a stream replay can honestly
measure. Latency is not measured here; that needs the real broker.
"""

from __future__ import annotations

import argparse
import json

from .engine import ScoringEngine
from .features import FeatureStore
from .generator import TransactionGenerator
from .model import HeuristicScorer


def run_offline(
    seed: int = 7,
    seconds: int = 120,
    tps: int = 150,
    ring_starts: tuple[int, ...] = (30, 70),
    ring_size: int = 14,
    block: float = 0.85,
    review: float = 0.5,
) -> dict:
    gen = TransactionGenerator(seed=seed)
    base = 1_700_000_000_000
    stream = [gen.legit(base + int(i * 1000 / tps)) for i in range(seconds * tps)]
    ring_ids: dict[str, int] = {}
    for start in ring_starts:
        txs = gen.ring(base + start * 1000, size=ring_size)
        ring_ids[txs[0].truth] = txs[0].ts
        stream.extend(txs)
    stream.sort(key=lambda t: t.ts)

    engine = ScoringEngine(FeatureStore(), HeuristicScorer(), block, review)
    legit = legit_block = legit_review = 0
    ring_tx = ring_block = ring_review = 0
    first_block: dict[str, int] = {}
    money_blocked = money_passed = 0.0
    for tx in stream:
        d = engine.handle(tx, now_ms=tx.ts)
        if tx.truth is None:
            legit += 1
            legit_block += d.action == "block"
            legit_review += d.action == "review"
        else:
            ring_tx += 1
            ring_block += d.action == "block"
            ring_review += d.action == "review"
            if d.action == "block":
                first_block.setdefault(tx.truth, tx.ts)
                money_blocked += tx.amount
            else:
                money_passed += tx.amount

    return {
        "legit_tx": legit,
        "legit_blocked": legit_block,
        "false_block_rate": legit_block / legit,
        "legit_reviewed": legit_review,
        "ring_tx": ring_tx,
        "ring_blocked": ring_block,
        "ring_block_recall": ring_block / ring_tx,
        "ring_reviewed": ring_review,
        "seconds_to_first_block": {
            rid: (first_block[rid] - ts) / 1000 if rid in first_block else None
            for rid, ts in ring_ids.items()
        },
        "fraud_money_blocked": round(money_blocked, 2),
        "fraud_money_passed": round(money_passed, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--tps", type=int, default=150)
    args = parser.parse_args()
    print(json.dumps(run_offline(seed=args.seed, seconds=args.seconds, tps=args.tps), indent=2))


if __name__ == "__main__":
    main()
