"""Publishes synthetic transactions to Kafka and reacts to control commands.

With `DATA_SOURCE=paysim` the legitimate traffic is the rows of the real PaySim file (see `paysim_stream`).

Control commands (topic `control`, JSON):
    {"type": "attack", "size": 14}   launch a fraud ring
    {"type": "rate", "tps": 300}     change the legitimate traffic rate
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import os
import time
from dataclasses import dataclass

import orjson
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from ..config import Settings
from ..generator import TransactionGenerator
from ..kafka_util import start_with_retry
from ..schemas import Transaction

log = logging.getLogger("simulator")

TICK_S = 0.02


@dataclass
class _State:
    tps: float


async def _control_loop(
    consumer: AIOKafkaConsumer,
    gen: TransactionGenerator,
    scheduled: list,
    state: _State,
) -> None:
    seq = 0
    async for msg in consumer:
        try:
            cmd = orjson.loads(msg.value)
        except orjson.JSONDecodeError:
            continue
        now_ms = int(time.time() * 1000)
        if cmd.get("type") == "attack":
            size = max(3, min(int(cmd.get("size", 14)), 60))
            txs = gen.ring(now_ms, size=size)
            for tx in txs:
                seq += 1
                heapq.heappush(scheduled, (tx.ts, seq, tx))
            log.info("attack launched: %s, %d accounts, %d transactions", txs[0].truth, size, len(txs))
        elif cmd.get("type") == "rate":
            state.tps = max(1.0, min(float(cmd.get("tps", state.tps)), 5000.0))
            log.info("rate set to %.0f tps", state.tps)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    if settings.data_source == "paysim":
        from ..paysim_stream import PaysimSource

        gen = PaysimSource(settings.paysim_csv, settings.paysim_start_step, seed=settings.seed)
        log.info(
            "streaming the real PaySim log from %s, hour %d (row %d)",
            gen.csv, gen.start_step, gen.start_index,
        )
    else:
        gen = TransactionGenerator(seed=settings.seed, n_accounts=settings.n_accounts)
    state = _State(tps=settings.tps)
    scheduled: list = []

    producer = await start_with_retry(
        lambda: AIOKafkaProducer(bootstrap_servers=settings.bootstrap, linger_ms=5)
    )
    consumer = await start_with_retry(
        lambda: AIOKafkaConsumer(
            settings.topic_control,
            bootstrap_servers=settings.bootstrap,
            group_id=None,
            auto_offset_reset="latest",
        )
    )
    control = asyncio.create_task(_control_loop(consumer, gen, scheduled, state))
    log.info("simulator running at %.0f tps -> %s", state.tps, settings.bootstrap)

    async def publish(tx: Transaction) -> None:
        await producer.send(settings.topic_transactions, tx.to_bytes(), key=tx.src.encode())

    try:
        credit = 0.0
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            credit += state.tps * (now - last)
            last = now
            now_ms = int(time.time() * 1000)
            n, credit = int(credit), credit - int(credit)
            for _ in range(n):
                await publish(gen.legit(now_ms))
            while scheduled and scheduled[0][0] <= now_ms:
                await publish(heapq.heappop(scheduled)[2])
    finally:
        control.cancel()
        await asyncio.gather(control, return_exceptions=True)
        await consumer.stop()
        await producer.stop()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
