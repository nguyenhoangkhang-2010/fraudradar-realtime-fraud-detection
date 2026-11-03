import math

import pytest

pytest.importorskip("numpy")
pytest.importorskip("aiokafka")

from fraudradar import loadtest as lt  # noqa: E402


def arrivals(latencies, start=1_000, gap=10):
    """(published, scored, arrived) for payments published `gap` ms apart, each taking the given time end to end."""
    return [(start + i * gap, start + i * gap + lat // 2, start + i * gap + lat) for i, lat in enumerate(latencies)]


def test_percentile_picks_the_value_at_the_rank():
    values = sorted(range(1, 101))
    assert lt.percentile(values, 0.5) == 51 and lt.percentile(values, 0.99) == 100
    assert math.isnan(lt.percentile([], 0.5))


def test_a_step_that_keeps_up_reports_its_delays_and_is_not_saturated():
    rows = arrivals([5] * 90 + [50] * 9 + [400])  # 100 payments, one slow tail
    step = lt.summarize_step(rate=100, sent=100, window_ms=(1_000, 2_000), arrivals=rows, send_seconds=1.0)
    assert step["decided"] == 100 and step["lost"] == 0 and not step["saturated"]
    assert step["e2e_ms"]["p50"] == 5 and step["e2e_ms"]["p99"] == 400 and step["e2e_ms"]["max"] == 400
    assert step["to_decision_ms"]["p50"] == 2  # half of the end-to-end time had passed when it was decided
    # those published just before the window ended were still being decided when it ended
    assert step["decided_in_window_rate"] == float(sum(1 for a in rows if a[2] <= 2_000)) < 100.0


def test_a_step_that_falls_behind_is_saturated_and_counts_only_what_finished_in_time():
    # 100 payments sent over 1 s; the scorer answers each 1.5 s after it was published
    rows = arrivals([1_500] * 100)
    step = lt.summarize_step(rate=100, sent=100, window_ms=(1_000, 2_000), arrivals=rows, send_seconds=1.0)
    assert step["decided"] == 100 and not step["saturated"]  # all came back, only late
    assert step["decided_in_window_rate"] < 50  # but few of them inside the window

    step = lt.summarize_step(rate=100, sent=100, window_ms=(1_000, 2_000), arrivals=rows[:60], send_seconds=1.0)
    assert step["lost"] == 40 and step["saturated"]  # under 99% came back


def test_decisions_of_other_steps_are_not_counted():
    rows = arrivals([5] * 20, start=500) + arrivals([5] * 20, start=1_000)  # the first 20 predate the window
    step = lt.summarize_step(rate=20, sent=20, window_ms=(1_000, 2_000), arrivals=rows, send_seconds=1.0)
    assert step["decided"] == 20
