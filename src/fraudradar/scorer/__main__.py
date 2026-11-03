"""Consumes transactions, scores them in order, publishes decisions.

With `DATA_SOURCE=paysim` the stream is the real PaySim log. The model's history features count every
earlier row. With the default in-memory feature state, when the scorer receives row `n` for the first time (or
after a restart, a gap, or the file looping) it first reads the file up to `n` into a fresh state, then carries on.
With `FEATURE_STORE=redis` the state lives in Redis, loaded once by `paysim_warmup`, so the scorer needs no file,
starts at once, and any number of replicas can share a topic with several partitions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from pathlib import Path

import orjson
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from ..config import Settings
from ..kafka_util import start_with_retry
from ..lgbm import ARTIFACT_DIR
from ..registry import Registry
from ..schemas import Transaction
from ..serving import build_engine

log = logging.getLogger("scorer")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    paysim = settings.data_source == "paysim"
    use_redis = settings.feature_store == "redis"
    replay_csv = None  # set only when this process rebuilds feature state from the file itself
    if paysim:
        if settings.scorer != "lightgbm":
            raise SystemExit("DATA_SOURCE=paysim needs SCORER=lightgbm: the heuristic reads device and account age, which PaySim lacks")
        if not use_redis:
            from ..paysim_stream import resolve_csv, warm_up

            replay_csv = resolve_csv(settings.paysim_csv)
            log.info("replaying the real PaySim log from %s", replay_csv)

    registry = Registry(settings.model_registry) if settings.model_registry else None
    lightgbm = settings.scorer == "lightgbm"
    model_dir = (registry.current() if registry else None) or ARTIFACT_DIR  # the version in service
    scorer_id = socket.gethostname()
    monitor = None

    def install_monitor(engine):
        """Watch the payments this engine scores (LightGBM only: the heuristic has no model inputs to compare)."""
        nonlocal monitor
        if not lightgbm:
            return
        from ..drift import monitor_for

        monitor = monitor_for(model_dir, scorer=engine.scorer, window=settings.drift_window)
        engine.observer = monitor.observe

    def new_engine():
        # PaySim amounts are already in the model's units, so they are not rescaled
        engine = build_engine(
            settings.scorer, settings.block_threshold, settings.review_threshold,
            amount_scale=1.0 if paysim else None,
            redis_url=settings.redis_url if use_redis else None,
            namespace=settings.data_source,
            num_threads=settings.lgbm_threads,
            directory=model_dir if lightgbm else None,
        )
        install_monitor(engine)
        return engine

    def follow_registry() -> None:
        """Serve the registry's current version if it has changed. A model that cannot be loaded, or that the
        running feature store cannot serve, is logged and ignored; the old one keeps scoring."""
        nonlocal model_dir
        wanted = registry.current()
        if wanted is None or wanted == model_dir:
            return
        try:
            from ..lgbm import LightGBMScorer

            scorer = LightGBMScorer.load(wanted)
            scorer.num_threads = settings.lgbm_threads
            engine.swap_scorer(
                scorer,
                scorer.default_block if settings.block_threshold is None else settings.block_threshold,
                scorer.default_review if settings.review_threshold is None else settings.review_threshold,
            )
        except Exception:
            log.exception("not switching to model %s; still serving %s", wanted.name, model_dir.name)
            return
        log.info("now serving model %s (was %s)", wanted.name, model_dir.name)
        model_dir = wanted
        install_monitor(engine)

    engine = new_engine()
    expected_row: int | None = None  # index of the next PaySim row the scorer expects
    log.info(
        "scorer=%s source=%s features=%s block>=%g review>=%g",
        settings.scorer, settings.data_source, settings.feature_store,
        engine.block_threshold, engine.review_threshold,
    )
    if use_redis and paysim and not engine.store.client.get("fr:paysim:warm"):
        log.warning("Redis holds no PaySim history yet (run fraudradar.paysim_warmup first); scores will be off")

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
    last_poll = last_report = window_start
    seen_stamp = registry.stamp() if registry else None
    try:
        while True:
            batch = await consumer.getmany(timeout_ms=100, max_records=1000)
            if use_redis:  # one round trip to Redis and one model call for the whole batch
                txs = [Transaction.from_bytes(msg.value) for messages in batch.values() for msg in messages]
                for decision in engine.handle_many(txs, now_ms=int(time.time() * 1000)):
                    await producer.send(settings.topic_decisions, decision.to_bytes())
                handled += len(txs)
                batch = {}
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
            now = time.monotonic()
            if registry and now - last_poll >= 5:
                last_poll = now
                if (stamp := registry.stamp()) != seen_stamp:
                    seen_stamp = stamp
                    follow_registry()
            if monitor is not None and now - last_report >= settings.drift_interval:
                last_report = now
                report = {"scorer": scorer_id, "model": model_dir.name, "ts": int(time.time() * 1000), **monitor.report()}
                await producer.send(settings.topic_drift, orjson.dumps(report))
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
