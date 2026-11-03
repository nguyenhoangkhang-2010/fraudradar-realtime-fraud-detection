import csv
import os
import uuid

import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("lightgbm")
redis = pytest.importorskip("redis")

from fraudradar import paysim_stream as ps  # noqa: E402
from fraudradar import paysim_warmup as wu  # noqa: E402
from fraudradar import replay as rp  # noqa: E402
from fraudradar.generator import EndOfLog  # noqa: E402
from fraudradar.lgbm import LightGBMScorer  # noqa: E402
from fraudradar.payment_features import PaymentFeatureStore  # noqa: E402
from fraudradar.redis_features import RedisPaymentFeatureStore, in_key  # noqa: E402

URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
HEADER = ["step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig", "nameDest",
          "oldbalanceDest", "newbalanceDest", "isFraud", "isFlaggedFraud"]


@pytest.fixture(scope="module")
def client():
    c = redis.Redis.from_url(URL, decode_responses=True, socket_connect_timeout=1)
    try:
        c.ping()
    except redis.exceptions.RedisError:
        pytest.skip(f"no Redis at {URL}; set REDIS_URL to run these")
    return c


@pytest.fixture()
def ns(client):
    name = f"test-{uuid.uuid4().hex[:8]}"
    yield name
    for key in client.scan_iter(match=f"fr:{name}:*"):
        client.unlink(key)


def make_csv(path, n=300):
    """PaySim-shaped rows made up for the test, with recipients that repeat."""
    rng = np.random.default_rng(11)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for i in range(n):
            step = 1 + i * 743 // n
            amt = round(float(rng.uniform(100, 9e5)), 2)
            if i % 25 == 0:
                w.writerow([step, "TRANSFER", amt, f"C{i}", amt, 0, f"C9{i}", 0.0, amt, 1, 0])
            else:
                w.writerow([step, "CASH_OUT", amt, f"C{i}", amt * 2, amt, f"C8{i % 12}", amt, amt * 2, 0, 0])
    return path


def test_warming_up_redis_gives_the_state_of_walking_the_rows(client, ns, tmp_path):
    path = make_csv(tmp_path / "paysim.csv")
    report = wu.warm(client, path, 200, namespace=ns)
    assert report["skipped"] is False and report["rows"] == 200
    assert client.get(wu.progress_key(ns)) == "200"

    scorer = LightGBMScorer.load()
    cats = scorer.meta["type_categories"]
    memory = PaymentFeatureStore(scorer.feature_names, cats, amount_scale=1.0)
    for tx in rp.transactions(path, limit=200):
        memory.update(tx)
    shared = RedisPaymentFeatureStore(client, scorer.feature_names, cats, scorer.unused_features(), ns, 1.0)
    for tx in rp.transactions(path, start=200, limit=260):  # recipients that repeat
        a, b = memory.columns(tx), shared.columns(tx)
        assert b["dest_prior_in"] == a["dest_prior_in"]
        assert b["dest_prior_in_sum_log"] == pytest.approx(a["dest_prior_in_sum_log"], rel=1e-12)


def test_warming_up_twice_does_nothing_and_a_new_start_row_starts_over(client, ns, tmp_path):
    path = make_csv(tmp_path / "paysim.csv")
    wu.warm(client, path, 200, namespace=ns)
    client.hincrby(in_key(ns, "C80"), "n", 5)  # something a second load would have to leave alone
    assert wu.warm(client, path, 200, namespace=ns) == {"skipped": True, "start_index": 200}
    assert wu.warm(client, path, 100, namespace=ns)["skipped"] is False  # a different start row reloads
    expected = sum(1 for tx in rp.transactions(path, limit=100) if tx.dst == "C80")
    assert client.hget(in_key(ns, "C80"), "n") == str(expected)  # rebuilt from the file, the 5 added by hand are gone


def test_one_worker_through_redis_scores_exactly_as_the_in_memory_replay(client, tmp_path):
    path = make_csv(tmp_path / "paysim.csv")
    memory = rp.replay(path, parts=rp.PARTS, chunk=64)
    shared = rp.replay_redis(path, URL, workers=1, parts=rp.PARTS, chunk=64)
    np.testing.assert_allclose(shared.scores, memory.scores, atol=1e-12)
    assert (shared.labels == memory.labels).all() and (shared.parts == memory.parts).all()


def test_several_workers_see_every_row_once_and_leave_no_state_behind(client, tmp_path):
    path = make_csv(tmp_path / "paysim.csv")
    one = rp.replay_redis(path, URL, workers=1, parts=rp.PARTS, chunk=32)
    many = rp.replay_redis(path, URL, workers=3, parts=rp.PARTS, chunk=32, namespace="replay-test-clean")
    assert len(many.scores) == len(one.scores) == 300
    assert (many.labels == one.labels).all()  # back in file order
    assert not list(client.scan_iter(match="fr:replay-test-clean:*"))  # the run cleaned up after itself


def test_a_source_resumes_where_it_stopped_and_can_refuse_to_loop(tmp_path):
    path = make_csv(tmp_path / "paysim.csv", n=12)
    src = ps.PaysimSource(path, start_step=1, resume_index=9, loop=False)
    assert src.next_index == 9
    assert [src.legit(1_000 + i).tx_id for i in range(3)] == ["P9", "P10", "P11"]
    assert src.next_index == 12
    with pytest.raises(EndOfLog):
        src.legit(2_000)
    assert ps.PaysimSource(path, start_step=1, resume_index=0).next_index == 0  # never before the start row
