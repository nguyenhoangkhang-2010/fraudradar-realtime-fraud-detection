import asyncio
import json
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiokafka")
pytest.importorskip("httpx")  # the test client

from fastapi.testclient import TestClient  # noqa: E402

from fraudradar.api import app as api  # noqa: E402
from fraudradar.config import Settings  # noqa: E402


class FakeProducer:
    def __init__(self):
        self.sent = []

    async def send_and_wait(self, topic, value):
        self.sent.append((topic, json.loads(value)))


class FakeSocket:
    def __init__(self, fail=False):
        self.fail, self.received = fail, []

    async def send_text(self, text):
        if self.fail:
            raise RuntimeError("client went away")
        self.received.append(json.loads(text))


def decision(action="allow", truth=None, **extra):
    d = {"tx_id": "t", "ts": 1_000, "src": "A", "dst": "B", "amount": 10.0, "src_city": 1, "dst_city": 2,
         "score": 0.01, "action": action, "scored_ts": 1_010, "latency_ms": 10.0, "reasons": [], "truth": truth,
         "src_district": -1, "dst_district": -1}
    return {**d, **extra}


@pytest.fixture()
def hub():
    return api.Hub()


@pytest.fixture()
def client(hub, monkeypatch):
    """The real app, without its Kafka lifespan: the state the lifespan would have set is set here."""
    producer = FakeProducer()
    monkeypatch.setattr(api.app.state, "hub", hub, raising=False)
    monkeypatch.setattr(api.app.state, "producer", producer, raising=False)
    monkeypatch.setattr(api.app.state, "settings", Settings.from_env(), raising=False)
    c = TestClient(api.app)  # not a context manager, so the lifespan does not run
    c.producer = producer
    return c


def run(coro):
    return asyncio.run(coro)


# --- plain endpoints ---------------------------------------------------------------------------------------------

def test_health_names_the_data_source(client):
    assert client.get("/api/health").json() == {"ok": True, "source": "synthetic"}


def test_stats_reflect_what_the_hub_has_seen(client, hub):
    hub.ingest(decision("block", truth="ring-1"))
    hub.ingest(decision("allow"))
    s = client.get("/api/stats").json()
    assert s["total"] == 2 and s["blocked"] == 1 and s["recall"] == 1.0 and s["scenario"]["id"] == "ring-1"


def test_cities_come_from_the_shared_places_file(client):
    cities = client.get("/api/cities").json()
    assert len(cities) > 10 and {"name", "lat", "lon"} <= set(cities[0])


def test_the_attack_button_publishes_a_command_on_the_control_topic(client):
    r = client.post("/api/attack", json={"size": 20})
    assert r.json() == {"ok": True, "size": 20}
    assert client.producer.sent == [("control", {"type": "attack", "size": 20})]


@pytest.mark.parametrize("size", [2, 61, "many"])
def test_an_attack_of_a_silly_size_is_refused_and_nothing_is_published(client, size):
    assert client.post("/api/attack", json={"size": size}).status_code == 422
    assert client.producer.sent == []


def test_the_attack_defaults_to_a_ring_of_fourteen(client):
    assert client.post("/api/attack", json={}).json()["size"] == 14


def test_the_drift_endpoint_lists_every_scorers_latest_report(client, hub):
    assert client.get("/api/drift").json() == {"summary": None, "scorers": []}
    hub.ingest_drift({"scorer": "a", "model": "v1", "status": "ok", "ts": int(time.time() * 1000)})
    body = client.get("/api/drift").json()
    assert body["summary"]["models"] == ["v1"] and [s["scorer"] for s in body["scorers"]] == ["a"]


def test_the_ui_and_its_data_are_served(client):
    index = client.get("/")
    assert index.status_code == 200 and "FRAUDRADAR" in index.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/globe.html").status_code == 200
    geo = client.get("/data/countries.geojson")
    assert geo.status_code == 200 and geo.json()["type"] == "FeatureCollection"


