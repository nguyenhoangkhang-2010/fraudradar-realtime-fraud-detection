"""Synthetic payment traffic: realistic-enough legitimate flow plus fraud rings.

Legitimate traffic deliberately contains the things that cause false positives
in real systems: popular merchants that receive many payments, a few brand-new
honest accounts, family accounts sharing a device, and the occasional large
purchase. A fraud ring is a burst of young accounts on a few shared devices
all paying the same mule account large amounts.

Every account lives in a place from `geo.active_places()`, weighted by population,
and, in Viet Nam's five big cities, in one of that city's districts.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from . import geo
from .schemas import Transaction


@dataclass(slots=True)
class _Profile:
    account: str
    city: int
    district: int
    mu: float  # mean of log1p(amount)
    sigma: float
    age_days: float
    device: str


class TransactionGenerator:
    def __init__(self, seed: int = 7, n_accounts: int = 2000, n_merchants: int = 100) -> None:
        self.rng = random.Random(seed)
        self.n_merchants = n_merchants
        self._seq = 0
        self._rings = 0
        self.places = geo.active_places()
        self._place_ids = list(range(len(self.places)))
        self._place_weights = [p["weight"] for p in self.places]
        self.profiles: list[_Profile] = []
        for i in range(n_accounts):
            if self.rng.random() < 0.03:
                age = self.rng.uniform(0.5, 6.0)  # honest new account
            else:
                age = 30.0 + self.rng.expovariate(1 / 700.0)
            device = f"D{i:05d}"
            if i > n_merchants and self.rng.random() < 0.06:  # family device
                device = self.profiles[self.rng.randrange(n_merchants, i)].device
            city = self._weighted_place()
            self.profiles.append(
                _Profile(
                    account=f"A{i:05d}",
                    city=city,
                    district=self._district_in(city),
                    mu=self.rng.gauss(3.8, 0.7),
                    sigma=self.rng.uniform(0.4, 0.9),
                    age_days=age,
                    device=device,
                )
            )

    def _weighted_place(self) -> int:
        return self.rng.choices(self._place_ids, weights=self._place_weights, k=1)[0]

    def _district_in(self, city: int) -> int:
        ids = self.places[city]["district_ids"]
        return self.rng.choice(ids) if ids else -1

    def _next_id(self) -> str:
        self._seq += 1
        return f"T{self._seq:09d}"

    def legit(self, ts: int) -> Transaction:
        rng = self.rng
        src = self.profiles[rng.randrange(self.n_merchants, len(self.profiles))]
        if rng.random() < 0.45:  # skewed towards a few popular merchants
            dst = self.profiles[int(self.n_merchants * rng.random() ** 2)]
        else:
            dst = self.profiles[rng.randrange(len(self.profiles))]
        if dst.account == src.account:
            dst = self.profiles[0]
        amount = max(1.0, math.expm1(rng.gauss(src.mu, src.sigma)))
        if rng.random() < 0.002:  # rare large honest purchase
            amount *= 20
        city, district = src.city, src.district
        if rng.random() < 0.05:  # traveller paying from somewhere else
            city, district = self._weighted_place(), -1
        return Transaction(
            tx_id=self._next_id(),
            ts=ts,
            src=src.account,
            dst=dst.account,
            amount=round(amount, 2),
            src_city=city,
            dst_city=dst.city,
            device=src.device,
            src_age_days=round(src.age_days, 1),
            src_district=district,
            dst_district=dst.district,
        )

    def ring(self, ts: int, size: int = 14, duration_ms: int = 6000) -> list[Transaction]:
        """A burst of `size` young accounts paying one mule, spread over `duration_ms`."""
        rng = self.rng
        self._rings += 1
        ring_id = f"ring-{self._rings:03d}"
        mule = f"M{self._rings:03d}-0"
        mule_city = self._weighted_place()
        mule_district = self._district_in(mule_city)
        devices = [f"RD{self._rings:03d}-{k}" for k in range(max(2, math.ceil(size / 5)))]
        origins = [self._weighted_place() for _ in range(rng.randint(1, 3))]
        txs: list[Transaction] = []
        for k in range(size):
            account = f"R{self._rings:03d}-{k:02d}"
            device = devices[k % len(devices)]
            city = rng.choice(origins)
            district = self._district_in(city)
            age = round(rng.uniform(0.1, 2.0), 1)
            for _ in range(rng.randint(2, 4)):
                txs.append(
                    Transaction(
                        tx_id=self._next_id(),
                        ts=ts + rng.randrange(duration_ms),
                        src=account,
                        dst=mule,
                        amount=round(max(50.0, math.expm1(rng.gauss(8.2, 0.4))), 2),
                        src_city=city,
                        dst_city=mule_city,
                        device=device,
                        src_age_days=age,
                        truth=ring_id,
                        src_district=district,
                        dst_district=mule_district,
                    )
                )
        txs.sort(key=lambda t: t.ts)
        return txs
