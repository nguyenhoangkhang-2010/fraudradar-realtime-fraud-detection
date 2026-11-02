"""The real PaySim log as the demo's live traffic.

With `DATA_SOURCE=paysim` the simulator publishes the rows of the PaySim file, in file order, instead of
synthetic payments. It starts at the first hour after the validation part (the model's metadata says
which), so every payment shown is one the model never trained or tuned on, and it loops back to the start
when the file's test part ends.

What is real and what is not:

* Real: type, amount, both balances, the merchant flag, the hour counter and the fraud label of every row.
* Not in PaySim, so made up for the map: where an account is. `Placement` hashes the account name to one
  of the active places (weighted like the simulator's, so a quarter land in Viet Nam). The same account is
  always in the same place, but the place means nothing.
* Injected: the **Launch attack** button. It adds PaySim-style fraud (a victim account swept into a fresh
  account that then cashes out) labelled `attack-NNN`; it is not from the file.

The scorer needs the history of every earlier row, so when it receives row `n` it first reads the
file up to `n` (`warm_up`). That makes it safe to restart either side: it resynchronises from the file.
"""

from __future__ import annotations

import bisect
import itertools
import json
import random
import zlib
from collections.abc import Iterator
from pathlib import Path

from . import geo
from .lgbm import ARTIFACT_DIR, META_FILE
from .replay import UNKNOWN_AGE_DAYS, transactions
from .schemas import Transaction

FRAUD_MEDIAN_AMOUNT = 460_000.0  # PaySim's fraud transfers are large; this is about their median
MAX_AMOUNT = 10_000_000.0


def resolve_csv(path: str | Path) -> Path:
    """The PaySim file itself, or the first CSV in a folder."""
    p = Path(path)
    if p.is_dir():
        hits = sorted(p.glob("*.csv"))
        if not hits:
            raise FileNotFoundError(f"no CSV in {p}; put the PaySim file there (see README)")
        return hits[0]
    if not p.exists():
        raise FileNotFoundError(f"{p} not found; put the PaySim file there (see README)")
    return p


def default_start_step(directory: str | Path = ARTIFACT_DIR) -> int:
    """First hour after the model's validation part."""
    meta = json.loads((Path(directory) / META_FILE).read_text(encoding="utf-8"))
    return int(meta["split"]["valid_end_hour"]) + 1


def first_index_of_step(path: str | Path, step: int) -> int:
    """Row index (from 0, after the header) of the first row at or after `step`; the file is sorted by step."""
    with open(path, encoding="utf-8") as fh:
        if not fh.readline().startswith("step"):
            raise ValueError(f"{path} does not look like PaySim: first column is not `step`")
        n = -1
        for n, line in enumerate(fh):
            if int(line.split(",", 1)[0]) >= step:
                return n
        return n + 1


class Placement:
    """Deterministic, population-weighted place for an account name. A demo device, not data."""

    def __init__(self) -> None:
        self.places = geo.active_places()
        self._cum = list(itertools.accumulate(p["weight"] for p in self.places))

    def of(self, account: str) -> tuple[int, int]:
        """(place index, district id or -1)."""
        point = zlib.crc32(account.encode()) / 2**32 * self._cum[-1]
        city = min(bisect.bisect_right(self._cum, point), len(self.places) - 1)
        ids = self.places[city]["district_ids"]
        district = ids[zlib.crc32(b"district:" + account.encode()) % len(ids)] if ids else -1
        return city, district

    def random_place(self, rng: random.Random) -> int:
        return min(bisect.bisect_right(self._cum, rng.random() * self._cum[-1]), len(self.places) - 1)


def live_rows(path: str | Path, start: int, placement: Placement) -> Iterator[Transaction]:
    """Rows from index `start` on, ready to publish. Fraud rows are labelled by the hour they fall in."""
    for tx in transactions(path, start=start):
        if tx.truth is not None:
            tx.truth = f"paysim-h{tx.step:03d}"
        tx.src_city, tx.src_district = placement.of(tx.src)
        tx.dst_city, tx.dst_district = placement.of(tx.dst)
        yield tx


def warm_up(engine, path: str | Path, upto: int) -> int:
    """Fold rows 0..upto-1 of the file into the engine's feature state without scoring them."""
    folded = 0
    for tx in transactions(path, limit=upto):
        engine.store.update(tx)
        folded += 1
    return folded


class PaysimSource:
    """Looks like `TransactionGenerator` to the simulator: `legit(ts)` and `ring(ts, size)`."""

    def __init__(self, csv: str | Path, start_step: int | None = None, seed: int = 7) -> None:
        self.csv = resolve_csv(csv)
        self.start_step = default_start_step() if start_step is None else start_step
        self.start_index = first_index_of_step(self.csv, self.start_step)
        self.placement = Placement()
        self.rng = random.Random(seed)
        self.step = self.start_step  # the hour the stream is in, so injected payments carry it too
        self.loops = 0
        self._attacks = 0
        self._rows = live_rows(self.csv, self.start_index, self.placement)

    def legit(self, ts: int) -> Transaction:
        """The next row of the file, stamped with the wall-clock time it is published at."""
        try:
            tx = next(self._rows)
        except StopIteration:
            self.loops += 1
            self._rows = live_rows(self.csv, self.start_index, self.placement)
            tx = next(self._rows)
        tx.ts = ts
        self.step = tx.step
        return tx

    def ring(self, ts: int, size: int = 14, duration_ms: int = 6000) -> list[Transaction]:
        """`size` victim accounts each swept into a fresh account that then cashes out: PaySim's fraud pattern."""
        rng = self.rng
        self._attacks += 1
        attack_id = f"attack-{self._attacks:03d}"
        origins = [self.placement.random_place(rng) for _ in range(rng.randint(1, 3))]
        txs: list[Transaction] = []
        for k in range(size):
            victim = f"CATK{self._attacks:03d}V{k:02d}"
            mule = f"CATK{self._attacks:03d}M{k:02d}"
            sink = f"CATK{self._attacks:03d}S{k:02d}"
            amount = round(min(max(rng.lognormvariate(13.0, 1.0), 20_000.0), MAX_AMOUNT), 2)
            sweep_at = ts + rng.randrange(duration_ms)
            home = rng.choice(origins)
            mule_place = self.placement.random_place(rng)
            common = dict(
                src_city=home, dst_city=mule_place, device="", src_age_days=UNKNOWN_AGE_DAYS,
                truth=attack_id, step=self.step, dst_merchant=False,
            )
            txs.append(Transaction(
                tx_id=f"A{self._attacks:03d}-{k:02d}a", ts=sweep_at, src=victim, dst=mule, amount=amount,
                kind="TRANSFER", src_balance=amount, dst_balance=0.0, **common,
            ))
            txs.append(Transaction(
                tx_id=f"A{self._attacks:03d}-{k:02d}b", ts=sweep_at + rng.randrange(500, 2500), src=mule, dst=sink,
                amount=amount, kind="CASH_OUT", src_balance=amount, dst_balance=0.0,
                **{**common, "src_city": mule_place, "dst_city": self.placement.random_place(rng)},
            ))
        txs.sort(key=lambda t: t.ts)
        return txs
