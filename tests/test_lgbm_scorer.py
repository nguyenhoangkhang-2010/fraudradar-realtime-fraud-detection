import math

import pytest

pytest.importorskip("numpy")
lgb = pytest.importorskip("lightgbm")

from fraudradar.evaluate import run_offline  # noqa: E402
from fraudradar.lgbm import LightGBMScorer, load_lightgbm_engine  # noqa: E402
from fraudradar.payment_features import PaymentFeatureStore  # noqa: E402
from fraudradar.schemas import Transaction  # noqa: E402

SECOND = 1000


def pay(src="A1", dst="A2", amount=50.0, ts=10 * SECOND, kind="TRANSFER", src_balance=900.0,
        dst_balance=400.0, merchant=False) -> Transaction:
    return Transaction(
        tx_id=f"{src}-{ts}", ts=ts, src=src, dst=dst, amount=amount, src_city=0, dst_city=1,
        device="D1", src_age_days=400.0, kind=kind, src_balance=src_balance,
        dst_balance=dst_balance, dst_merchant=merchant,
    )


@pytest.fixture(scope="module")
def scorer() -> LightGBMScorer:
    return LightGBMScorer.load()


@pytest.fixture()
def store(scorer) -> PaymentFeatureStore:
    return PaymentFeatureStore(scorer.feature_names, scorer.meta["type_categories"])


def test_the_store_covers_every_feature_the_model_was_trained_on(scorer, store):
    assert list(store.columns(pay())) == scorer.feature_names


def test_a_model_with_unknown_columns_is_refused(scorer):
    with pytest.raises(ValueError, match="cannot build"):
        PaymentFeatureStore([*scorer.feature_names, "made_up"], scorer.meta["type_categories"])


def test_features_are_read_only_and_exclude_the_current_payment(store):
    t = pay()
    before = store.columns(t)
    assert store.columns(t) == before
    assert before["dest_prior_in"] == 0 and before["orig_prior_out"] == 0
    store.update(t)
    after = store.columns(pay(ts=t.ts + SECOND))
    assert after["dest_prior_in"] == 1 and after["orig_prior_out"] == 1


def test_drain_and_balance_features(store):
    drain = store.columns(pay(amount=900.0, src_balance=900.0, dst_balance=0.0))
    assert drain["drains_account"] == 1.0 and drain["dest_old_zero"] == 1.0
    ordinary = store.columns(pay(amount=50.0))
    assert ordinary["drains_account"] == 0.0 and ordinary["amount_to_oldbal"] < 1
    broke = store.columns(pay(src_balance=0.0))
    assert broke["orig_old_zero"] == 1.0 and broke["drains_account"] == 0.0


def test_receiving_money_feeds_the_senders_chain_features(store):
    store.update(pay(src="X", dst="A1", amount=100.0, ts=2 * SECOND))
    cols = store.columns(pay(src="A1", amount=50.0, ts=5 * SECOND))
    assert cols["recv_count"] == 1
    assert cols["steps_since_recv"] == 3  # one simulated hour per second
    assert math.isclose(cols["amount_to_last_recv"], 0.5)


def test_missing_payment_details_become_missing_values_not_zeros(store):
    cols = store.columns(Transaction("t", 0, "A", "B", 5.0, 0, 1, "D", 9.0))
    assert math.isnan(cols["type"]) and math.isnan(cols["drains_account"])


def test_thresholds_come_from_the_models_own_validation(scorer):
    assert 0 < scorer.default_review < scorer.default_block < 0.1
    engine = load_lightgbm_engine()
    assert (engine.review_threshold, engine.block_threshold) == (scorer.default_review, scorer.default_block)
    assert load_lightgbm_engine(block=0.9, review=0.4).block_threshold == 0.9


def test_a_sweep_into_an_empty_account_is_blocked_and_explained():
    engine = load_lightgbm_engine()
    d = engine.handle(pay(amount=900.0, src_balance=900.0, dst_balance=0.0), now_ms=10 * SECOND)
    assert d.action == "block"
    assert "payment empties the sender's account" in d.reasons


def test_an_everyday_merchant_payment_is_allowed():
    engine = load_lightgbm_engine()
    d = engine.handle(
        pay(dst="M1", kind="PAYMENT", amount=40.0, dst_balance=0.0, merchant=True), now_ms=10 * SECOND
    )
    assert d.action == "allow" and d.reasons == []


def test_engine_does_not_use_ground_truth():
    clean = load_lightgbm_engine().handle(pay(amount=900.0, src_balance=900.0), 10 * SECOND)
    t = pay(amount=900.0, src_balance=900.0)
    t.truth = "ring-001"
    labeled = load_lightgbm_engine().handle(t, 10 * SECOND)
    assert (clean.score, clean.action) == (labeled.score, labeled.action)


def test_every_ring_account_is_stopped_at_least_once():
    r = run_offline(seed=7, seconds=60, tps=100, ring_starts=(15, 40), scorer="lightgbm")
    assert r["ring_accounts_blocked"] == r["ring_accounts"] == 28
    assert r["false_block_rate"] < 0.01
