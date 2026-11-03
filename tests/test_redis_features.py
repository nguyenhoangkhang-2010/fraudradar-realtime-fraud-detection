import os
import uuid

import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("lightgbm")
redis = pytest.importorskip("redis")

from fraudradar.generator import TransactionGenerator  # noqa: E402
from fraudradar.lgbm import LightGBMScorer, load_lightgbm_engine  # noqa: E402
from fraudradar.payment_features import PaymentFeatureStore  # noqa: E402
from fraudradar.redis_features import UNTRACKED, RedisPaymentFeatureStore, in_key  # noqa: E402
from fraudradar.schemas import Transaction  # noqa: E402

URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


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


@pytest.fixture(scope="module")
def scorer():
    return LightGBMScorer.load()


def stores(client, scorer, ns, scale=1500.0):
    cats = scorer.meta["type_categories"]
    mem = PaymentFeatureStore(scorer.feature_names, cats, amount_scale=scale)
    rds = RedisPaymentFeatureStore(client, scorer.feature_names, cats, scorer.unused_features(), ns, scale)
    return mem, rds


def stream(n=1500, seed=3):
    gen = TransactionGenerator(seed=seed, n_accounts=120, n_merchants=10)  # few accounts, so they repeat
    txs = [gen.legit(1_700_000_000_000 + i * 10) for i in range(n)]
    txs += gen.ring(1_700_000_000_000 + 5_000, size=6)
    return sorted(txs, key=lambda t: t.ts)


def test_the_model_uses_none_of_the_history_columns_the_redis_store_leaves_out(scorer):
    assert set(UNTRACKED) <= set(scorer.unused_features())


def test_a_model_that_uses_an_untracked_column_is_refused(client, scorer, ns):
    with pytest.raises(ValueError, match="does not keep"):
        RedisPaymentFeatureStore(client, scorer.feature_names, scorer.meta["type_categories"], [], ns)


def test_features_match_the_in_memory_store_payment_by_payment(client, scorer, ns):
    mem, rds = stores(client, scorer, ns)
    for tx in stream():
        a, b = mem.columns(tx), rds.columns(tx)
        for name in scorer.feature_names:
            if name in UNTRACKED:
                assert np.isnan(b[name])
            else:
                assert b[name] == pytest.approx(a[name], rel=1e-9, abs=1e-9, nan_ok=True), name
        mem.update(tx)
        rds.update(tx)


def test_reading_features_changes_nothing(client, scorer, ns):
    _, rds = stores(client, scorer, ns)
    tx = stream(10)[0]
    before = rds.features(tx).copy()
    for _ in range(3):
        rds.features(tx)
    assert client.exists(in_key(ns, tx.dst)) == 0
    np.testing.assert_array_equal(before, rds.features(tx))


def test_a_batch_in_one_round_trip_equals_payment_by_payment(client, scorer, ns):
    mem, rds = stores(client, scorer, ns)
    txs = stream(600)
    one_by_one = []
    for tx in txs:
        one_by_one.append(mem.features(tx)[0])
        mem.update(tx)
    batched = []
    for i in range(0, len(txs), 128):  # several batches, with repeated recipients inside each
        batched += rds.features_and_update_many(txs[i : i + 128])
    assert rds.features_and_update_many([]) == []
    for a, b in zip(one_by_one, batched):
        keep = [i for i, n in enumerate(scorer.feature_names) if n not in UNTRACKED]
        np.testing.assert_allclose(b[keep], a[keep], rtol=1e-9, atol=1e-9)


def test_decisions_are_the_same_with_state_in_memory_or_in_redis(client, ns):
    mem = load_lightgbm_engine()
    rds = load_lightgbm_engine(redis_client=client, namespace=ns)
    txs = stream(2500)
    want = [mem.handle(tx, tx.ts) for tx in txs]
    got = []
    for i in range(0, len(txs), 200):
        got += rds.handle_many(txs[i : i + 200], txs[i + 199 if i + 199 < len(txs) else -1].ts)
    assert len(got) == len(want)
    assert [d.action for d in got] == [d.action for d in want]
    np.testing.assert_allclose([d.score for d in got], [d.score for d in want], atol=1e-4)
    assert [d.reasons for d in got] == [d.reasons for d in want]
    assert any(d.action == "block" for d in want)  # the stream does contain something to block


def test_state_survives_a_new_store_and_namespaces_do_not_mix(client, scorer, ns):
    _, first = stores(client, scorer, ns)
    tx = Transaction("t", 0, "A", "B", 10.0, 0, 1, "D", 9.0, kind="TRANSFER", src_balance=10.0, dst_balance=0.0)
    first.update(tx)
    first.update(tx)
    _, second = stores(client, scorer, ns)  # as after a scorer restart
    assert second.columns(tx)["dest_prior_in"] == 2
    _, other = stores(client, scorer, ns + "-other")
    assert other.columns(tx)["dest_prior_in"] == 0
    assert first.clear() == 1
    assert second.columns(tx)["dest_prior_in"] == 0
