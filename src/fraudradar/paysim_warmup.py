"""Load the history of the PaySim rows before the demo's start into Redis, once.

    python -m fraudradar.paysim_warmup

Replaces the scorer's own read of the file on every start: the work is done here, then every scorer
replica finds the state already in Redis. It is idempotent: if Redis already holds the state for this start
row it does nothing. It reads the rows before `PAYSIM_START_STEP`, adds up per recipient how many payments it
received and their total, and writes one hash per recipient.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import Settings
from .paysim_stream import default_start_step, first_index_of_step, resolve_csv
from .redis_features import in_key
from .replay import transactions

log = logging.getLogger("warmup")
NAMESPACE = "paysim"
CHUNK = 20_000


def marker_key(namespace: str = NAMESPACE) -> str:
    return f"fr:{namespace}:warm"


def progress_key(namespace: str = NAMESPACE) -> str:
    return f"fr:{namespace}:sim_row"


def warm(client, csv: str | Path, start_index: int, namespace: str = NAMESPACE, scale: float = 1.0) -> dict:
    """Make Redis hold the history of rows 0..start_index-1. Returns what it did."""
    if client.get(marker_key(namespace)) == str(start_index):
        return {"skipped": True, "start_index": start_index}
    started = time.monotonic()
    removed = 0
    batch: list[str] = []
    for key in client.scan_iter(match=f"fr:{namespace}:*", count=10_000):
        batch.append(key)
        if len(batch) >= 10_000:
            removed += client.unlink(*batch)
            batch = []
    if batch:
        removed += client.unlink(*batch)

    counts: dict[str, int] = {}
    sums: dict[str, float] = {}
    rows = 0
    for tx in transactions(csv, limit=start_index):
        counts[tx.dst] = counts.get(tx.dst, 0) + 1
        sums[tx.dst] = sums.get(tx.dst, 0.0) + tx.amount * scale
        rows += 1

    pipe = client.pipeline(transaction=False)
    pending = 0
    for dst, n in counts.items():
        pipe.hset(in_key(namespace, dst), mapping={"n": n, "sum": repr(sums[dst])})
        pending += 1
        if pending >= CHUNK:
            pipe.execute()
            pending = 0
    pipe.execute()
    client.set(progress_key(namespace), start_index)
    client.set(marker_key(namespace), start_index)  # last: a crash before this redoes the load
    return {
        "skipped": False,
        "rows": rows,
        "recipients": len(counts),
        "cleared": removed,
        "seconds": round(time.monotonic() - started, 1),
        "start_index": start_index,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    from .serving import connect_redis

    client = connect_redis(settings.redis_url)
    csv = resolve_csv(settings.paysim_csv)
    step = settings.paysim_start_step if settings.paysim_start_step is not None else default_start_step()
    start_index = first_index_of_step(csv, step)
    log.info("warming Redis with the %d PaySim rows before hour %d", start_index, step)
    log.info("done: %s", warm(client, csv, start_index))


if __name__ == "__main__":
    main()
