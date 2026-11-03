"""Open-loop load test of the running pipeline.

    python -m fraudradar.loadtest --rates 250,500,1000,2000 --duration 15

Publishes payments to Kafka at a fixed rate whatever the scorer does with them (open loop, so a slow scorer
shows up as growing delay instead of slowing the test down), reads the decisions back, and reports for each rate
how many payments were decided per second and how long they took, from the moment the payment is published
to the moment its decision arrives here. Run it inside the Compose network so every clock is the same one, with
the simulator and the api stopped so nothing else competes for the scorers.

Rows come from the real PaySim file (`--source paysim`, as the scorer in PaySim mode expects) or from the
synthetic generator (`--source synthetic`). The ramp stops at the first rate the scorers cannot keep up with: a
step is saturated when, after a drain period, fewer than 99% of its decisions have come back.

What the numbers are: the delay of one machine running everything (this tool, the broker, Redis and the scorers
share its cores), so they say how the pieces compare, not what a production cluster would do.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import orjson
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .config import Settings
from .kafka_util import start_with_retry
from .schemas import Transaction

log = logging.getLogger("loadtest")

TICK_S = 0.002
SATURATED_BELOW = 0.99  # share of a step's decisions that must be back after the drain
PRIME_ROWS = 200
PRIME_RATE = 50


@dataclass
class Collected:
    """Decisions as they arrive: (publish time ms, scored time ms, arrival time ms)."""

    rows: list[tuple[int, int, int]] = field(default_factory=list)


def percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return float("nan")
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def summarize_step(
    rate: int, sent: int, window_ms: tuple[int, int], arrivals: list[tuple[int, int, int]], send_seconds: float
) -> dict:
    """Numbers for one step from the decisions whose publish time falls in its window."""
    start, end = window_ms
    mine = [a for a in arrivals if start <= a[0] < end]
    e2e = sorted(a[2] - a[0] for a in mine)
    to_decision = sorted(a[1] - a[0] for a in mine)
    in_window = sum(1 for a in mine if a[2] <= end)  # decided before the step ended
    duration = (end - start) / 1000
    return {
        "target_rate": rate,
        "sent": sent,
        "sent_rate": round(sent / send_seconds, 1) if send_seconds else 0.0,
        "decided": len(mine),
        "lost": sent - len(mine),
        "decided_in_window_rate": round(in_window / duration, 1),
        "e2e_ms": {q: round(percentile(e2e, v), 1) for q, v in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99), ("p999", 0.999))}
        | {"max": round(e2e[-1], 1) if e2e else float("nan")},
        "to_decision_ms": {q: round(percentile(to_decision, v), 1) for q, v in (("p50", 0.5), ("p99", 0.99))},
        "saturated": len(mine) < SATURATED_BELOW * sent,
    }


def build_rows(source: str, total: int, csv: str | None, start_row: int | None, start_step: int | None) -> tuple[list[Transaction], int]:
    """`total` payments to send and the file row index the next unused row has (0 for the synthetic source)."""
    if source == "synthetic":
        from .generator import TransactionGenerator

        gen = TransactionGenerator(seed=11)
        return [gen.legit(0) for _ in range(total)], 0
    from .paysim_stream import default_start_step, first_index_of_step, resolve_csv
    from .replay import transactions

    path = resolve_csv(csv or "/data")
    begin = start_row if start_row is not None else first_index_of_step(
        path, start_step if start_step is not None else default_start_step()
    )
    rows = list(transactions(path, limit=begin + total, start=begin))
    return rows, begin + len(rows)


async def run(args: argparse.Namespace) -> dict:
    settings = Settings.from_env()
    rates = [int(r) for r in args.rates.split(",")]
    total = PRIME_ROWS + sum(r * args.duration for r in rates)

    redis_client = None
    start_row = args.start_row
    if args.redis:
        from .paysim_warmup import progress_key
        from .serving import connect_redis

        redis_client = connect_redis(args.redis)
        if start_row is None:
            start_row = int(redis_client.get(progress_key()) or 0) or None
    log.info("reading %d payments from the %s source", total, args.source)
    rows, next_row = build_rows(args.source, total, os.environ.get("PAYSIM_CSV"), start_row, args.start_step)
    if len(rows) < total:
        raise SystemExit(f"the source has only {len(rows)} payments left, {total} are needed; lower the rates or the duration")

    collected = Collected()
    consumer = await start_with_retry(
        lambda: AIOKafkaConsumer(
            settings.topic_decisions,
            bootstrap_servers=settings.bootstrap,
            group_id=None,
            auto_offset_reset="latest",
            max_poll_records=2000,
        )
    )
    producer = await start_with_retry(
        lambda: AIOKafkaProducer(bootstrap_servers=settings.bootstrap, linger_ms=1)
    )

    async def collect() -> None:
        async for msg in consumer:
            now = int(time.time() * 1000)
            d = orjson.loads(msg.value)
            collected.rows.append((d["ts"], d["scored_ts"], now))

    reader = asyncio.create_task(collect())
    cursor = 0

    async def publish(n_msgs: int, rate: float) -> tuple[int, float, int, int]:
        """Send `n_msgs` payments at `rate` per second. Returns (sent, seconds, first ts, end ts)."""
        nonlocal cursor
        begin = time.perf_counter()
        first_ts = int(time.time() * 1000)
        sent = 0
        while sent < n_msgs:
            due = min(n_msgs, int((time.perf_counter() - begin) * rate) + 1)
            while sent < due:
                tx = rows[cursor]
                cursor += 1
                tx.ts = int(time.time() * 1000)
                await producer.send(settings.topic_transactions, tx.to_bytes(), key=tx.src.encode())
                sent += 1
            await asyncio.sleep(TICK_S)
        seconds = time.perf_counter() - begin
        return sent, seconds, first_ts, int(time.time() * 1000)

    async def wait_for(count_at_least: int, since_ms: int, until_s: float) -> int:
        deadline = time.monotonic() + until_s
        while time.monotonic() < deadline:
            got = sum(1 for a in collected.rows if a[0] >= since_ms)
            if got >= count_at_least:
                return got
            await asyncio.sleep(0.25)
        return sum(1 for a in collected.rows if a[0] >= since_ms)

    steps: list[dict] = []
    try:
        # Prime: the first payments make a scorer that rebuilds its state from the file do so; do not time that.
        log.info("priming with %d payments (a scorer may be loading its state; this can take a minute or two)", PRIME_ROWS)
        sent, _, first_ts, _ = await publish(PRIME_ROWS, PRIME_RATE)
        got = await wait_for(int(0.9 * sent), first_ts, args.prime_timeout)
        if got < int(0.9 * sent):
            raise SystemExit(f"the scorers did not answer the priming payments ({got} of {sent}); is the stack up?")
        await asyncio.sleep(2)

        for rate in rates:
            n_msgs = rate * args.duration
            log.info("step: %d payments/s for %d s", rate, args.duration)
            sent, seconds, first_ts, end_ts = await publish(n_msgs, rate)
            await wait_for(int(SATURATED_BELOW * sent), first_ts, args.drain)
            await asyncio.sleep(0.5)
            step = summarize_step(rate, sent, (first_ts, end_ts), collected.rows, seconds)
            step["window_epoch_ms"] = [first_ts, end_ts]
            steps.append(step)
            log.info(
                "  decided %d/%d, %.0f/s in the window, e2e p50 %.0f ms p99 %.0f ms%s",
                step["decided"], sent, step["decided_in_window_rate"], step["e2e_ms"]["p50"], step["e2e_ms"]["p99"],
                "  SATURATED" if step["saturated"] else "",
            )
            if step["saturated"]:
                log.info("saturated: stopping the ramp here")
                break
            await asyncio.sleep(args.pause)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await consumer.stop()
        await producer.stop()
        if redis_client is not None and args.source == "paysim":
            from .paysim_warmup import progress_key

            redis_client.set(progress_key(), next_row - (len(rows) - cursor))  # rows used, so the next run carries on after them

    return {"source": args.source, "duration_s": args.duration, "rates": rates, "steps": steps}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rates", default="250,500,1000,2000", help="payments per second, one step each")
    parser.add_argument("--duration", type=int, default=15, help="seconds per step")
    parser.add_argument("--source", choices=("paysim", "synthetic"), default="paysim")
    parser.add_argument("--redis", help="Redis URL; the rows continue after, and advance, the demo's saved position")
    parser.add_argument("--start-row", type=int, help="first PaySim row to send (default: the demo's start, or Redis's position)")
    parser.add_argument("--start-step", type=int)
    parser.add_argument("--drain", type=float, default=20.0, help="seconds to wait for a step's decisions after it ends")
    parser.add_argument("--pause", type=float, default=2.0, help="seconds between steps")
    parser.add_argument("--prime-timeout", type=float, default=240.0)
    parser.add_argument("--out", help="write the results here as JSON")
    args = parser.parse_args()

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(run(args))
    print(json.dumps(result, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
