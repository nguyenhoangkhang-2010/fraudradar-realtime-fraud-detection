import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("lightgbm")

from fraudradar import retrain  # noqa: E402
from fraudradar.drift import Dataset  # noqa: E402
from fraudradar.engine import ScoringEngine  # noqa: E402
from fraudradar.lgbm import ARTIFACT_DIR, LightGBMScorer, load_lightgbm_engine  # noqa: E402
from fraudradar.schemas import Transaction  # noqa: E402

TYPES = ["CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER"]


@pytest.fixture(scope="module")
def champion():
    return LightGBMScorer.load()


def make_dataset(names, n=40_000, hours=100, seed=0, fraud_rule="drains"):
    """Random features, with fraud decided by one of them, so a model can learn it and a different rule can drift."""
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, len(names))).astype(np.float32)
    x[:, names.index("type")] = rng.integers(0, 5, n)
    step = np.sort(rng.integers(1, hours + 1, n)).astype(np.int32)
    col = names.index("drains_account" if fraud_rule == "drains" else "amount_log")
    y = (x[:, col] > 2.0) & (rng.random(n) < 0.9)
    return Dataset(x, y, step, list(names))


def test_the_three_windows_follow_each_other_and_stop_before_the_unlabelled_hours():
    w = retrain.windows(now=500, label_delay=48, valid_hours=36)
    assert w == {"train": (1, 380), "valid": (381, 416), "gate": (417, 452)}
    assert retrain.windows(500, 48, 36, history=100)["train"] == (281, 380)  # only the newest 100 hours


def m(ap, fa=0.001):
    return {"AP": ap, "false_alarm_rate@0.001": fa}


def test_the_gate_wants_a_real_gain():
    assert retrain.gate(m(0.90), m(0.95), 0.002)[0]
    assert not retrain.gate(m(0.95), m(0.951), 0.002)[0]
    assert not retrain.gate(m(0.95), m(0.90), 0.0)[0]


def test_the_gate_lets_in_a_model_that_fixes_a_threshold_that_drifted_even_without_an_ap_gain():
    ok, why = retrain.gate(m(0.9990, fa=0.05), m(0.9985, fa=0.002), 0.002)
    assert ok and "false-alarm target" in why
    assert not retrain.gate(m(0.9990, fa=0.05), m(0.9990, fa=0.04), 0.002)[0]  # not enough of a fix
    assert not retrain.gate(m(0.9990, fa=0.05), m(0.95, fa=0.002), 0.002)[0]  # the fix costs ranking quality
    assert not retrain.gate(m(0.9990, fa=0.0015), m(0.9990, fa=0.0005), 0.002)[0]  # the old one is still on target


def test_a_challenger_that_floods_the_analysts_is_refused_whatever_its_ap():
    ok, why = retrain.gate(m(0.30), m(0.90, fa=0.02), 0.002)
    assert not ok and "over 5x" in why


def test_too_little_fraud_is_refused(champion):
    ds = make_dataset(champion.feature_names)
    ds.y[:] = False
    with pytest.raises(ValueError, match="too few fraud"):
        retrain.fit(ds, TYPES, (1, 60), (61, 80))


def test_a_retrained_model_loads_as_a_scorer_and_learns_the_rule(champion, tmp_path):
    ds = make_dataset(champion.feature_names)
    trained = retrain.fit(ds, TYPES, (1, 70), (71, 85), max_rounds=60, learning_rate=0.2)
    folder = retrain.save(trained, tmp_path / "v2")
    scorer = LightGBMScorer.load(folder)
    assert scorer.feature_names == champion.feature_names
    test = ds.step > 85
    p = scorer.score_batch(ds.X[test])
    assert retrain.metrics(p, ds.y[test], trained.meta["thresholds_chosen_on_validation"])["AP"] > 0.9
    t = trained.meta["thresholds_chosen_on_validation"]
    assert t["fpr_0.0001"] >= t["fpr_0.001"] > 0  # the block threshold is the stricter one
    assert trained.reference["features"]["type"]["kind"] == "categorical"
    assert trained.meta["split"] == {"train_end_hour": 70, "valid_end_hour": 85} and trained.meta["parent"] is None


def test_retrain_beats_a_model_that_learned_another_rule_and_the_gate_says_so(champion, tmp_path):
    # the data now follows a different rule from the one the bundled model learned
    ds = make_dataset(champion.feature_names, seed=3, fraud_rule="amount")
    trained, report = retrain.retrain(ds, ARTIFACT_DIR, now=100, label_delay=10, valid_hours=15,
                                      max_rounds=60)
    assert report["promote"] and report["challenger_on_gate"]["AP"] > report["champion_on_gate"]["AP"] + 0.1
    assert report["windows"]["gate"][1] == 90 and trained.meta["parent"] == "paysim_v1"


def test_the_engine_counts_what_it_scores_and_can_swap_models(champion, tmp_path):
    engine = load_lightgbm_engine()
    seen = []
    engine.observer = lambda rows, flagged: seen.append((np.asarray(rows).shape, list(flagged)))
    tx = Transaction(tx_id="t1", ts=1000, step=1, src="C1", dst="C2", amount=100.0, src_city=0, dst_city=0, device="",
                     src_age_days=9999.0, kind="TRANSFER", src_balance=100.0, dst_balance=0.0)
    engine.handle(tx, now_ms=2000)
    assert seen[0][0] == (1, 14) and len(seen[0][1]) == 1

    ds = make_dataset(champion.feature_names)
    folder = retrain.save(retrain.fit(ds, TYPES, (1, 70), (71, 85), max_rounds=20, learning_rate=0.2), tmp_path / "v2")
    new = LightGBMScorer.load(folder)
    engine.swap_scorer(new, new.default_block, new.default_review)
    assert engine.scorer is new and engine.review_threshold == new.default_review

    other = LightGBMScorer.load(folder)
    other.feature_names = other.feature_names[:-1]
    kept = (engine.scorer, engine.review_threshold)
    with pytest.raises(ValueError, match="different features"):
        engine.swap_scorer(other, 0.5, 0.1)
    assert (engine.scorer, engine.review_threshold) == kept  # a refused swap changes nothing
    assert isinstance(engine, ScoringEngine)
