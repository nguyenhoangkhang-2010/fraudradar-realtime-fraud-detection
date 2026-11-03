import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiokafka")

from fraudradar.api.app import DRIFT_STALE_S, Hub  # noqa: E402


def report(scorer, status, model="paysim_v1", age_s=0.0, reasons=()):
    return {"scorer": scorer, "model": model, "status": status, "reasons": list(reasons),
            "ts": int((time.time() - age_s) * 1000)}


def test_no_report_means_no_badge():
    assert Hub().model_health() is None


def test_the_badge_shows_the_worst_status_among_the_scorers_and_their_models():
    hub = Hub()
    hub.ingest_drift(report("a", "ok"))
    hub.ingest_drift(report("b", "drift", model="v2", reasons=["amount_log moved"]))
    hub.ingest_drift(report("c", "watch"))
    health = hub.model_health()
    assert health == {"status": "drift", "reasons": ["amount_log moved"], "models": ["paysim_v1", "v2"], "scorers": 3}


def test_a_scorer_that_stopped_reporting_is_not_counted_and_a_new_report_replaces_its_old_one():
    hub = Hub()
    hub.ingest_drift(report("gone", "drift", age_s=DRIFT_STALE_S + 5))
    assert hub.model_health() is None
    hub.ingest_drift(report("a", "drift"))
    hub.ingest_drift(report("a", "ok"))
    assert hub.model_health()["status"] == "ok"
