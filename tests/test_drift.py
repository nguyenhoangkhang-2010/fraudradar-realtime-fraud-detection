import numpy as np
import pytest

pytest.importorskip("numpy")

from fraudradar import drift, skew  # noqa: E402
from fraudradar.payment_features import PaymentFeatureStore  # noqa: E402
from fraudradar.schemas import Transaction  # noqa: E402

NAMES = ["type", "amount_log", "drains_account", "unused"]
TYPES = ["CASH_IN", "CASH_OUT", "PAYMENT"]


def traffic(n, rng, shift=0.0):
    return np.column_stack([
        rng.integers(0, 3, n).astype(float),
        rng.normal(9.0 + shift, 1.5, n),
        (rng.random(n) < 0.05).astype(float),
        rng.normal(0.0, 1.0, n),
    ])


@pytest.fixture(scope="module")
def reference():
    rng = np.random.default_rng(1)
    x = traffic(60_000, rng)
    return skew.build_reference(x.astype(np.float32), rng.random(60_000) < 0.01, NAMES, TYPES)


SPLITS = {"type": 3, "amount_log": 40, "drains_account": 12, "unused": 0}


def monitor(reference, **kw):
    kw = {"window": 10_000, "min_rows": 2_000, **kw}
    return drift.DriftMonitor(reference, NAMES, SPLITS, **kw)


def test_the_reference_has_the_bins_the_skew_check_expects(reference):
    f = reference["features"]
    assert f["type"]["kind"] == "categorical" and f["type"]["bins"] == TYPES
    assert f["drains_account"]["kind"] == "discrete" and f["drains_account"]["values"] == [0.0, 1.0]
    assert f["amount_log"]["kind"] == "numeric" and len(f["amount_log"]["edges"]) == 9
    assert sum(f["amount_log"]["share_all"]) == pytest.approx(1.0)
    assert reference["rows"] == 60_000 and reference["split"] == "train"


def test_traffic_like_the_training_data_is_healthy(reference):
    m = monitor(reference)
    m.observe(traffic(10_000, np.random.default_rng(2)), np.zeros(10_000, bool))
    r = m.report()
    assert r["status"] == "ok" and r["worst_used"]["psi"] < skew.MODERATE


def test_the_monitor_waits_for_enough_payments(reference):
    m = monitor(reference)
    m.observe(traffic(500, np.random.default_rng(2)), np.zeros(500, bool))
    assert m.report()["status"] == "warming up"


def test_a_shifted_input_the_model_uses_is_drift(reference):
    m = monitor(reference)
    m.observe(traffic(10_000, np.random.default_rng(3), shift=2.0), np.zeros(10_000, bool))
    r = m.report()
    assert r["status"] == "drift" and r["worst_used"]["feature"] == "amount_log"
    assert "amount_log" in r["reasons"][0]


def test_a_shifted_input_the_model_never_splits_on_is_ignored(reference):
    m = monitor(reference)
    x = traffic(10_000, np.random.default_rng(4))
    x[:, 3] += 5.0
    m.observe(x, np.zeros(10_000, bool))
    r = m.report()
    assert r["status"] == "ok"
    assert next(f for f in r["features"] if f["feature"] == "unused")["psi"] > skew.LARGE  # still listed


def test_the_first_full_window_sets_the_flag_rate_baseline_and_a_collapse_is_reported(reference):
    m = monitor(reference)
    rng = np.random.default_rng(5)
    m.observe(traffic(10_000, rng), rng.random(10_000) < 0.02)
    first = m.report()
    assert first["alert"]["baseline"] == pytest.approx(0.02, abs=0.01) and first["status"] == "ok"
    m.observe(traffic(10_000, rng), rng.random(10_000) < 0.002)  # the model has stopped flagging
    r = m.report()
    assert r["alert"]["status"] == "low" and r["alert"]["z"] < -4 and r["status"] == "drift"
    m.observe(traffic(10_000, rng), rng.random(10_000) < 0.05)
    assert m.report()["alert"]["status"] == "high"


def test_the_ring_keeps_only_the_latest_window(reference):
    m = monitor(reference, window=5_000)
    rng = np.random.default_rng(6)
    m.observe(traffic(4_000, rng, shift=3.0), np.zeros(4_000, bool))
    m.observe(traffic(7_000, rng), np.zeros(7_000, bool))  # more than a window: only its tail counts
    m.observe(traffic(1_000, rng), np.zeros(1_000, bool))
    r = m.report()
    assert r["rows"] == 5_000 and r["seen"] == 12_000 and r["status"] == "ok"


# --- injected drift and the feature matrix --------------------------------------------------------------------

def pay(tx_id, step, amount=100.0, src_balance=100.0, fraud=False, dst="C2"):
    return Transaction(
        tx_id=tx_id, ts=step * 1000, step=step, src="C1", dst=dst, amount=amount, src_city=0, dst_city=0,
        device="", src_age_days=9999.0, truth="fraud" if fraud else None, kind="TRANSFER",
        src_balance=src_balance, dst_balance=0.0, dst_merchant=False,
    )


def test_inflation_multiplies_money_only_from_its_start_hour():
    f = drift.inflation(3.0, from_step=10)
    before, after = f(pay("a", 9)), f(pay("b", 10))
    assert before.amount == 100.0 and after.amount == 300.0
    assert after.src_balance == 300.0 and after.dst_balance == 0.0


def test_evasion_stops_fraud_emptying_the_account_but_leaves_honest_payments_alone():
    f = drift.evasion(from_step=10)
    honest = pay("h", 20, fraud=False)
    old_fraud, new_fraud = pay("f1", 5, fraud=True), pay("f2", 20, fraud=True)
    assert f(honest) is honest and f(old_fraud) is old_fraud
    assert 20.0 <= f(new_fraud).amount <= 80.0 and f(new_fraud).truth == "fraud"
    assert f(pay("f2", 20, fraud=True)).amount == f(new_fraud).amount  # deterministic per payment


def test_the_feature_matrix_is_what_the_serving_store_computes(tmp_path):
    header = "step,type,amount,nameOrig,oldbalanceOrg,newbalanceOrig,nameDest,oldbalanceDest,newbalanceDest,isFraud,isFlaggedFraud"
    rows = [
        "1,TRANSFER,100.0,C1,100.0,0.0,C2,0.0,100.0,1,0",
        "1,PAYMENT,5.0,C3,50.0,45.0,M9,0.0,0.0,0,0",
        "2,CASH_OUT,40.0,C2,100.0,60.0,C7,10.0,50.0,0,0",
    ]
    path = tmp_path / "log.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    ds = drift.feature_matrix(path)
    assert ds.X.shape == (3, 14) and ds.y.tolist() == [True, False, False] and ds.step.tolist() == [1, 1, 2]

    from fraudradar.replay import transactions

    store = PaymentFeatureStore(ds.names, ["CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER"], amount_scale=1.0)
    for i, tx in enumerate(transactions(path)):
        expected = store.features(tx)[0]
        assert np.allclose(ds.X[i], expected, equal_nan=True)
        store.update(tx)
    assert ds.X[2][ds.names.index("dest_prior_in")] == 0 and ds.X[2][ds.names.index("recv_count")] == 1
