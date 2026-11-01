from fraudradar.evaluate import run_offline
from fraudradar.features import FeatureStore
from fraudradar.engine import ScoringEngine
from fraudradar.model import HeuristicScorer, decide
from fraudradar.schemas import Decision, Transaction


def test_decide_thresholds():
    assert decide(0.95, 0.85, 0.5) == "block"
    assert decide(0.6, 0.85, 0.5) == "review"
    assert decide(0.1, 0.85, 0.5) == "allow"


def test_engine_does_not_use_ground_truth():
    base = dict(tx_id="t", ts=1_000_000, src="A1", dst="M1", amount=40.0,
                src_city=0, dst_city=1, device="D1", src_age_days=300.0)
    clean = ScoringEngine(FeatureStore(), HeuristicScorer()).handle(Transaction(**base), 1_000_005)
    labeled = ScoringEngine(FeatureStore(), HeuristicScorer()).handle(
        Transaction(**base, truth="ring-001"), 1_000_005
    )
    assert clean.score == labeled.score and clean.action == labeled.action


def test_decision_roundtrips_through_bytes():
    d = ScoringEngine(FeatureStore(), HeuristicScorer()).handle(
        Transaction("t", 1_000_000, "A1", "M1", 40.0, 0, 1, "D1", 300.0), 1_000_012
    )
    again = Decision.from_bytes(d.to_bytes())
    assert again == d and again.latency_ms == 12


def test_rings_are_caught_without_blocking_honest_traffic():
    r = run_offline(seed=7, seconds=90, tps=120, ring_starts=(20, 55))
    assert r["false_block_rate"] < 0.002
    assert r["ring_block_recall"] > 0.6
    for rid, delay in r["seconds_to_first_block"].items():
        assert delay is not None and delay < 6, rid
