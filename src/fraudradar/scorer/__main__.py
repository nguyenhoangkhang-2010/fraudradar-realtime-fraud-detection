"""Consumes transactions, scores them in order, publishes decisions.

With `DATA_SOURCE=paysim` the stream is the real PaySim log. The model's history features count every
earlier row, so when the scorer receives row `n` for the first time (or after a restart, a gap, or the
file looping) it first reads the file up to `n` into a fresh feature state, then carries on.
"""

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
    replay_csv = None
    if settings.data_source == "paysim":
        if settings.scorer != "lightgbm":
            raise SystemExit("DATA_SOURCE=paysim needs SCORER=lightgbm: the heuristic reads device and account age, which PaySim lacks")
        from ..paysim_stream import resolve_csv, warm_up

        replay_csv = resolve_csv(settings.paysim_csv)
        log.info("replaying the real PaySim log from %s", replay_csv)

    def new_engine():
        # PaySim amounts are already in the model's units, so they are not rescaled
        return build_engine(
            settings.scorer, settings.block_threshold, settings.review_threshold,
            amount_scale=1.0 if replay_csv else None,
        )

    engine = new_engine()
    expected_row: int | None = None  # index of the next PaySim row the scorer expects
    log.info(
        "scorer=%s source=%s block>=%g review>=%g",
        settings.scorer, settings.data_source, engine.block_threshold, engine.review_threshold,
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
                    if replay_csv and tx.tx_id.startswith("P"):  # rows of the file; injected attacks start with "A"
                        row = int(tx.tx_id[1:])
                        if row != expected_row:
                            if expected_row is not None:
                                engine = None  # free the old state before building the next
                                engine = new_engine()
                            log.info("syncing to PaySim row %d: reading the file up to there", row)
                            started = time.monotonic()
                            folded = await asyncio.to_thread(warm_up, engine, replay_csv, row)  # keeps heartbeats alive
                            log.info("synced: %d earlier rows in %.0f s", folded, time.monotonic() - started)
                        expected_row = row + 1
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
