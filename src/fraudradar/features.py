"""Streaming features with point-in-time correctness.

`FeatureStore.features(tx)` is read-only and describes the world *before* `tx`;
`FeatureStore.update(tx)` folds `tx` into the state afterwards. The scoring
engine always calls them in that order, so a transaction never sees itself.

State is in-process memory. That is the simplest thing that works for one
scorer replica; moving it to Redis is the first step toward horizontal scale.
Events are assumed to arrive roughly in event-time order.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field

from .schemas import Transaction

WINDOW_MS = 60_000
YOUNG_DAYS = 7.0
FLAG_TTL_MS = 10 * 60_000
MIN_HISTORY = 5
PRIOR_MU = math.log1p(60.0)
PRIOR_SIGMA = 1.0
MIN_SIGMA = 0.3
MAX_Z = 8.0


@dataclass(frozen=True, slots=True)
class Features:
    new_account: float  # 1.0 if the sender account is younger than YOUNG_DAYS
    amount_z: float  # how far above this account's usual amount (log scale), >= 0
    velocity: float  # sender's transactions in the last window
    shared_device: float  # OTHER accounts seen on this device
    young_fan_in: float  # distinct young senders paying the same recipient in the window
    flagged_linked: float  # recently blocked accounts among device mates, co-senders and the sender

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class _Account:
    n: int = 0
    mean: float = 0.0  # Welford running mean of log1p(amount)
    m2: float = 0.0
    recent: deque = field(default_factory=deque)  # timestamps inside the window


class FeatureStore:
    def __init__(self, window_ms: int = WINDOW_MS) -> None:
        self.window_ms = window_ms
        self._accounts: dict[str, _Account] = {}
        self._device_accounts: dict[str, set[str]] = defaultdict(set)
        self._young_inbound: dict[str, deque] = defaultdict(deque)  # dst -> (ts, young src)
        self._flagged: dict[str, int] = {}  # account -> flagged until (event ms)

    def features(self, tx: Transaction) -> Features:
        cutoff = tx.ts - self.window_ms
        acct = self._accounts.get(tx.src)

        velocity = sum(1 for t in acct.recent if t > cutoff) if acct else 0

        lamt = math.log1p(tx.amount)
        if acct and acct.n >= MIN_HISTORY:
            mu = acct.mean
            sigma = max(math.sqrt(acct.m2 / (acct.n - 1)), MIN_SIGMA)
        else:
            mu, sigma = PRIOR_MU, PRIOR_SIGMA
        amount_z = min(max((lamt - mu) / sigma, 0.0), MAX_Z)

        mates = self._device_accounts.get(tx.device, set())
        shared_device = len(mates) - (1 if tx.src in mates else 0)

        co_senders = {
            s for t, s in self._young_inbound.get(tx.dst, ()) if t > cutoff and s != tx.src
        }
        linked = set(mates) | co_senders | {tx.src}
        flagged_linked = sum(1 for a in linked if self._flagged.get(a, 0) > tx.ts)

        return Features(
            new_account=1.0 if tx.src_age_days < YOUNG_DAYS else 0.0,
            amount_z=amount_z,
            velocity=float(velocity),
            shared_device=float(shared_device),
            young_fan_in=float(len(co_senders)),
            flagged_linked=float(flagged_linked),
        )

    def update(self, tx: Transaction) -> None:
        acct = self._accounts.get(tx.src)
        if acct is None:
            acct = self._accounts[tx.src] = _Account()
        x = math.log1p(tx.amount)
        acct.n += 1
        delta = x - acct.mean
        acct.mean += delta / acct.n
        acct.m2 += delta * (x - acct.mean)

        cutoff = tx.ts - self.window_ms
        while acct.recent and acct.recent[0] <= cutoff:
            acct.recent.popleft()
        acct.recent.append(tx.ts)

        self._device_accounts[tx.device].add(tx.src)

        if tx.src_age_days < YOUNG_DAYS:
            q = self._young_inbound[tx.dst]
            while q and q[0][0] <= cutoff:
                q.popleft()
            q.append((tx.ts, tx.src))

    def flag(self, account: str, ts: int) -> None:
        self._flagged[account] = ts + FLAG_TTL_MS
