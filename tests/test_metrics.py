import time

import pytest

from fraudradar.api.metrics import Metrics


def decision(action="allow", truth=None, amount=100.0, latency=5.0, ts=1_000):
    return {"action": action, "truth": truth, "amount": amount, "latency_ms": latency, "ts": ts}


def test_an_empty_stream_reports_zeros_and_no_ratios():
    s = Metrics().snapshot()
    assert s["total"] == 0 and s["tps"] == 0 and s["latency_p99_ms"] == 0
    assert s["precision"] is None and s["recall"] is None and s["scenario"] is None


def test_the_confusion_counts_follow_truth_and_action():
    m = Metrics()
    m.ingest(decision("block", truth="ring-1", amount=500))  # caught fraud
    m.ingest(decision("allow", truth="ring-1", amount=300))  # missed fraud
    m.ingest(decision("block"))  # honest payment blocked
    m.ingest(decision("allow"))
    m.ingest(decision("review"))
    s = m.snapshot()
    assert (m.tp, m.fn, m.fp, m.tn) == (1, 1, 1, 2)
    assert s["total"] == 5 and s["blocked"] == 2 and s["reviewed"] == 1 and s["false_blocks"] == 1
    assert s["precision"] == pytest.approx(0.5) and s["recall"] == pytest.approx(0.5)
    assert s["fraud_blocked_amount"] == 500 and s["fraud_passed_amount"] == 300


def test_a_review_of_fraud_does_not_count_as_caught():
    m = Metrics()
    m.ingest(decision("review", truth="ring-1"))
    assert (m.tp, m.fn) == (0, 1)


def test_latency_percentiles_come_from_the_recent_samples_only():
    m = Metrics(latency_samples=100)
    for _ in range(50):
        m.ingest(decision(latency=1000.0))  # old, slow
    for latency in range(100):
        m.ingest(decision(latency=float(latency)))
    s = m.snapshot()
    assert s["latency_p50_ms"] == 50.0 and s["latency_p99_ms"] == 99.0


def test_throughput_counts_only_the_last_few_seconds(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    m = Metrics(rate_window_s=5.0)
    for _ in range(50):
        m.ingest(decision())
    assert m.snapshot()["tps"] == 10.0
    clock[0] += 6.0
    assert m.snapshot()["tps"] == 0.0


def test_a_scenario_tracks_its_first_block_and_the_money_each_way():
    m = Metrics()
    m.ingest(decision("allow", truth="ring-1", amount=40, ts=10_000))
    m.ingest(decision("block", truth="ring-1", amount=60, ts=12_500))
    m.ingest(decision("block", truth="ring-1", amount=10, ts=11_000))  # earlier than the first block seen
    sc = m.snapshot()["scenario"]
    assert sc == {"id": "ring-1", "tx": 3, "blocked": 2, "seconds_to_first_block": 1.0,
                  "amount_blocked": 70.0, "amount_passed": 40.0}


def test_a_scenario_never_blocked_has_no_time_to_block():
    m = Metrics()
    m.ingest(decision("allow", truth="ring-9", ts=5_000))
    assert m.snapshot()["scenario"]["seconds_to_first_block"] is None


def test_the_snapshot_describes_the_latest_scenario():
    m = Metrics()
    m.ingest(decision("block", truth="ring-1"))
    m.ingest(decision("block", truth="ring-2"))
    assert m.snapshot()["scenario"]["id"] == "ring-2"
