"""The PaySim model's history features, kept in Redis instead of the scorer's memory.

Why: with state in the process, one scorer can run, and a restart forgets everything (the demo then
rereads the whole PaySim file to rebuild it). With state in Redis any number of scorer replicas
share it, and a restarted scorer carries on at once.

What is kept: for every account that has received a payment, how many it received and their total
(`fr:<namespace>:in:<account>`, a hash with fields `n` and `sum`). That is all the model uses of the
history: `dest_prior_in` and `dest_prior_in_sum_log`. The other history columns (`orig_prior_out`,
`recv_count`, `steps_since_recv`, `amount_to_last_recv`) are never split on by the trained model, so
they are not kept and are sent as missing; the store refuses a model that does use them.

Point-in-time correctness: one Lua script reads an account's state and then adds the payment, and Redis
runs a script without interleaving, so each payment sees the state before itself even with many
replicas. A pipeline of scripts keeps its order. What does *not* hold across replicas is the order of
payments from different partitions: two payments to the same recipient that land on different replicas
are applied in whichever order they arrive, so the "before" state of one may or may not contain the other.
With one partition, or one replica, the result is exactly the in-memory store's.

Single Redis node only (a script touches one key, so it would also work in a cluster).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

import numpy as np

from .payment_features import AMOUNT_SCALE, NAN, SIM_HOUR_MS, History, PaymentFeatureStore
from .schemas import Transaction

UNTRACKED = ("orig_prior_out", "recv_count", "steps_since_recv", "amount_to_last_recv")

# Read the recipient's history, then fold the payment in. Returns the history as it was before.
FUSED = """
local n = redis.call('HGET', KEYS[1], 'n')
local sum = redis.call('HGET', KEYS[1], 'sum')
redis.call('HINCRBY', KEYS[1], 'n', 1)
redis.call('HINCRBYFLOAT', KEYS[1], 'sum', ARGV[1])
return {n or false, sum or false}
"""


def in_key(namespace: str, account: str) -> str:
    return f"fr:{namespace}:in:{account}"


class RedisPaymentFeatureStore(PaymentFeatureStore):
    def __init__(
        self,
        client,
        feature_names: Sequence[str],
        type_categories: Sequence[str],
        unused_features: Collection[str],
        namespace: str = "ps",
        amount_scale: float = AMOUNT_SCALE,
        sim_hour_ms: int = SIM_HOUR_MS,
    ) -> None:
        super().__init__(feature_names, type_categories, amount_scale, sim_hour_ms)
        needed = [n for n in UNTRACKED if n in feature_names and n not in set(unused_features)]
        if needed:
            raise ValueError(
                f"the model splits on {needed}, which the Redis store does not keep; use the in-memory store"
            )
        self.client = client
        self.namespace = namespace
        self._fused = client.register_script(FUSED)

    def _history(self, tx: Transaction) -> History:
        n, total = self.client.hmget(in_key(self.namespace, tx.dst), "n", "sum")
        return History(dst_count=float(n or 0), dst_total=float(total or 0.0))

    def assemble(self, tx: Transaction, h: History) -> dict[str, float]:
        cols = super().assemble(tx, h)
        for name in UNTRACKED:
            cols[name] = NAN
        return cols

    def update(self, tx: Transaction) -> None:
        self._fused(keys=[in_key(self.namespace, tx.dst)], args=[repr(tx.amount * self.amount_scale)])

    def features_and_update_many(self, txs: Sequence[Transaction]) -> list[np.ndarray]:
        """Features of each payment as of just before it, in order, folding every payment in; one round trip."""
        if not txs:
            return []
        pipe = self.client.pipeline(transaction=False)
        for tx in txs:
            self._fused(
                keys=[in_key(self.namespace, tx.dst)], args=[repr(tx.amount * self.amount_scale)], client=pipe
            )
        before = pipe.execute()
        rows = []
        for tx, (n, total) in zip(txs, before):
            cols = self.assemble(tx, History(dst_count=float(n or 0), dst_total=float(total or 0.0)))
            rows.append(np.array([cols[name] for name in self.feature_names], dtype=np.float64))
        return rows

    def clear(self) -> int:
        """Delete this namespace's state. Returns how many keys went."""
        removed = 0
        batch: list[str] = []
        for key in self.client.scan_iter(match=f"fr:{self.namespace}:*", count=10_000):
            batch.append(key)
            if len(batch) >= 10_000:
                removed += self.client.unlink(*batch)
                batch = []
        if batch:
            removed += self.client.unlink(*batch)
        return removed
