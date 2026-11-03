"""Inject drift into the real PaySim log, watch what the monitor and the model do, retrain, and measure the result.

    python scripts/drift_experiment.py --csv training/data/PS_20174392719_1491204439457_log.csv

For each scenario (see fraudradar.drift) the log is changed from hour `--from-step` on, then:

1. `scan`: for each 24 h window of the model's test part, the flag rate, the worst PSI among the features the model
   uses, and (with labels, which a real system gets late) the average precision and recall of the model in service.
2. For each `--nows` hour, a retrain as it would have happened then: labels known only up to `now - label_delay`, the
   challenger gated against the model in service on the newest labelled window, and, whatever the gate said, both
   measured on the held-out hours `--holdout`, which neither saw.

Everything here is simulated drift on a simulated dataset. It shows that the machinery works and how much a retrain
depends on how much labelled data of the new kind exists yet; it does not show how often real fraud drifts.
Results go to docs/results/drift_retrain/.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fraudradar.drift import SCENARIOS, feature_matrix, scan  # noqa: E402
from fraudradar.lgbm import ARTIFACT_DIR, LightGBMScorer  # noqa: E402
from fraudradar.replay import find_csv  # noqa: E402
from fraudradar.retrain import in_hours, metrics, retrain  # noqa: E402
from fraudradar.skew import load_reference  # noqa: E402

OUT = ROOT / "docs" / "results" / "drift_retrain"


def fraud_per_step(csv: Path) -> np.ndarray:
    """Fraud cases per PaySim hour. Injected drift never changes a payment's hour or label, so this holds for every scenario."""
    import pandas as pd

    frame = pd.read_csv(csv, usecols=["step", "isFraud"])
    return np.bincount(frame["step"].to_numpy(), weights=frame["isFraud"].to_numpy(), minlength=frame["step"].max() + 1)


def drifted_fraud(per_step: np.ndarray, from_step: int, hours: tuple[int, int]) -> int:
    lo, hi = max(from_step, hours[0]), hours[1]
    return int(per_step[lo:hi + 1].sum()) if hi >= lo else 0


def add_counts(result: dict, per_step: np.ndarray) -> dict:
    """How much of each retrain's data was already drifted: what the model learned from is the training part only."""
    for t in result["retrains"]:
        t["drifted_fraud_in_training"] = drifted_fraud(per_step, result["from_step"], tuple(t["windows"]["train"]))
        t["drifted_fraud_in_validation"] = drifted_fraud(per_step, result["from_step"], tuple(t["windows"]["valid"]))
    return result


def run_scenario(name: str, csv: Path, args: argparse.Namespace, champion: LightGBMScorer, reference: dict) -> dict:
    print(f"\n=== {name} from hour {args.from_step} ===", flush=True)
    ds = feature_matrix(csv, ARTIFACT_DIR, SCENARIOS[name](args.from_step))
    print(f"features for {len(ds.y):,} payments in {ds.seconds:.0f}s", flush=True)
    first = int(champion.meta["split"]["valid_end_hour"]) + 1
    windows = scan(ds, champion, reference, first, window_hours=24)
    if args.scan_only:
        return {"scenario": name, "from_step": args.from_step, "windows": windows, "retrains": []}

    hold = in_hours(ds, *args.holdout)
    champ_hold = metrics(champion.score_batch(ds.X[hold]), ds.y[hold], champion.meta["thresholds_chosen_on_validation"])
    rounds = []
    for now in args.nows:
        started = time.perf_counter()
        trained, report = retrain(
            ds, ARTIFACT_DIR, now, label_delay=args.label_delay, valid_hours=args.valid_hours, min_gain=args.min_gain
        )
        chall_hold = metrics(trained.booster.predict(ds.X[hold]), ds.y[hold], trained.meta["thresholds_chosen_on_validation"])
        rounds.append({
            "now": now,
            "windows": report["windows"],
                        "gate": {"promote": report["promote"], "why": report["why"],
                     "champion": report["champion_on_gate"], "challenger": report["challenger_on_gate"]},
            "holdout_challenger": chall_hold,
            "seconds": time.perf_counter() - started,
        })
        print(f"now={now}: gate={'PASS' if report['promote'] else 'fail'} ({report['why']}); "
              f"holdout AP {champ_hold['AP']:.4f} -> {chall_hold['AP']:.4f} "
              f"in {rounds[-1]['seconds']:.0f}s", flush=True)
    return {"scenario": name, "from_step": args.from_step, "windows": windows, "holdout_hours": list(args.holdout),
            "holdout_champion": champ_hold, "retrains": rounds}


