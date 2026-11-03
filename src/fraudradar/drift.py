"""Drift monitoring, and the data side of retraining.

    python -m fraudradar.drift --csv training/data/PS_20174392719_1491204439457_log.csv

A trained model goes stale when the traffic it sees stops looking like the traffic it learned from. Two things
can be watched without waiting for fraud labels, which arrive days late in real life:

* **The spread of each input feature** against the training part (`reference_stats.json`), as a population
  stability index per feature (the measure `skew.py` uses). Only features the model splits on count: a
  feature it never uses cannot change a score.
* **The share of payments flagged.** If fraudsters change tactics the model stops flagging them, and the flag
  rate falls while the inputs may hardly move, because fraud is a fraction of a percent of all payments and
  moves no decile. The monitor takes the rate of its first full window as the baseline and reports a window
  whose count is more than `z_limit` standard errors away.

`DriftMonitor` is what the scorer runs on its live stream. `scan` replays a log through the same code in
windows of hours, so the monitor can be tried on the real PaySim file, and on the same file with drift
injected (`inflation`, `evasion`), which is the only way to see it fire: PaySim has no real change of regime.
"""

from __future__ import annotations

import argparse
import json
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from .lgbm import ARTIFACT_DIR, META_FILE, LightGBMScorer
from .payment_features import PaymentFeatureStore
from .replay import average_precision, transactions
from .schemas import Transaction
from .skew import LARGE, MODERATE, compare, load_reference

Perturb = Callable[[Transaction], Transaction]


# --- monitor -------------------------------------------------------------------------------------------------

class DriftMonitor:
    """A ring of the last `window` payments' feature rows and flags, and a report on them.

    `splits` maps feature -> how often the model splits on it (None: count every feature).
    """

    def __init__(
        self,
        reference: dict,
        names: list[str],
        splits: dict[str, int] | None = None,
        window: int = 20_000,
        min_rows: int = 2_000,
        baseline_alert_rate: float | None = None,
        z_limit: float = 4.0,
    ) -> None:
        self.reference, self.names, self.splits = reference, list(names), splits
        self.window, self.min_rows, self.z_limit = window, min(min_rows, window), z_limit
        self.baseline = baseline_alert_rate
        self._x = np.full((window, len(self.names)), np.nan)
        self._flag = np.zeros(window, dtype=bool)
        self._next = 0
        self._filled = 0
        self.seen = 0

    def observe(self, rows, flagged) -> None:
        x = np.asarray(rows, dtype=np.float64).reshape(-1, len(self.names))
        f = np.asarray(flagged, dtype=bool)
        self.seen += len(x)
        if len(x) >= self.window:
            x, f = x[-self.window:], f[-self.window:]
        at = (self._next + np.arange(len(x))) % self.window
        self._x[at], self._flag[at] = x, f
        self._next = (self._next + len(x)) % self.window
        self._filled = min(self.window, self._filled + len(x))

    def report(self) -> dict:
        n = self._filled
        if n < self.min_rows:
            return {"status": "warming up", "rows": n, "needed": self.min_rows, "seen": self.seen}
        x, flags = self._x[:n], self._flag[:n]
        skew = compare(self.reference, x, self.names, "all", self.splits)
        used = [r for r in skew if r.splits is None or r.splits > 0]
        worst = max(used, key=lambda r: r.psi) if used else None

        rate = float(flags.mean())
        if self.baseline is None and n == self.window:
            self.baseline = rate  # the first full window after the model went live is what "normal" means
        alert = {"rate": rate, "flagged": int(flags.sum()), "baseline": self.baseline, "z": None, "status": "calibrating"}
        if self.baseline is not None:
            p = min(max(self.baseline, 1e-6), 1 - 1e-6)
            alert["z"] = float((flags.sum() - n * p) / np.sqrt(n * p * (1 - p)))
            alert["status"] = "ok" if abs(alert["z"]) < self.z_limit else ("low" if alert["z"] < 0 else "high")

        reasons = []
        if worst is not None and worst.psi >= LARGE:
            reasons.append(f"{worst.feature} spread is very different from training (PSI {worst.psi:.2f})")
        if alert["status"] in ("low", "high"):
            reasons.append(f"flag rate is {alert['status']}: {rate:.3%} against {self.baseline:.3%} at the start")
        status = "drift" if reasons else ("watch" if worst is not None and worst.psi >= MODERATE else "ok")
        return {
            "status": status,
            "reasons": reasons,
            "rows": n,
            "seen": self.seen,
            "worst_used": None if worst is None else {"feature": worst.feature, "psi": worst.psi},
            "alert": alert,
            "features": [
                {"feature": r.feature, "psi": r.psi, "verdict": r.verdict, "splits": r.splits,
                 "median_reference": r.median_reference, "median_live": r.median_live}
                for r in sorted(skew, key=lambda r: -r.psi)
            ],
        }


