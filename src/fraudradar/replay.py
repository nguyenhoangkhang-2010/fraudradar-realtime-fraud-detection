"""Replay the real PaySim log through the serving code.

    python -m fraudradar.replay --csv training/archive/PS_20174392719_1491204439457_log.csv

The skew check showed that the simulator's honest traffic looks little like PaySim's, so
numbers measured on the simulator say little about the model. This feeds the model the
rows of the real PaySim file instead, through the same `PaymentFeatureStore` and the same
bundled model the scorer service uses, in file order, and measures them against the
fraud labels. It scores the test part (hours after `valid_end_hour` in the model's
metadata), which the model never saw, and puts the result next to the figures the notebook
recorded for the same part.

It is also a parity check between training and serving: the store rebuilds the notebook's
features one payment at a time, so if it differs from the notebook's vectorised code, the
metrics here will not match. The whole file is walked in order, because the history
features count every earlier payment, but only the chosen part is scored.

Differences from the live scorer that are on purpose: payments are scored in chunks
(features do not depend on earlier decisions, so the numbers are the same as one by one;
a test checks that), and money is not rescaled, because PaySim is already in its own units.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .lgbm import ARTIFACT_DIR, META_FILE, LightGBMScorer
from .payment_features import SIM_HOUR_MS, PaymentFeatureStore
from .schemas import Transaction

COLUMNS = ("step", "type", "amount", "nameOrig", "oldbalanceOrg", "nameDest", "oldbalanceDest", "isFraud")
UNKNOWN_AGE_DAYS = 9999.0  # PaySim has no account age; the model does not use one
CHUNK = 50_000
PARTS = ("train", "valid", "test")
DEFAULT_LOCATIONS = ("training/data", "training/archive")


def find_csv(root: str | Path = ".") -> Path:
    for folder in DEFAULT_LOCATIONS:
        hits = sorted((Path(root) / folder).glob("*.csv"))
        if hits:
            return hits[0]
    raise FileNotFoundError(f"no PaySim CSV in {', '.join(DEFAULT_LOCATIONS)}; pass --csv")


def transactions(path: str | Path, limit: int | None = None) -> Iterator[Transaction]:
    """PaySim rows as transactions, in file order. `truth` marks fraud and is never read by the model."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        missing = [c for c in COLUMNS if c not in header]
        if missing:
            raise ValueError(f"{path} does not look like PaySim: missing columns {missing}")
        i_step, i_type, i_amt, i_src, i_old_src, i_dst, i_old_dst, i_fraud = (header.index(c) for c in COLUMNS)
        for n, row in enumerate(reader):
            if limit is not None and n >= limit:
                return
            dst = row[i_dst]
            yield Transaction(
                tx_id=f"P{n}",
                ts=int(row[i_step]) * SIM_HOUR_MS,
                src=row[i_src],
                dst=dst,
                amount=float(row[i_amt]),
                src_city=0,
                dst_city=0,
                device="",
                src_age_days=UNKNOWN_AGE_DAYS,
                truth="fraud" if row[i_fraud] == "1" else None,
                kind=row[i_type],
                src_balance=float(row[i_old_src]),
                dst_balance=float(row[i_old_dst]),
                dst_merchant=dst.startswith("M"),
            )


def part_of(step: int, split: dict) -> str:
    if step <= split["train_end_hour"]:
        return "train"
    return "valid" if step <= split["valid_end_hour"] else "test"


@dataclass(slots=True)
class Replay:
    scores: np.ndarray  # one per scored payment, as the serving code computes them (float64 inputs)
    scores_f32: np.ndarray  # the same payments with float32 inputs, which is how the notebook fed the model
    labels: np.ndarray
    parts: np.ndarray
    rows_walked: int = 0
    seconds: float = 0.0