def summary(results: list[dict]) -> str:
    lines = []
    for r in results:
        lines += [f"### {r['scenario']} from hour {r['from_step']}", "",
                  "| Window (hours) | Fraud | Flag rate | Worst PSI (used features) | AP | Recall at review |", "|---|---|---|---|---|---|"]
        for w in r["windows"]:
            ap = "" if w["average_precision"] is None else f"{w['average_precision']:.4f}"
            rec = "" if w["recall_at_review"] is None else f"{w['recall_at_review']:.3f}"
            lines.append(f"| {w['hours'][0]}-{w['hours'][1]} | {w['fraud']} | {w['flag_rate']:.3%} | {w['worst_psi']:.2f} ({w['worst_feature']}) | {ap} | {rec} |")
        h = r["holdout_champion"]
        lines += ["", f"Held-out hours {r['holdout_hours'][0]}-{r['holdout_hours'][1]} ({h['fraud']} fraud): model in service "
                      f"AP {h['AP']:.4f}, recall {h['recall@0.001']:.3f} and false alarms {h['false_alarm_rate@0.001']:.3%} of honest payments at its review threshold (target 0.100%).", "",
                  "| Retrain at hour | Labels known up to | Drifted fraud in training / validation | Gate | Held-out AP | Held-out recall at its review threshold | False alarms |",
                  "|---|---|---|---|---|---|---|"]
        for t in r["retrains"]:
            c = t["holdout_challenger"]
            lines.append(f"| {t['now']} | {t['windows']['gate'][1]} | {t['drifted_fraud_in_training']} / {t['drifted_fraud_in_validation']} | {'pass' if t['gate']['promote'] else 'fail'} | "
                         f"{c['AP']:.4f} | {c['recall@0.001']:.3f} | {c['false_alarm_rate@0.001']:.4%} |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv")
    parser.add_argument("--scenarios", default="evasion,inflation")
    parser.add_argument("--from-step", type=int, default=450)
    parser.add_argument("--nows", default="500,550,600", type=lambda s: [int(x) for x in s.split(",")])
    parser.add_argument("--label-delay", type=int, default=48)
    parser.add_argument("--valid-hours", type=int, default=36)
    parser.add_argument("--min-gain", type=float, default=0.002)
    parser.add_argument("--resummarize", action="store_true", help="rebuild the summary from the saved results, without rerunning")
    parser.add_argument("--scan-only", action="store_true", help="only the windows of the scan, no retrains (use with --tag)")
    parser.add_argument("--tag", default="", help="suffix for the result files, so another run does not overwrite these")
    parser.add_argument("--holdout", default="650,743", type=lambda s: tuple(int(x) for x in s.split(",")))
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    csv = Path(args.csv) if args.csv else find_csv()
    per_step = fraud_per_step(csv)
    if args.resummarize:
        saved = [add_counts(json.loads((OUT / f"experiment_{n}{args.tag}.json").read_text(encoding="utf-8")), per_step)
                 for n in args.scenarios.split(",")]
        for r in saved:
            (OUT / f"experiment_{r['scenario']}{args.tag}.json").write_text(json.dumps(r, indent=1, default=float), encoding="utf-8")
        (OUT / f"summary{args.tag}.md").write_text(summary(saved), encoding="utf-8")
        return
    champion = LightGBMScorer.load(ARTIFACT_DIR)
    reference = load_reference(ARTIFACT_DIR)
    results = []
    for name in args.scenarios.split(","):
        result = add_counts(run_scenario(name, csv, args, champion, reference), per_step)
        (OUT / f"experiment_{name}{args.tag}.json").write_text(json.dumps(result, indent=1, default=float), encoding="utf-8")
        results.append(result)
    (OUT / f"summary{args.tag}.md").write_text(summary(results), encoding="utf-8")
    print("\n" + summary(results))


if __name__ == "__main__":
    main()