def monitor_for(directory: str | Path = ARTIFACT_DIR, scorer: LightGBMScorer | None = None, **kwargs) -> DriftMonitor:
    scorer = scorer or LightGBMScorer.load(directory)
    splits = dict(zip(scorer.feature_names, (int(n) for n in scorer.booster.feature_importance("split"))))
    return DriftMonitor(load_reference(directory), scorer.feature_names, splits, **kwargs)


# --- injected drift --------------------------------------------------------------------------------------------

def inflation(factor: float, from_step: int) -> Perturb:
    """Every amount and balance is `factor` times larger from hour `from_step` on. Prices rose, or the currency
    was redenominated. Ratios do not move; sizes do."""

    def apply(tx: Transaction) -> Transaction:
        if tx.step is None or tx.step < from_step:
            return tx
        return replace(
            tx, amount=tx.amount * factor,
            src_balance=None if tx.src_balance is None else tx.src_balance * factor,
            dst_balance=None if tx.dst_balance is None else tx.dst_balance * factor,
        )

    return apply


def evasion(from_step: int, low: float = 0.2, high: float = 0.8, seed: int = 7) -> Perturb:
    """From hour `from_step` on, fraud no longer empties the account: it takes between `low` and `high` of the
    balance. PaySim's fraud always takes all of it, and so does what the model learned."""

    def apply(tx: Transaction) -> Transaction:
        if tx.truth is None or tx.step is None or tx.step < from_step or not tx.src_balance:
            return tx
        u = (zlib.crc32(f"{seed}:{tx.tx_id}".encode()) % 10_000) / 10_000
        return replace(tx, amount=round(tx.src_balance * (low + (high - low) * u), 2))

    return apply


SCENARIOS = {"none": lambda step: None, "inflation": lambda step: inflation(3.0, step), "evasion": lambda step: evasion(step)}


# --- the log as a feature matrix ------------------------------------------------------------------------------

@dataclass(slots=True)
class Dataset:
    X: np.ndarray  # float32, one row per payment in file order; the columns of the model
    y: np.ndarray  # bool, fraud
    step: np.ndarray  # int32, PaySim hour
    names: list[str]
    seconds: float = 0.0


def feature_matrix(
    path: str | Path,
    directory: str | Path = ARTIFACT_DIR,
    perturb: Perturb | None = None,
    limit: int | None = None,
) -> Dataset:
    """Walk the log in order through the serving feature store (so the columns are exactly what the scorer
    computes), optionally changing each payment first. About a minute and a half for the whole file."""
    meta = json.loads((Path(directory) / META_FILE).read_text(encoding="utf-8"))
    names = list(meta["features"])
    store = PaymentFeatureStore(names, meta["type_categories"], amount_scale=1.0)
    chunks, labels, steps = [], [], []
    rows: list[list[float]] = []
    start = time.perf_counter()
    for tx in transactions(path, limit):
        if perturb is not None:
            tx = perturb(tx)
        cols = store.columns(tx)
        rows.append([cols[n] for n in names])
        labels.append(tx.truth is not None)
        steps.append(tx.step)
        store.update(tx)
        if len(rows) >= 200_000:
            chunks.append(np.array(rows, dtype=np.float32))
            rows = []
    if rows:
        chunks.append(np.array(rows, dtype=np.float32))
    return Dataset(
        np.vstack(chunks), np.array(labels, dtype=bool), np.array(steps, dtype=np.int32), names,
        time.perf_counter() - start,
    )


# --- scan ------------------------------------------------------------------------------------------------------

