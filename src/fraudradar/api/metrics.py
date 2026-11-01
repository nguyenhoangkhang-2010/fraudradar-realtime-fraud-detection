"""Rolling operational and detection metrics derived from the decision stream."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class _Scenario:
    id: str
    first_fraud_ts: int
    first_block_ts: int | None = None
    tx: int = 0
    blocked: int = 0
    amount_blocked: float = 0.0
    amount_passed: float = 0.0

    def snapshot(self) -> dict:
        ttd = None
        if self.first_block_ts is not None:
            ttd = round((self.first_block_ts - self.first_fraud_ts) / 1000, 2)
        return {
            "id": self.id,
            "tx": self.tx,
            "blocked": self.blocked,
            "seconds_to_first_block": ttd,
            "amount_blocked": round(self.amount_blocked, 2),
            "amount_passed": round(self.amount_passed, 2),
        }


def _pct(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


class Metrics:
    def __init__(self, latency_samples: int = 2000, rate_window_s: float = 5.0) -> None:
        self._arrivals: deque[float] = deque()
        self._latencies: deque[float] = deque(maxlen=latency_samples)
        self._rate_window_s = rate_window_s
        self.total = 0
        self.blocked = 0
        self.reviewed = 0
        self.tp = self.fp = self.fn = self.tn = 0
        self.fraud_blocked_amount = 0.0
        self.fraud_passed_amount = 0.0
        self._scenarios: dict[str, _Scenario] = {}
        self._latest_scenario: str | None = None

    def ingest(self, d: dict) -> None:
        now = time.monotonic()
        self._arrivals.append(now)
        self._latencies.append(float(d["latency_ms"]))
        self.total += 1
        action = d["action"]
        is_fraud = d.get("truth") is not None
        is_block = action == "block"
        self.blocked += is_block
        self.reviewed += action == "review"
        if is_fraud and is_block:
            self.tp += 1
            self.fraud_blocked_amount += d["amount"]
        elif is_fraud:
            self.fn += 1
            self.fraud_passed_amount += d["amount"]
        elif is_block:
            self.fp += 1
        else:
            self.tn += 1

        if is_fraud:
            sid = d["truth"]
            sc = self._scenarios.get(sid)
            if sc is None:
                sc = self._scenarios[sid] = _Scenario(sid, first_fraud_ts=d["ts"])
                self._latest_scenario = sid
            sc.first_fraud_ts = min(sc.first_fraud_ts, d["ts"])
            sc.tx += 1
            if is_block:
                sc.blocked += 1
                sc.amount_blocked += d["amount"]
                if sc.first_block_ts is None or d["ts"] < sc.first_block_ts:
                    sc.first_block_ts = d["ts"]
            else:
                sc.amount_passed += d["amount"]

    def snapshot(self) -> dict:
        now = time.monotonic()
        while self._arrivals and now - self._arrivals[0] > self._rate_window_s:
            self._arrivals.popleft()
        lat = sorted(self._latencies)
        precision = self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None
        recall = self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None
        return {
            "tps": round(len(self._arrivals) / self._rate_window_s, 1),
            "latency_p50_ms": round(_pct(lat, 0.50), 1),
            "latency_p99_ms": round(_pct(lat, 0.99), 1),
            "total": self.total,
            "blocked": self.blocked,
            "reviewed": self.reviewed,
            "false_blocks": self.fp,
            "fraud_blocked_amount": round(self.fraud_blocked_amount, 2),
            "fraud_passed_amount": round(self.fraud_passed_amount, 2),
            "precision": precision,
            "recall": recall,
            "scenario": self._scenarios[self._latest_scenario].snapshot()
            if self._latest_scenario
            else None,
        }
