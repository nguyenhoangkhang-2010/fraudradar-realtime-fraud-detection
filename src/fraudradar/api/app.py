"""HTTP + WebSocket gateway for the command-center UI."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

import orjson
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import geo
from ..config import Settings
from ..kafka_util import start_with_retry
from .metrics import Metrics

log = logging.getLogger("api")

STATIC_DIR = Path(__file__).parent / "static"
FLUSH_INTERVAL_S = 0.1
MAX_ALLOWED_PER_TICK = 60  # keep the browser light at high throughput; every non-allow is sent
DRIFT_STALE_S = 60  # a scorer that has not reported for this long is not counted
HEALTH_RANK = {"warming up": 0, "ok": 1, "watch": 2, "drift": 3}


class AttackRequest(BaseModel):
    size: int = Field(14, ge=3, le=60)


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.metrics = Metrics()
        self._pending: list[dict] = []
        self.drift: dict[str, dict] = {}  # latest drift report of each scorer replica

    def ingest_drift(self, report: dict) -> None:
        self.drift[report["scorer"]] = report

    def model_health(self) -> dict | None:
        """One line for the UI: the worst status among the scorers that reported lately, and the models they serve."""
        now = time.time() * 1000
        live = [r for r in self.drift.values() if now - r["ts"] < DRIFT_STALE_S * 1000]
        if not live:
            return None
        worst = max(live, key=lambda r: HEALTH_RANK.get(r["status"], 0))
        return {
            "status": worst["status"],
            "reasons": worst.get("reasons", []),
            "models": sorted({r["model"] for r in live}),
            "scorers": len(live),
        }

    def ingest(self, d: dict) -> None:
        self.metrics.ingest(d)
        if self.clients:
            self._pending.append(d)

    def _compact(self, items: list[dict]) -> list[dict]:
        flagged = [d for d in items if d["action"] != "allow"]
        allowed = [d for d in items if d["action"] == "allow"]
        if len(allowed) > MAX_ALLOWED_PER_TICK:
            allowed = random.sample(allowed, MAX_ALLOWED_PER_TICK)
        return [
            {
                "src": d["src"],
                "dst": d["dst"],
                "amount": d["amount"],
                "src_city": d["src_city"],
                "dst_city": d["dst_city"],
                "src_district": d.get("src_district", -1),
                "dst_district": d.get("dst_district", -1),
                "score": d["score"],
                "action": d["action"],
                "reasons": d["reasons"],
                "ts": d["ts"],
            }
            for d in flagged + allowed
        ]

    async def flush(self) -> None:
        items, self._pending = self._pending, []
        if not self.clients:
            return
        payload = orjson.dumps(
            {"type": "tick", "items": self._compact(items), "stats": self.metrics.snapshot(), "model": self.model_health()}
        ).decode()
        results = await asyncio.gather(
            *(ws.send_text(payload) for ws in list(self.clients)), return_exceptions=True
        )
        for ws, res in zip(list(self.clients), results):
            if isinstance(res, Exception):
                self.clients.discard(ws)


async def _consume_decisions(consumer: AIOKafkaConsumer, hub: Hub) -> None:
    async for msg in consumer:
        try:
            hub.ingest(orjson.loads(msg.value))
        except Exception:
            log.exception("bad decision message")


async def _consume_drift(consumer: AIOKafkaConsumer, hub: Hub) -> None:
    async for msg in consumer:
        try:
            hub.ingest_drift(orjson.loads(msg.value))
        except Exception:
            log.exception("bad drift message")


async def _flush_loop(hub: Hub) -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        await hub.flush()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    hub = Hub()
    producer = await start_with_retry(lambda: AIOKafkaProducer(bootstrap_servers=settings.bootstrap))
    consumer = await start_with_retry(
        lambda: AIOKafkaConsumer(
            settings.topic_decisions,
            bootstrap_servers=settings.bootstrap,
            group_id=None,
            auto_offset_reset="latest",
        )
    )
    drift_consumer = await start_with_retry(
        lambda: AIOKafkaConsumer(
            settings.topic_drift,
            bootstrap_servers=settings.bootstrap,
            group_id=None,
            auto_offset_reset="latest",
        )
    )
    app.state.hub = hub
    app.state.producer = producer
    app.state.settings = settings
    tasks = [
        asyncio.create_task(_consume_decisions(consumer, hub)),
        asyncio.create_task(_consume_drift(drift_consumer, hub)),
        asyncio.create_task(_flush_loop(hub)),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await consumer.stop()
        await drift_consumer.stop()
        await producer.stop()


app = FastAPI(title="FraudRadar", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)  # the GeoJSON files are several MB uncompressed


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "source": app.state.settings.data_source}


@app.get("/api/cities")
async def cities() -> list[dict]:
    return geo.as_dicts()


@app.get("/api/stats")
async def stats() -> dict:
    return app.state.hub.metrics.snapshot()


@app.get("/api/drift")
async def drift() -> dict:
    """The latest drift report of every scorer replica: per-feature PSI, flag rate, and which model it is serving."""
    hub: Hub = app.state.hub
    return {"summary": hub.model_health(), "scorers": list(hub.drift.values())}


@app.post("/api/attack")
async def attack(req: AttackRequest) -> dict:
    cmd = orjson.dumps({"type": "attack", "size": req.size})
    await app.state.producer.send_and_wait(app.state.settings.topic_control, cmd)
    return {"ok": True, "size": req.size}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub: Hub = app.state.hub
    hub.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # clients send nothing; this detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


app.mount("/data", StaticFiles(directory=geo.DATA_DIR), name="geodata")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