def test_big_geojson_is_gzipped_when_the_browser_accepts_it(client):
    r = client.get("/data/vn_districts.geojson", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


# --- the hub -----------------------------------------------------------------------------------------------------

def test_every_flagged_payment_is_sent_but_allowed_ones_are_sampled_to_keep_the_browser_light(hub):
    items = [decision("block", src=f"B{i}") for i in range(5)] + [decision("review")]
    items += [decision("allow", src=f"A{i}") for i in range(api.MAX_ALLOWED_PER_TICK * 3)]
    out = hub._compact(items)
    assert sum(i["action"] != "allow" for i in out) == 6
    assert sum(i["action"] == "allow" for i in out) == api.MAX_ALLOWED_PER_TICK
    assert {"src", "dst", "amount", "src_city", "dst_city", "src_district", "dst_district", "score", "action", "reasons", "ts"} == set(out[0])


def test_a_small_tick_is_sent_whole(hub):
    items = [decision("allow", src=f"A{i}") for i in range(10)]
    assert len(hub._compact(items)) == 10


def test_a_tick_carries_the_items_the_stats_and_the_model_health(hub):
    ws = FakeSocket()
    hub.clients.add(ws)
    hub.ingest(decision("block", truth="ring-1"))
    hub.ingest_drift({"scorer": "a", "model": "v1", "status": "drift", "reasons": ["x"], "ts": int(time.time() * 1000)})
    run(hub.flush())
    (tick,) = ws.received
    assert tick["type"] == "tick" and len(tick["items"]) == 1 and tick["stats"]["total"] == 1
    assert tick["model"]["status"] == "drift"


def test_a_tick_has_no_model_health_until_a_scorer_reports(hub):
    ws = FakeSocket()
    hub.clients.add(ws)
    hub.ingest(decision())
    run(hub.flush())
    assert ws.received[0]["model"] is None


def test_items_are_buffered_only_while_someone_is_listening(hub):
    hub.ingest(decision())
    assert hub._pending == [] and hub.metrics.total == 1  # still counted
    hub.clients.add(FakeSocket())
    hub.ingest(decision())
    assert len(hub._pending) == 1


def test_a_flush_empties_the_buffer_so_nothing_is_sent_twice(hub):
    ws = FakeSocket()
    hub.clients.add(ws)
    hub.ingest(decision())
    run(hub.flush())
    run(hub.flush())
    assert [len(t["items"]) for t in ws.received] == [1, 0]


def test_a_client_that_fails_to_receive_is_dropped_and_the_others_still_get_the_tick(hub):
    good, bad = FakeSocket(), FakeSocket(fail=True)
    hub.clients.update({good, bad})
    hub.ingest(decision())
    run(hub.flush())
    assert hub.clients == {good} and len(good.received) == 1


def test_with_no_clients_a_flush_does_nothing(hub):
    hub.ingest(decision())
    run(hub.flush())  # must not raise


# --- the websocket -----------------------------------------------------------------------------------------------

def test_a_websocket_client_is_registered_while_connected_and_forgotten_after(client, hub):
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello")  # clients send nothing meaningful; it must be tolerated
        deadline = time.time() + 2
        while not hub.clients and time.time() < deadline:
            time.sleep(0.01)
        assert len(hub.clients) == 1
    deadline = time.time() + 2
    while hub.clients and time.time() < deadline:
        time.sleep(0.01)
    assert hub.clients == set()


# --- the consumer loops ------------------------------------------------------------------------------------------

class FakeStream:
    def __init__(self, values):
        self.values = values

    def __aiter__(self):
        async def gen():
            for v in self.values:
                yield type("Msg", (), {"value": v})()

        return gen()


def test_a_bad_decision_message_is_skipped_and_the_next_one_still_counts(hub):
    stream = FakeStream([b"not json", json.dumps(decision("block")).encode()])
    run(api._consume_decisions(stream, hub))
    assert hub.metrics.total == 1


def test_a_bad_drift_message_is_skipped_too(hub):
    good = json.dumps({"scorer": "a", "model": "v1", "status": "ok", "ts": 1}).encode()
    run(api._consume_drift(FakeStream([b"\xff", good]), hub))
    assert list(hub.drift) == ["a"]
