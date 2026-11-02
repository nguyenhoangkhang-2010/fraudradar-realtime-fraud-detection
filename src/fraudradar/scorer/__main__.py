"""Consumes transactions, scores them in order, publishes decisions."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from ..config import Settings
from ..kafka_util import start_with_retry
from ..schemas import Transaction
from ..serving import build_engine

log = logging.getLogger("scorer")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    engine = build_engine(settings.scorer, settings.block_threshold, settings.review_threshold)
    log.info(
        "scorer=%s block>=%g review>=%g",
        settings.scorer, engine.block_threshold, engine.review_threshold,
    )

    consumer = await start_with_retry(
        lambda: AIOKafkaConsumer(
            settings.topic_transactions,
            bootstrap_servers=settings.bootstrap,
            group_id="fraudradar-scorer",
            auto_offset_reset="latest",
            max_poll_records=1000,
        )
    )
    producer = await start_with_retry(
        lambda: AIOKafkaProducer(bootstrap_servers=settings.bootstrap, linger_ms=5)
    )
    log.info("scorer running: %s -> %s", settings.topic_transactions, settings.topic_decisions)

    handled = 0
    window_start = time.monotonic()
    try:
        while True:
            batch = await consumer.getmany(timeout_ms=100, max_records=1000)
            for messages in batch.values():
                for msg in messages:
                    tx = Transaction.from_bytes(msg.value)
                    decision = engine.handle(tx, now_ms=int(time.time() * 1000))
                    await producer.send(settings.topic_decisions, decision.to_bytes())
                    handled += 1
            if time.monotonic() - window_start >= 10:
                log.info("scored %.0f tx/s", handled / (time.monotonic() - window_start))
                handled, window_start = 0, time.monotonic()
    finally:
        await consumer.stop()
        await producer.stop()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
