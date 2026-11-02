import csv
import json

import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("lightgbm")

from fraudradar import replay as rp  # noqa: E402
from fraudradar.lgbm import ARTIFACT_DIR, META_FILE, load_lightgbm_engine  # noqa: E402

HEADER = ["step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig", "nameDest",
          "oldbalanceDest", "newbalanceDest", "isFraud", "isFlaggedFraud"]


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)


def sample_rows():
    """A few hundred PaySim-shaped rows, made up for the test, spanning all three parts of the split."""
    rng = np.random.default_rng(3)
    rows = []
    for i in range(600):
        step = 1 + i * 743 // 600
        if i % 25 == 0:  # a sweep into an empty account
            amt = round(float(rng.uniform(1e5, 1e6)), 2)
            rows.append([step, "TRANSFER", amt, f"C{i}", amt, 0, f"C9{i}", 0.0, amt, 1, 0])
        elif i % 3 == 0:
            amt = round(float(rng.uniform(10, 5e4)), 2)
            rows.append([step, "PAYMENT", amt, f"C{i}", amt * 3, amt * 2, f"M{i % 7}", 0.0, 0.0, 0, 0])
        else:
            amt = round(float(rng.uniform(10, 5e4)), 2)
            rows.append([step, "CASH_OUT", amt, f"C{i % 40}", amt * 2, amt, f"C8{i % 11}", amt, amt * 2, 0, 0])
    return rows


def test_average_precision_on_a_hand_worked_example():
    # scores sorted: 0.9(+) 0.8(-) 0.7(+) 0.1(-); precision at the two hits: 1/1 and 2/3
    ap = rp.average_precision(np.array([0.9, 0.8, 0.7, 0.1]), np.array([True, False, True, False]))
    assert ap == pytest.approx(0.5 * 1.0 + 0.5 * (2 / 3))
    assert rp.average_precision(np.array([0.5, 0.5]), np.array([True, False])) == pytest.approx(0.5)  # a tie
    assert np.isnan(rp.average_precision(np.array([0.1, 0.2]), np.array([False, False])))


def test_threshold_metrics_count_each_kind_of_outcome():
    scores = np.array([0.9, 0.8, 0.3, 0.2])
    labels = np.array([True, False, True, False])
    m = rp.at_threshold(scores, labels, 0.5)
    assert (m["caught"], m["missed"], m["false_alarms"]) == (1, 1, 1)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["false_alarm_rate"] == 0.5


def test_rows_become_transactions_without_leaking_the_label_into_the_model_inputs(tmp_path):
    path = tmp_path / "paysim.csv"
    write_rows(path, sample_rows())
    txs = list(rp.transactions(path, limit=30))
    assert txs[0].truth == "fraud" and txs[0].kind == "TRANSFER" and txs[0].src_balance == txs[0].amount
    pay = next(t for t in txs if t.kind == "PAYMENT")
    assert pay.dst_merchant and pay.dst.startswith("M")
    assert all(t.ts % 1000 == 0 for t in txs)


def test_a_file_that_is_not_paysim_is_refused(tmp_path):
    path = tmp_path / "other.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not look like PaySim"):
        list(rp.transactions(path))


def test_scoring_in_chunks_gives_the_same_numbers_as_the_service_path(tmp_path):
    path = tmp_path / "paysim.csv"
    write_rows(path, sample_rows())
    run = rp.replay(path, parts=("train", "valid", "test"), chunk=64)  # several chunks
    engine = load_lightgbm_engine(amount_scale=1.0)
    one_by_one = [engine.handle(tx, tx.ts).score for tx in rp.transactions(path)]
    assert len(run.scores) == len(one_by_one) == 600
    # the engine rounds a score to 4 decimals, so compare at that precision
    np.testing.assert_allclose(run.scores, one_by_one, atol=1e-4)


def test_only_the_chosen_part_is_scored_but_the_history_comes_from_the_whole_file(tmp_path):
    path = tmp_path / "paysim.csv"
    write_rows(path, sample_rows())
    meta = json.loads((ARTIFACT_DIR / META_FILE).read_text(encoding="utf-8"))
    test_only = rp.replay(path, parts=("test",))
    everything = rp.replay(path, parts=rp.PARTS)
    n_test = int((everything.parts == "test").sum())
    assert 0 < len(test_only.scores) == n_test < len(everything.scores)
    assert test_only.rows_walked == everything.rows_walked == 600
    np.testing.assert_allclose(test_only.scores, everything.scores[everything.parts == "test"])
    assert rp.summarize(test_only, meta)["payments"] == n_test


def test_summary_puts_the_notebook_figures_beside_the_replay(tmp_path):
    path = tmp_path / "paysim.csv"
    write_rows(path, sample_rows())
    meta = json.loads((ARTIFACT_DIR / META_FILE).read_text(encoding="utf-8"))
    result = rp.summarize(rp.replay(path), meta)
    assert result["notebook"]["AP"] == meta["test_metrics"]["AP"]
    assert set(result["at_threshold"]) == set(meta["thresholds_chosen_on_validation"])
