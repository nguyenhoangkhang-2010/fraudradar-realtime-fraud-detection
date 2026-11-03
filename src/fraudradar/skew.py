"""Training-serving skew check.

    python -m fraudradar.skew

The model learned from PaySim's feature values. If the features the stream feeds it
are spread differently, its scores mean something other than what the notebook
measured. This replays the simulator through the same `PaymentFeatureStore` the
scorer uses and compares every feature with `reference_stats.json`, which the
notebook writes from the training part of PaySim.

The measure is the population stability index (PSI) over the bins stored in the reference
(one per value for flags and the payment type, deciles for the rest), with the usual rule of thumb: below 0.1 the spread is close, 0.1 to 0.25 it
has moved, above 0.25 it is a different distribution. Honest traffic is compared with
all PaySim payments (99.87% honest); the ring is compared with PaySim's fraud. The ring
is a few dozen payments, so its figures are noisy.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .generator import TransactionGenerator
from .lgbm import ARTIFACT_DIR, META_FILE
from .payment_features import AMOUNT_SCALE, PaymentFeatureStore

REFERENCE_FILE = "reference_stats.json"
FLOOR = 1e-4  # a share below this counts as this, so an empty bin does not send PSI to infinity
MODERATE, LARGE = 0.1, 0.25


def load_reference(directory: str | Path = ARTIFACT_DIR) -> dict:
    path = Path(directory) / REFERENCE_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Re-run training/paysim_lightgbm.ipynb (it now writes {REFERENCE_FILE}) "
            "and put the file next to the model."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def n_bins(entry: dict) -> int:
    if entry["kind"] == "categorical":
        return len(entry["bins"])
    if entry["kind"] == "discrete":
        return len(entry["values"]) + 1  # the last bin takes any value the reference never saw
    return len(entry["edges"]) + 1


def bin_codes(entry: dict, values: np.ndarray) -> np.ndarray:
    """Bin index per value, -1 for a missing one. The same rules the notebook used to build the reference."""
    values = np.asarray(values, dtype=np.float64)
    missing = np.isnan(values)
    if entry["kind"] == "categorical":
        codes = np.where(missing, -1, values).astype(np.int64)
        return np.where((codes >= 0) & (codes < len(entry["bins"])), codes, -1)
    if entry["kind"] == "discrete":
        known = np.asarray(entry["values"], dtype=np.float64)
        at = np.minimum(np.searchsorted(known, values), len(known) - 1)
        return np.where(missing, -1, np.where(known[at] == values, at, len(known)))
    return np.where(missing, -1, np.searchsorted(entry["edges"], values, side="right"))


def shares(entry: dict, values: np.ndarray) -> tuple[np.ndarray, float]:
    """Share of the present values in each bin, and the share of values that are missing."""
    codes = bin_codes(entry, values)
    present = codes[codes >= 0]
    counts = np.bincount(present, minlength=n_bins(entry))
    return counts / max(1, len(present)), float((codes < 0).mean()) if len(codes) else 0.0


def psi(reference: np.ndarray, live: np.ndarray) -> float:
    ref = np.maximum(np.asarray(reference, dtype=np.float64), FLOOR)
    cur = np.maximum(np.asarray(live, dtype=np.float64), FLOOR)
    return float(np.sum((cur - ref) * np.log(cur / ref)))


QUANTILES = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
MAX_DISCRETE = 12  # a feature with this many distinct values or fewer gets one bin per value


def build_reference(matrix: np.ndarray, fraud: np.ndarray, names: list[str], type_categories: list[str], rows_of: str = "train") -> dict:
    """`reference_stats.json` for a model trained on `matrix` (one row per payment), as the notebook writes it.

    The bins are cut here, from these rows: deciles for continuous features, one bin per value for flags and small
    counts. Pass the float32 matrix the model was trained on, so the cuts equal the notebook's.
    """
    fraud = np.asarray(fraud, dtype=bool)
    features = {}
    for j, col in enumerate(names):
        x = np.asarray(matrix[:, j], dtype=np.float64)
        entry: dict = {}
        if col == "type":
            codes = np.where(np.isnan(x), -1, x).astype(np.int64)
            codes = np.where((codes >= 0) & (codes < len(type_categories)), codes, -1)
            n = len(type_categories)
            entry["kind"], entry["bins"] = "categorical", list(type_categories)
        else:
            finite = x[~np.isnan(x)]
            distinct = np.unique(finite)
            if len(distinct) <= MAX_DISCRETE:
                at = np.minimum(np.searchsorted(distinct, x), len(distinct) - 1)
                codes = np.where(np.isnan(x), -1, np.where(distinct[at] == x, at, len(distinct)))
                n = len(distinct) + 1
                entry["kind"], entry["values"] = "discrete", distinct.tolist()
            else:
                edges = np.unique(np.quantile(finite, np.linspace(0.1, 0.9, 9)))
                codes = np.where(np.isnan(x), -1, np.searchsorted(edges, x, side="right"))
                n = len(edges) + 1
                entry["kind"], entry["edges"] = "numeric", edges.tolist()
        for group, mask in (("all", np.ones(len(codes), bool)), ("fraud", fraud)):
            c = codes[mask]
            present = c[c >= 0]
            entry[f"share_{group}"] = (np.bincount(present, minlength=n) / max(1, len(present))).tolist()
            entry[f"missing_{group}"] = float((c < 0).mean()) if len(c) else 0.0
            if col != "type":
                xs = x[mask][~np.isnan(x[mask])]
                entry[f"quantiles_{group}"] = np.quantile(xs, QUANTILES).tolist() if len(xs) else []
        features[col] = entry
    return {
        "split": rows_of, "rows": int(len(matrix)), "fraud_rows": int(fraud.sum()),
        "quantiles": QUANTILES, "features": features,
    }


def verdict(value: float) -> str:
    return "close" if value < MODERATE else "moved" if value < LARGE else "DIFFERENT"


@dataclass(slots=True)
class FeatureSkew:
    feature: str
    psi: float
    verdict: str
    missing_reference: float
    missing_live: float
    median_reference: float | None  # numeric features only
    median_live: float | None
    splits: int | None = None  # how often the model splits on it; 0 means its skew cannot matter


def model_splits(directory: str | Path = ARTIFACT_DIR) -> dict[str, int] | None:
    """Split counts per feature from the bundled model, or None where LightGBM is not installed."""
    try:
        from .lgbm import LightGBMScorer

        booster = LightGBMScorer.load(directory).booster
    except (ImportError, RuntimeError):
        return None
    return dict(zip(booster.feature_name(), (int(n) for n in booster.feature_importance("split"))))


def compare(
    reference: dict,
    matrix: np.ndarray,
    names: list[str],
    group: str = "all",
    splits: dict[str, int] | None = None,
) -> list[FeatureSkew]:
    """`group` is "all" (PaySim payments) or "fraud"; `matrix` has one row per live payment."""
    out = []
    for j, name in enumerate(names):
        entry = reference["features"][name]
        live_shares, live_missing = shares(entry, matrix[:, j])
        score = psi(entry[f"share_{group}"], live_shares)
        med_ref = med_live = None
        if entry["kind"] != "categorical":
            q = entry.get(f"quantiles_{group}") or []
            med_ref = q[reference["quantiles"].index(0.5)] if q else None
            col = matrix[:, j][~np.isnan(matrix[:, j])]
            med_live = float(np.median(col)) if len(col) else None
        out.append(
            FeatureSkew(
                name, score, verdict(score), entry[f"missing_{group}"], live_missing, med_ref, med_live,
                None if splits is None else splits.get(name, 0),
            )
        )
    return out


def collect(
    seconds: int = 120,
    tps: int = 150,
    seed: int = 7,
    rings: int = 2,
    ring_size: int = 14,
    amount_scale: float = AMOUNT_SCALE,
    directory: str | Path = ARTIFACT_DIR,
) -> tuple[np.ndarray, np.ndarray, list[str], np.ndarray]:
    """Feature rows the scorer would see: (honest rows, ring rows, column names, raw honest amounts)."""
    meta = json.loads((Path(directory) / META_FILE).read_text(encoding="utf-8"))
    names = list(meta["features"])
    store = PaymentFeatureStore(names, meta["type_categories"], amount_scale=amount_scale)
    gen = TransactionGenerator(seed=seed)
    base = 1_700_000_000_000
    stream = [gen.legit(base + int(i * 1000 / tps)) for i in range(seconds * tps)]
    for k in range(rings):
        stream.extend(gen.ring(base + int((k + 1) * seconds * 1000 / (rings + 1)), size=ring_size))
    stream.sort(key=lambda t: t.ts)
    honest, ring, amounts = [], [], []
    for tx in stream:
        (honest if tx.truth is None else ring).append(store.features(tx)[0])
        if tx.truth is None:
            amounts.append(tx.amount)
        store.update(tx)
    return np.array(honest), np.array(ring), names, np.array(amounts)


def suggested_scale(reference: dict, raw_amounts: np.ndarray) -> float | None:
    """Scale that would put the median honest amount where PaySim's median amount is."""
    entry = reference["features"]["amount_log"]
    q = entry.get("quantiles_all") or []
    if not q or not len(raw_amounts):
        return None
    return float(math.expm1(q[reference["quantiles"].index(0.5)]) / np.median(raw_amounts))


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.3g}"