def replay(
    path: str | Path,
    parts: tuple[str, ...] = ("test",),
    directory: str | Path = ARTIFACT_DIR,
    limit: int | None = None,
    chunk: int = CHUNK,
) -> Replay:
    scorer = LightGBMScorer.load(directory)
    store = PaymentFeatureStore(scorer.feature_names, scorer.meta["type_categories"], amount_scale=1.0)
    split = scorer.meta["split"]
    names = store.feature_names
    scored = set(parts)

    rows: list[list[float]] = []
    out64: list[np.ndarray] = []
    out32: list[np.ndarray] = []
    labels: list[bool] = []
    where: list[str] = []

    def flush() -> None:
        if rows:
            x = np.array(rows, dtype=np.float64)
            out64.append(scorer.score_batch(x))
            out32.append(scorer.score_batch(x.astype(np.float32)))
            rows.clear()

    start = time.perf_counter()
    walked = 0
    for tx in transactions(path, limit):
        walked += 1
        part = part_of(tx.ts // SIM_HOUR_MS, split)
        if part in scored:
            cols = store.columns(tx)
            rows.append([cols[n] for n in names])
            labels.append(tx.truth is not None)
            where.append(part)
            if len(rows) >= chunk:
                flush()
        store.update(tx)
    flush()
    seconds = time.perf_counter() - start
    empty = np.array([], dtype=np.float64)
    return Replay(
        np.concatenate(out64) if out64 else empty,
        np.concatenate(out32) if out32 else empty,
        np.array(labels, dtype=bool),
        np.array(where),
        walked,
        seconds,
    )


def average_precision(scores: np.ndarray, labels: np.ndarray) -> float:
    """Area under the precision-recall curve, step-wise, ties handled as scikit-learn does."""
    positives = int(labels.sum())
    if positives == 0:
        return float("nan")
    order = np.argsort(-scores, kind="stable")
    s, y = scores[order], labels[order].astype(np.int64)
    tp, fp = np.cumsum(y), np.cumsum(1 - y)
    ends = np.r_[np.flatnonzero(np.diff(s)), len(s) - 1]  # last index of each distinct score
    tp, fp = tp[ends], fp[ends]
    precision, recall = tp / (tp + fp), tp / positives
    return float(np.sum((recall - np.r_[0.0, recall[:-1]]) * precision))


def at_threshold(scores: np.ndarray, labels: np.ndarray, threshold: float) -> dict:
    flagged = scores > threshold
    tp, fp = int((flagged & labels).sum()), int((flagged & ~labels).sum())
    fn = int((~flagged & labels).sum())
    honest = int((~labels).sum())
    return {
        "recall": tp / max(1, tp + fn),
        "precision": tp / max(1, tp + fp),
        "false_alarm_rate": fp / max(1, honest),
        "caught": tp,
        "missed": fn,
        "false_alarms": fp,
    }


def summarize(run: Replay, meta: dict, part: str = "test") -> dict:
    mask = run.parts == part
    y = run.labels[mask]
    thresholds = meta["thresholds_chosen_on_validation"]
    out = {
        "part": part,
        "payments": int(mask.sum()),
        "fraud": int(y.sum()),
        "average_precision": average_precision(run.scores[mask], y),
        "average_precision_float32": average_precision(run.scores_f32[mask], y),
        "at_threshold": {
            name: at_threshold(run.scores[mask], y, thr) for name, thr in thresholds.items()
        },
        "thresholds": thresholds,
    }
    if part == "test":
        out["notebook"] = meta["test_metrics"]
    return out


def _print(result: dict, run: Replay) -> None:
    nb = result.get("notebook", {})
    print(f"\nPart: {result['part']}  ({result['payments']:,} payments, {result['fraud']:,} fraud)")
    print(f"{'metric':<34}{'replay':>12}{'notebook':>12}")
    ap = result["average_precision"]
    print(f"{'average precision':<34}{ap:>12.5f}{nb.get('AP', float('nan')):>12.5f}")
    print(f"{'  (float32 inputs, as in notebook)':<34}{result['average_precision_float32']:>12.5f}")
    for name, m in result["at_threshold"].items():
        fpr = name.split("_", 1)[1]
        label = {"0.001": "review", "0.0001": "block"}.get(fpr, name)
        print(f"-- flagged above the {label} threshold {result['thresholds'][name]:.5f}"
              f" ({fpr} of honest payments on validation)")
        for key in ("recall", "precision", "false_alarm_rate"):
            ref = nb.get(f"{key}@{fpr}")
            shown = "" if ref is None else f"{ref:>12.5f}"
            print(f"{'  ' + key:<34}{m[key]:>12.5f}{shown}")
        print(f"{'  caught / missed / false alarms':<34}{m['caught']:>12,}{m['missed']:>12,}{m['false_alarms']:>12,}")
    print(f"\nWalked {run.rows_walked:,} rows in {run.seconds:.0f}s ({run.rows_walked / max(run.seconds, 1e-9):,.0f} rows/s).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", help="PaySim CSV; default: the first CSV in training/data or training/archive")
    parser.add_argument("--part", choices=(*PARTS, "all"), default="test")
    parser.add_argument("--limit", type=int, help="only the first N rows of the file (a quick look)")
    parser.add_argument("--reference-dir", default=str(ARTIFACT_DIR), help="folder with the model and its metadata")
    parser.add_argument("--json", help="also write the results to this file")
    args = parser.parse_args()

    path = Path(args.csv) if args.csv else find_csv()
    parts = PARTS if args.part == "all" else (args.part,)
    meta = json.loads((Path(args.reference_dir) / META_FILE).read_text(encoding="utf-8"))
    print(f"Replaying {path} ({', '.join(parts)}) ...")
    run = replay(path, parts, args.reference_dir, args.limit)
    results = [summarize(run, meta, p) for p in parts]
    for r in results:
        _print(r, run)
    if args.json:
        Path(args.json).write_text(
            json.dumps({"csv": path.name, "rows_walked": run.rows_walked, "results": results}, indent=1),
            encoding="utf-8",
        )
        print("wrote", args.json)


if __name__ == "__main__":
    main()