def scan(
    ds: Dataset,
    scorer: LightGBMScorer,
    reference: dict,
    first_step: int,
    last_step: int | None = None,
    window_hours: int = 24,
    review: float | None = None,
    scores: np.ndarray | None = None,
) -> list[dict]:
    """One row per window of `window_hours`: what the monitor would have said, next to how the model really did.

    Not a simulation of the live monitor's ring (a window here is a fixed span of hours, not a count of
    payments), but the same PSI, the same used-feature rule and the same flag-rate test, against a baseline
    taken from the first window.
    """
    review = scorer.default_review if review is None else review
    scores = scorer.score_batch(ds.X) if scores is None else scores
    splits = dict(zip(scorer.feature_names, (int(n) for n in scorer.booster.feature_importance("split"))))
    last_step = int(ds.step.max()) if last_step is None else last_step
    out, baseline = [], None
    for lo in range(first_step, last_step + 1, window_hours):
        hi = min(lo + window_hours - 1, last_step)
        m = (ds.step >= lo) & (ds.step <= hi)
        n = int(m.sum())
        if n == 0:
            continue
        y, s = ds.y[m], scores[m]
        flagged = s >= review
        rate = float(flagged.mean())
        baseline = rate if baseline is None else baseline
        p = min(max(baseline, 1e-6), 1 - 1e-6)
        z = float((flagged.sum() - n * p) / np.sqrt(n * p * (1 - p)))
        skew = [r for r in compare(reference, ds.X[m].astype(np.float64), ds.names, "all", splits) if r.splits]
        worst = max(skew, key=lambda r: r.psi)
        out.append({
            "hours": [lo, hi],
            "payments": n,
            "fraud": int(y.sum()),
            "flagged": int(flagged.sum()),
            "flag_rate": rate,
            "flag_rate_z": z,
            "worst_psi": worst.psi,
            "worst_feature": worst.feature,
            "features_different": [r.feature for r in skew if r.psi >= LARGE],
            "psi": {r.feature: r.psi for r in skew},
            "average_precision": average_precision(s, y) if y.sum() >= 5 else None,
            "recall_at_review": float((flagged & y).sum() / y.sum()) if y.sum() else None,
        })
    return out


def print_scan(rows: list[dict]) -> None:
    print(f"{'hours':<10}{'payments':>10}{'fraud':>7}{'flag rate':>11}{'z':>7}{'worst PSI':>11}  {'feature':<22}{'AP':>8}{'recall':>8}")
    for r in rows:
        ap = "" if r["average_precision"] is None else f"{r['average_precision']:.4f}"
        rec = "" if r["recall_at_review"] is None else f"{r['recall_at_review']:.3f}"
        print(f"{r['hours'][0]:>4}-{r['hours'][1]:<5}{r['payments']:>10,}{r['fraud']:>7}{r['flag_rate']:>11.3%}{r['flag_rate_z']:>7.1f}"
              f"{r['worst_psi']:>11.3f}  {r['worst_feature']:<22}{ap:>8}{rec:>8}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", help="PaySim CSV; default: the first CSV in training/data or training/archive")
    parser.add_argument("--scenario", choices=list(SCENARIOS), default="none", help="inject drift into the log first")
    parser.add_argument("--from-step", type=int, default=400, help="hour at which the injected drift starts")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--reference-dir", default=str(ARTIFACT_DIR))
    parser.add_argument("--json", help="also write the windows to this file")
    args = parser.parse_args()

    from .replay import find_csv

    path = Path(args.csv) if args.csv else find_csv()
    scorer = LightGBMScorer.load(args.reference_dir)
    reference = load_reference(args.reference_dir)
    first = int(scorer.meta["split"]["valid_end_hour"]) + 1
    print(f"Walking {path.name} (scenario: {args.scenario}) ...")
    ds = feature_matrix(path, args.reference_dir, SCENARIOS[args.scenario](args.from_step))
    print(f"{len(ds.y):,} payments in {ds.seconds:.0f}s. Scanning the model's test part, from hour {first}.")
    rows = scan(ds, scorer, reference, first, window_hours=args.window_hours)
    print_scan(rows)
    if args.json:
        Path(args.json).write_text(json.dumps({"scenario": args.scenario, "from_step": args.from_step, "windows": rows}, indent=1), encoding="utf-8")
        print("wrote", args.json)


if __name__ == "__main__":
    main()
