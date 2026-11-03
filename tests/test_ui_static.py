"""The map page is plain HTML and JavaScript with no build step, so nothing compiles it. These checks catch the
breakage a compiler would: a script that does not parse, an element the script looks up that the page does not have,
a file the page links that is not there, and a message field the script reads that the server does not send."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "fraudradar" / "api" / "static"
PAGES = {"index.html": "app.js", "globe.html": "app-globe.js"}


def text(name):
    return (STATIC / name).read_text(encoding="utf-8")


def ids_in(html):
    return set(re.findall(r'\bid="([^"]+)"', html))


def ids_looked_up(js):
    return set(re.findall(r'\$\("([^"]+)"\)', js)) | set(re.findall(r'getElementById\("([^"]+)"\)', js))


@pytest.mark.parametrize("page,script", PAGES.items())
def test_every_element_the_script_looks_up_exists_on_its_page(page, script):
    missing = sorted(ids_looked_up(text(script)) - ids_in(text(page)))
    assert not missing, f"{script} looks up ids that {page} does not have: {missing}"


@pytest.mark.parametrize("page", PAGES)
def test_no_id_appears_twice_on_a_page(page):
    found = re.findall(r'\bid="([^"]+)"', text(page))
    assert len(found) == len(set(found))


@pytest.mark.parametrize("page", PAGES)
def test_local_files_a_page_links_exist(page):
    html = text(page)
    refs = re.findall(r'(?:src|href)="([^"#]+)"', html)
    local = [r for r in refs if not r.startswith(("http://", "https://", "//", "data:"))]
    assert local, "expected at least the script and the stylesheet"
    assert [r for r in local if not (STATIC / r).exists()] == []


def test_the_pages_pin_their_cdn_libraries_to_exact_versions():
    for page in PAGES:
        for url in re.findall(r'(?:src|href)="(https://cdn[^"]+)"', text(page)):
            assert re.search(r"@\d+\.\d+\.\d+/", url), f"{url} is not pinned to a version"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed (CI checks this in its own job)")
@pytest.mark.parametrize("script", PAGES.values())
def test_the_script_parses(script):
    done = subprocess.run(["node", "--check", str(STATIC / script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# --- the contract with the server --------------------------------------------------------------------------------

def test_the_tick_fields_the_map_reads_are_the_ones_the_server_sends():
    pytest.importorskip("fastapi")
    pytest.importorskip("aiokafka")
    from fraudradar.api.app import Hub

    hub = Hub()
    sample = {"tx_id": "t", "ts": 1, "src": "A", "dst": "B", "amount": 1.0, "src_city": 1, "dst_city": 2, "score": 0.5,
              "action": "block", "scored_ts": 2, "latency_ms": 1.0, "reasons": [], "truth": "ring-1"}
    hub.ingest(sample)
    item = hub._compact([sample])[0]
    stats = hub.metrics.snapshot()

    js = text("app.js")
    for field in set(re.findall(r"\bit\.([a-z_]+)", js)) - {"go", "label"}:  # `go` and `label` belong to other objects
        assert field in item, f"app.js reads item.{field}, which the server does not send"
    for field in set(re.findall(r"\bs\.([a-z_0-9]+)", js[js.index("function renderKpis"):js.index("function renderHealth")])):
        assert field in stats, f"renderKpis reads stats.{field}, which the server does not send"
    for field in ("tps", "latency_p99_ms", "total", "blocked", "fraud_blocked_amount", "precision", "recall", "scenario"):
        assert field in stats


def test_the_scenario_fields_the_incident_card_reads_are_in_the_snapshot():
    pytest.importorskip("fastapi")
    from fraudradar.api.metrics import Metrics

    m = Metrics()
    m.ingest({"action": "block", "truth": "ring-1", "amount": 1.0, "latency_ms": 1.0, "ts": 1})
    scenario = m.snapshot()["scenario"]
    js = text("app.js")
    for field in set(re.findall(r"\bsc\.([a-z_]+)", js)):
        assert field in scenario, f"app.js reads scenario.{field}, which the server does not send"


def test_the_health_badge_knows_every_status_the_monitor_can_report():
    pytest.importorskip("numpy")
    from fraudradar.api.app import HEALTH_RANK

    labels = re.search(r"HEALTH_LABEL = \{([^}]*)\}", text("app.js")).group(1)
    known = set(re.findall(r'(?:^|,)\s*"?([a-z ]+?)"?\s*:', labels))
    assert set(HEALTH_RANK) <= known, f"the badge has no label for {set(HEALTH_RANK) - known}"


def test_the_packaged_static_files_are_listed_for_installation():
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "api/static/**/*" in pyproject and any(STATIC.iterdir())
