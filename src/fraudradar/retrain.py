"""Retrain the PaySim model from the log, compare it with the one in service, and promote it only if it is better.

    python -m fraudradar.retrain --csv training/data/PS_20174392719_1491204439457_log.csv --now 500 --promote

The notebook trains the model once, by hand. This does the same thing as a command that can be run again when
the drift monitor says the model has gone stale, with the pieces a rerun needs:

* **Features come from the serving code** (`drift.feature_matrix` walks the log through `PaymentFeatureStore`),
  so a retrained model cannot differ from the scorer in how its inputs are built.
* **Labels arrive late.** `--now H` is the hour of the log at which the retrain happens and `--label-delay D` how
  many hours a payment's fraud label takes to be known, so only payments up to hour `H - D` are used.
* **Three consecutive parts of that labelled history**, newest last: training (everything older), validation
  (`--valid-hours`; early stopping and the two thresholds, as in the notebook) and a *gate* window
  (`--valid-hours`) that neither the new model nor the old one has been tuned on.
* **A gate.** The challenger goes live only if it adds `--min-gain` average precision on the gate window, or the model
  in service has drifted off its false-alarm target and the challenger fixes it (see `gate`). Otherwise the report says why and nothing changes.
* **A registry** (`registry.py`): the new version is written next to the others and, if it passes, the pointer the
  scorers watch is moved to it. Moving it back is the rollback.

What this cannot tell you: how the new model will do on payments after the gate window. The experiment in
`scripts/drift_experiment.py` measures that on a held-out stretch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .drift import SCENARIOS, Dataset, feature_matrix
from .lgbm import ARTIFACT_DIR, META_FILE, MODEL_FILE, LightGBMScorer
from .registry import Registry
from .replay import at_threshold, average_precision, find_csv
from .skew import REFERENCE_FILE, build_reference

FPR_TARGETS = (1e-3, 1e-4)  # the review and block false-alarm rates, as in the notebook
PARAMS = dict(
    objective="binary", metric="average_precision", learning_rate=0.05, num_leaves=63, min_child_samples=100,
    bagging_fraction=0.8, bagging_freq=1, feature_fraction=0.8, lambda_l2=1.0, verbosity=-1,
)


@dataclass(slots=True)
class Trained:
    booster: object
    meta: dict
    reference: dict


def _sample(mask: np.ndarray, y: np.ndarray, neg_frac: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """All fraud rows and a share of the honest ones, with weights that undo the down-sampling."""
    idx = np.flatnonzero(mask)
    keep = np.random.default_rng(seed).random(len(idx)) < neg_frac
    idx = idx[y[idx] | keep]
    return idx, np.where(y[idx], 1.0, 1.0 / neg_frac)


def in_hours(ds: Dataset, lo: int, hi: int) -> np.ndarray:
    return (ds.step >= lo) & (ds.step <= hi)


def metrics(scores: np.ndarray, y: np.ndarray, thresholds: dict[str, float]) -> dict:
    out = {"payments": int(len(y)), "fraud": int(y.sum()), "AP": average_precision(scores, y)}
    for name, thr in thresholds.items():
        fpr = name.split("_", 1)[1]
        m = at_threshold(scores, y, thr)
        out.update({f"recall@{fpr}": m["recall"], f"precision@{fpr}": m["precision"], f"false_alarm_rate@{fpr}": m["false_alarm_rate"]})
    return out


def fit(
    ds: Dataset,
    type_categories: list[str],
    train: tuple[int, int],
    valid: tuple[int, int],
    parent: str | None = None,
    neg_frac: float = 0.25,
    max_rounds: int = 3000,
    seed: int = 7,
    learning_rate: float | None = None,
    threads: int = 0,
) -> Trained:
    """Train on hours `train` (inclusive), stop early and set thresholds on hours `valid`."""
    import lightgbm as lgb

    tr_mask, va_mask = in_hours(ds, *train), in_hours(ds, *valid)
    if int(ds.y[tr_mask].sum()) < 20 or int(ds.y[va_mask].sum()) < 20:
        raise ValueError("too few fraud cases in the training or validation hours to fit or tune a model")
    tr, w_tr = _sample(tr_mask, ds.y, neg_frac, seed)
    va, w_va = _sample(va_mask, ds.y, neg_frac, seed + 1)
    params = {**PARAMS, "seed": seed, "num_threads": threads}
    if learning_rate is not None:
        params["learning_rate"] = learning_rate
    cat = ["type"] if "type" in ds.names else "auto"

    def dataset(idx: np.ndarray, w: np.ndarray, ref=None):
        return lgb.Dataset(ds.X[idx], ds.y[idx].astype(int), weight=w, feature_name=ds.names, categorical_feature=cat, reference=ref)

    d_tr = dataset(tr, w_tr)
    booster = lgb.train(
        params, d_tr, num_boost_round=max_rounds, valid_sets=[dataset(va, w_va, d_tr)],
        callbacks=[lgb.early_stopping(100, verbose=False)],
    )
    best = booster.best_iteration or booster.current_iteration()
    # Keep exactly the rounds that were best, so the saved file is the model that was measured.
    booster = lgb.Booster(model_str=booster.model_to_string(num_iteration=best))

    p_val = booster.predict(ds.X[va_mask])
    y_val = ds.y[va_mask]
    thresholds = {f"fpr_{fpr:g}": float(np.quantile(p_val[~y_val], 1.0 - fpr)) for fpr in FPR_TARGETS}
    meta = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": "ealaxi/paysim1",
        "features": list(ds.names),
        "type_categories": list(type_categories),
        "best_iteration": int(best),
        "params": {"learning_rate": params["learning_rate"], "num_leaves": params["num_leaves"],
                   "min_child_samples": params["min_child_samples"], "subsample": params["bagging_fraction"],
                   "colsample_bytree": params["feature_fraction"], "reg_lambda": params["lambda_l2"], "random_state": seed},
        "split": {"train_end_hour": int(train[1]), "valid_end_hour": int(valid[1])},
        "trained_on_hours": [int(train[0]), int(train[1])],
        "validated_on_hours": [int(valid[0]), int(valid[1])],
        "thresholds_chosen_on_validation": thresholds,
        "alert_rate_valid": float((p_val >= thresholds["fpr_0.001"]).mean()),
        "parent": parent,
        "retrained": True,
        "versions": {"lightgbm": lgb.__version__, "numpy": np.__version__},
    }
    reference = build_reference(ds.X[tr_mask], ds.y[tr_mask], ds.names, type_categories)
    return Trained(booster, meta, reference)


def save(trained: Trained, directory: str | Path, report: dict | None = None) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    trained.booster.save_model(str(directory / MODEL_FILE))
    (directory / META_FILE).write_text(json.dumps(trained.meta, indent=2), encoding="utf-8")
    (directory / REFERENCE_FILE).write_text(json.dumps(trained.reference, indent=1), encoding="utf-8")
    if report is not None:
        (directory / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    return directory


def gate(champion: dict, challenger: dict, min_gain: float, target: float = 1e-3, trigger: float = 3.0, ceiling: float = 5.0) -> tuple[bool, str]:
    """Should the challenger replace the model in service? Both were measured on the gate window.

    Average precision alone is blind to one kind of drift: when the money in the log inflates, the ranking of
    payments stays good but the old model's fixed threshold flags far too many of them. So there are two ways in,
    and one way out: the challenger must add `min_gain` average precision, or the model in service must have
    drifted off its false-alarm target (more than `trigger` times `target` at its review threshold) while the
    challenger at least halves that without losing average precision; and a challenger whose own false-alarm rate
    is over `ceiling` times the target never goes in.
    """
    fa_key = f"false_alarm_rate@{target:g}"
    old_fa, new_fa = champion[fa_key], challenger[fa_key]
    gain = challenger["AP"] - champion["AP"]
    note = (f"average precision on the gate window {champion['AP']:.4f} -> {challenger['AP']:.4f} ({gain:+.4f}); "
            f"false alarms at the review threshold {old_fa:.3%} -> {new_fa:.3%} (target {target:.2%})")
    if new_fa > ceiling * target:
        return False, note + f"; the challenger is over {ceiling:g}x the false-alarm target"
    if gain >= min_gain:
        return True, note + f"; gain is at least {min_gain:g}"
    if old_fa > trigger * target and new_fa <= old_fa / 2 and gain >= -min_gain:
        return True, note + "; the model in service has drifted off its false-alarm target"
    return False, note + f"; no gain of {min_gain:g} and the model in service is on target"


def windows(now: int, label_delay: int, valid_hours: int, first: int = 1, history: int = 0) -> dict[str, tuple[int, int]]:
    """Training, validation and gate hours for a retrain at hour `now`; `history > 0` keeps only that many hours
    of training data."""
    labelled = now - label_delay
    gate_w = (labelled - valid_hours + 1, labelled)
    valid_w = (gate_w[0] - valid_hours, gate_w[0] - 1)
    train_w = (max(first, valid_w[0] - history) if history else first, valid_w[0] - 1)
    return {"train": train_w, "valid": valid_w, "gate": gate_w}


def retrain(
    ds: Dataset,
    champion_dir: str | Path,
    now: int,
    label_delay: int = 48,
    valid_hours: int = 36,
    history: int = 0,
    min_gain: float = 0.002,
    seed: int = 7,
    max_rounds: int = 3000,
    threads: int = 0,
) -> tuple[Trained, dict]:
    champion = LightGBMScorer.load(champion_dir)
    w = windows(now, label_delay, valid_hours, int(ds.step.min()), history)
    trained = fit(ds, champion.meta["type_categories"], w["train"], w["valid"], parent=Path(champion_dir).name,
                  seed=seed, max_rounds=max_rounds, threads=threads)
    gm = in_hours(ds, *w["gate"])
    y = ds.y[gm]
    import lightgbm as lgb  # noqa: F401  (the booster comes from fit)

    chall_scores = trained.booster.predict(ds.X[gm])
    champ_scores = champion.score_batch(ds.X[gm])
    chall = metrics(chall_scores, y, trained.meta["thresholds_chosen_on_validation"])
    champ = metrics(champ_scores, y, champion.meta["thresholds_chosen_on_validation"])
    ok, why = gate(champ, chall, min_gain)
    trained.meta["test_metrics"] = chall
    trained.meta["test_metrics_hours"] = list(w["gate"])
    report = {
        "now": now, "label_delay": label_delay, "windows": w, "champion": champion.meta.get("created"),
        "champion_dir": str(champion_dir), "champion_on_gate": champ, "challenger_on_gate": chall,
        "promote": ok, "why": why, "min_gain": min_gain,
    }
    return trained, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv")
    parser.add_argument("--now", type=int, required=True, help="hour of the log at which the retrain happens")
    parser.add_argument("--label-delay", type=int, default=48, help="hours until a payment's fraud label is known")
    parser.add_argument("--valid-hours", type=int, default=36, help="length of the validation and of the gate window")
    parser.add_argument("--history", type=int, default=0, help="train on only this many hours before validation (0: all)")
    parser.add_argument("--min-gain", type=float, default=0.002, help="average precision the challenger must add on the gate window")
    parser.add_argument("--registry", default="models")
    parser.add_argument("--name", help="version name; default v<date>-<time>")
    parser.add_argument("--champion-dir", help="model to beat; default: the registry's current model, else the bundled one")
    parser.add_argument("--promote", action="store_true", help="move the registry pointer to the new version if it passes the gate")
    parser.add_argument("--scenario", choices=list(SCENARIOS), default="none", help="inject drift into the log first (see drift.py)")
    parser.add_argument("--from-step", type=int, default=400)
    args = parser.parse_args()

    registry = Registry(args.registry)
    champion_dir = Path(args.champion_dir) if args.champion_dir else (registry.current() or ARTIFACT_DIR)
    path = Path(args.csv) if args.csv else find_csv()
    print(f"Champion: {champion_dir}. Walking {path.name} (scenario: {args.scenario}) ...")
    ds = feature_matrix(path, champion_dir, SCENARIOS[args.scenario](args.from_step))
    trained, report = retrain(ds, champion_dir, args.now, args.label_delay, args.valid_hours, args.history, args.min_gain)
    name = args.name or time.strftime("v%Y%m%d-%H%M%S")
    folder = save(trained, registry.root / name, report)
    print(json.dumps({k: report[k] for k in ("windows", "champion_on_gate", "challenger_on_gate", "promote", "why")}, indent=1))
    print("saved", folder)
    if report["promote"] and args.promote:
        registry.promote(name, report["why"])
        print(f"promoted {name}; scorers pick it up within a few seconds")
    elif args.promote:
        print("not promoted:", report["why"])


if __name__ == "__main__":
    main()
