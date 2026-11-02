import csv

import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("lightgbm")

from fraudradar import paysim_stream as ps  # noqa: E402
from fraudradar import replay as rp  # noqa: E402
from fraudradar.lgbm import load_lightgbm_engine  # noqa: E402
from fraudradar.payment_features import PaymentFeatureStore  # noqa: E402
from fraudradar.schemas import Transaction  # noqa: E402

HEADER = ["step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig", "nameDest",
          "oldbalanceDest", "newbalanceDest", "isFraud", "isFlaggedFraud"]


def make_csv(path, n=240):
    """PaySim-shaped rows made up for the test: steps 1..n//4, a sweep every 20th row."""
    rng = np.random.default_rng(5)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for i in range(n):
            step = 1 + i // 4
            amt = round(float(rng.uniform(100, 9e5)), 2)
            if i % 20 == 0:
                w.writerow([step, "TRANSFER", amt, f"C{i}", amt, 0, f"C9{i}", 0.0, amt, 1, 0])
            elif i % 3 == 0:
                w.writerow([step, "PAYMENT", amt, f"C{i}", amt * 3, amt * 2, f"M{i % 5}", 0.0, 0.0, 0, 0])
            else:
                w.writerow([step, "CASH_OUT", amt, f"C{i % 30}", amt * 2, amt, f"C8{i % 9}", amt, amt * 2, 0, 0])
    return path


def test_the_same_account_is_always_in_the_same_place():
    p = ps.Placement()
    assert p.of("C123456789") == p.of("C123456789")
    cities = {p.of(f"C{i}")[0] for i in range(2000)}
    assert len(cities) > 40 and all(0 <= c < len(p.places) for c in cities)  # spread over many places


def test_a_district_is_given_only_where_the_place_has_districts():
    p = ps.Placement()
    for i in range(300):
        city, district = p.of(f"C{i}")
        ids = p.places[city]["district_ids"]
        assert (district in ids) if ids else district == -1


def test_start_index_is_the_first_row_of_the_hour(tmp_path):
    path = make_csv(tmp_path / "paysim.csv")
    assert ps.first_index_of_step(path, 1) == 0
    assert ps.first_index_of_step(path, 11) == 40  # four rows per hour
    assert ps.first_index_of_step(path, 10_000) == 240  # past the end


def test_a_csv_that_is_not_paysim_is_refused(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not look like PaySim"):
        ps.first_index_of_step(bad, 1)
    with pytest.raises(FileNotFoundError):
        ps.resolve_csv(tmp_path / "missing.csv")


def test_a_folder_resolves_to_its_csv(tmp_path):
    path = make_csv(tmp_path / "log.csv", n=8)
    assert ps.resolve_csv(tmp_path) == path


def test_rows_keep_their_index_step_and_label_and_get_a_place(tmp_path):
    path = make_csv(tmp_path / "paysim.csv")
    rows = list(ps.live_rows(path, 40, ps.Placement()))
    assert rows[0].tx_id == "P40" and rows[0].step == 11
    sweep = next(t for t in rows if t.kind == "TRANSFER")
    assert sweep.truth == f"paysim-h{sweep.step:03d}" and sweep.src_balance == sweep.amount
    assert all(t.truth is None for t in rows if t.kind != "TRANSFER")
    assert all(t.src_city >= 0 and t.dst_city >= 0 for t in rows)


def test_the_source_stamps_wall_clock_time_keeps_the_log_hour_and_loops(tmp_path):
    path = make_csv(tmp_path / "paysim.csv", n=12)
    src = ps.PaysimSource(path, start_step=2)
    assert src.start_index == 4
    seen = [src.legit(1_000 + i) for i in range(12)]  # 8 rows left in the file, then it starts over
    assert [t.tx_id for t in seen][:2] == ["P4", "P5"] and src.loops == 1
    assert seen[0].ts == 1_000 and seen[0].step == 2
    assert [t.tx_id for t in seen][8] == "P4"


def test_an_injected_attack_is_a_sweep_into_a_fresh_account_then_a_cash_out(tmp_path):
    src = ps.PaysimSource(make_csv(tmp_path / "paysim.csv"), start_step=5)
    src.legit(0)
    txs = src.ring(5_000, size=6)
    assert len(txs) == 12 and {t.truth for t in txs} == {"attack-001"}
    assert [t.ts for t in txs] == sorted(t.ts for t in txs)
    sweeps = [t for t in txs if t.kind == "TRANSFER"]
    cash_outs = {t.src: t for t in txs if t.kind == "CASH_OUT"}
    assert len(sweeps) == len(cash_outs) == 6
    for s in sweeps:
        assert s.amount == s.src_balance and s.dst_balance == 0.0
        out = cash_outs[s.dst]  # the fresh account cashes out the same amount
        assert out.amount == s.amount == out.src_balance and out.ts > s.ts
        assert s.step == src.step
    assert src.ring(0, size=3)[0].truth == "attack-002"


def test_the_feature_store_uses_the_logs_hour_when_a_transaction_carries_one():
    store = PaymentFeatureStore(
        ["hour", "type"], ["CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER"], amount_scale=1.0
    )
    tx = Transaction("t", 1_700_000_000_000, "A", "B", 5.0, 0, 1, "D", 9.0, kind="TRANSFER",
                     src_balance=9.0, dst_balance=0.0)
    assert store.columns(tx)["hour"] == (1_700_000_000_000 // 1000) % 24  # no step: one hour per second
    tx.step = 77
    assert store.columns(tx)["hour"] == 77 % 24


def test_resuming_after_a_warm_up_gives_the_scores_of_walking_the_whole_file(tmp_path):
    path = make_csv(tmp_path / "paysim.csv")
    whole = rp.replay(path, parts=rp.PARTS, chunk=50).scores  # every row, in order
    k = 120
    engine = load_lightgbm_engine(amount_scale=1.0)
    assert ps.warm_up(engine, path, k) == k
    resumed = [engine.handle(tx, tx.ts).score for tx in rp.transactions(path, start=k)]
    np.testing.assert_allclose(resumed, whole[k:], atol=1e-4)  # the engine rounds a score to 4 decimals