def _print(title: str, rows: list[FeatureSkew], n: int) -> None:
    print(f"\n{title} ({n:,} payments)")
    print(f"{'feature':<24}{'PSI':>8}  {'verdict':<10}{'median ref':>12}{'median live':>13}{'missing ref/live':>20}{'splits':>8}")
    for r in sorted(rows, key=lambda r: -r.psi):
        miss = f"{r.missing_reference:.1%} / {r.missing_live:.1%}"
        used = "" if r.splits is None else str(r.splits)
        print(f"{r.feature:<24}{r.psi:>8.3f}  {r.verdict:<10}{_fmt(r.median_reference):>12}{_fmt(r.median_live):>13}{miss:>20}{used:>8}")
    if rows and rows[0].splits is not None:
        hit = [r.feature for r in rows if r.verdict == "DIFFERENT" and r.splits]
        idle = [r.feature for r in rows if r.verdict == "DIFFERENT" and not r.splits]
        print(f"different and used by the model ({len(hit)}): {', '.join(hit) or 'none'}")
        print(f"different but never split on, so harmless ({len(idle)}): {', '.join(idle) or 'none'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--tps", type=int, default=150)
    parser.add_argument("--amount-scale", type=float, default=AMOUNT_SCALE)
    parser.add_argument("--reference-dir", default=str(ARTIFACT_DIR))
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args()

    reference = load_reference(args.reference_dir)
    honest, ring, names, amounts = collect(
        args.seconds, args.tps, args.seed, amount_scale=args.amount_scale, directory=args.reference_dir
    )
    splits = model_splits(args.reference_dir)
    honest_skew = compare(reference, honest, names, "all", splits)
    ring_skew = compare(reference, ring, names, "fraud", splits)
    scale = suggested_scale(reference, amounts)
    if args.json:
        print(json.dumps({
            "amount_scale": args.amount_scale,
            "suggested_amount_scale": scale,
            "honest": [vars_of(r) for r in honest_skew],
            "ring": [vars_of(r) for r in ring_skew],
        }, indent=2))
        return
    _print("Honest traffic vs all PaySim payments", honest_skew, len(honest))
    _print("Ring traffic vs PaySim fraud", ring_skew, len(ring))
    if scale is not None:
        print(f"\nAmount scale in use: {args.amount_scale:g}. Scale that would align the median amount: {scale:.3g}.")


def vars_of(r: FeatureSkew) -> dict:
    return {s: getattr(r, s) for s in r.__slots__}


if __name__ == "__main__":
    main()
