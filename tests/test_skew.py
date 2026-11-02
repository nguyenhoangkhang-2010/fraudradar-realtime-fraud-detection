import json

import numpy as np
import pytest

pytest.importorskip("numpy")

from fraudradar import skew  # noqa: E402

NUMERIC = {"kind": "numeric", "edges": [1.0, 2.0, 3.0]}  # four bins: <1, [1,2), [2,3), >=3
CATEGORICAL = {"kind": "categorical", "bins": ["A", "B", "C"]}
FLAG = {"kind": "discrete", "values": [0.0, 1.0]}  # three bins: 0, 1, anything else


def test_a_value_on_an_edge_goes_to_the_upper_bin():
    codes = skew.bin_codes(NUMERIC, np.array([0.5, 1.0, 1.5, 2.0, 99.0, np.nan]))
    assert codes.tolist() == [0, 1, 1, 2, 3, -1]


def test_categories_are_their_index_and_unknown_codes_count_as_missing():
    assert skew.bin_codes(CATEGORICAL, np.array([0.0, 2.0, 3.0, np.nan])).tolist() == [0, 2, -1, -1]


def test_a_flag_gets_a_bin_per_value_so_zero_and_one_never_share_one():
    codes = skew.bin_codes(FLAG, np.array([0.0, 1.0, 1.0, 7.0, np.nan]))
    assert codes.tolist() == [0, 1, 1, 2, -1]
    s, _ = skew.shares(FLAG, np.array([1.0] * 8 + [0.0] * 2))
    assert s.tolist() == [pytest.approx(0.2), pytest.approx(0.8), 0.0]
    assert skew.psi([0.9, 0.1, 0.0], s) > skew.LARGE  # all-ones against a mostly-zero reference


def test_shares_ignore_missing_values_and_report_them_separately():
    s, missing = skew.shares(NUMERIC, np.array([0.0, 0.0, 1.5, np.nan]))
    assert s.tolist() == [pytest.approx(2 / 3), pytest.approx(1 / 3), 0.0, 0.0]
    assert missing == pytest.approx(0.25)


def test_psi_is_zero_for_the_same_spread_and_grows_as_it_moves():
    ref = np.array([0.25, 0.25, 0.25, 0.25])
    assert skew.psi(ref, ref) == pytest.approx(0.0)
    small = skew.psi(ref, [0.30, 0.25, 0.25, 0.20])
    large = skew.psi(ref, [0.70, 0.10, 0.10, 0.10])
    assert 0 < small < skew.MODERATE < skew.LARGE < large
    assert np.isfinite(skew.psi(ref, [1.0, 0.0, 0.0, 0.0]))  # an empty bin does not blow up


def test_verdict_uses_the_usual_cutoffs():
    assert [skew.verdict(v) for v in (0.05, 0.15, 0.4)] == ["close", "moved", "DIFFERENT"]


def reference() -> dict:
    return {
        "quantiles": [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99],
        "features": {
            "x": {**NUMERIC, "share_all": [0.25] * 4, "share_fraud": [0.0, 0.0, 0.5, 0.5],
                  "missing_all": 0.0, "missing_fraud": 0.0,
                  "quantiles_all": [0, 0, 1, 2, 3, 4, 5], "quantiles_fraud": [2, 2, 2, 3, 3, 3, 3]},
            "type": {**CATEGORICAL, "share_all": [0.5, 0.25, 0.25], "share_fraud": [0.0, 0.5, 0.5],
                     "missing_all": 0.0, "missing_fraud": 0.0},
        },
    }


def test_compare_flags_the_feature_that_moved_and_not_the_one_that_did_not():
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 4, 4000)  # even over the four bins, like the reference
    kinds = rng.choice([0.0, 1.0, 2.0], 4000, p=[0.5, 0.25, 0.25])
    same = skew.compare(reference(), np.column_stack([x, kinds]), ["x", "type"])
    assert {r.feature: r.verdict for r in same} == {"x": "close", "type": "close"}

    shifted = skew.compare(reference(), np.column_stack([x + 3.0, kinds]), ["x", "type"])
    by_name = {r.feature: r for r in shifted}
    assert by_name["x"].verdict == "DIFFERENT" and by_name["type"].verdict == "close"
    assert by_name["x"].median_live == pytest.approx(5.0, abs=0.1) and by_name["x"].median_reference == 2


def test_the_ring_is_compared_with_fraud_not_with_everyone():
    ring = np.column_stack([np.full(50, 2.5), np.full(50, 1.0)])
    vs_fraud = {r.feature: r.psi for r in skew.compare(reference(), ring, ["x", "type"], "fraud")}
    vs_all = {r.feature: r.psi for r in skew.compare(reference(), ring, ["x", "type"], "all")}
    assert vs_fraud["x"] < vs_all["x"]


def test_suggested_scale_aligns_the_median_amount():
    ref = {"quantiles": [0.5], "features": {"amount_log": {"quantiles_all": [np.log1p(75_000.0)]}}}
    assert skew.suggested_scale(ref, np.array([40.0, 50.0, 60.0])) == pytest.approx(75_000 / 50.0)


def test_a_missing_reference_file_says_how_to_make_one(tmp_path):
    with pytest.raises(FileNotFoundError, match="reference_stats.json"):
        skew.load_reference(tmp_path)


def test_collect_builds_rows_in_the_models_column_order(tmp_path):
    lgb = pytest.importorskip("lightgbm")  # noqa: F841 - the bundled model's metadata lives beside the model
    honest, ring, names, amounts = skew.collect(seconds=5, tps=50, rings=1)
    meta = json.loads((skew.ARTIFACT_DIR / skew.META_FILE).read_text(encoding="utf-8"))
    assert names == meta["features"]
    assert honest.shape == (250, len(names)) and ring.shape[1] == len(names) and len(amounts) == 250


def test_the_bundled_reference_matches_the_bundled_model():
    pytest.importorskip("lightgbm")
    reference = skew.load_reference()
    meta = json.loads((skew.ARTIFACT_DIR / skew.META_FILE).read_text(encoding="utf-8"))
    assert set(reference["features"]) == set(meta["features"])
    assert reference["split"] == "train" and reference["fraud_rows"] > 0
    for entry in reference["features"].values():
        assert abs(sum(entry["share_all"]) - 1.0) < 1e-6


def test_the_report_marks_features_the_model_never_splits_on():
    pytest.importorskip("lightgbm")
    splits = skew.model_splits()
    assert splits["amount_log"] > 0 and splits["recv_count"] == 0
    honest, ring, names, _ = skew.collect(seconds=5, tps=50, rings=1)
    rows = skew.compare(skew.load_reference(), honest, names, "all", splits)
    assert {r.feature: r.splits for r in rows} == splits
