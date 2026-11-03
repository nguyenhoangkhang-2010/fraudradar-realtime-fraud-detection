"""Draw docs/img/drift_retrain.png from the results in docs/results/drift_retrain/.

    python scripts/plot_drift.py

One column per injected scenario, the same real PaySim windows without drift (the control) drawn behind it. Top
to bottom: what the monitor can see without labels (the flag rate, and the worst PSI among the features the model
uses), then what only labels reveal (average precision of the model in service).
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "docs" / "results" / "drift_retrain"
SCENARIOS = [
    ("evasion", "Fraud changes: it stops emptying the account", "amount_to_oldbal"),
    ("inflation", "Amounts triple (inflation)", "amount_log"),
]
INK, MUTED, ACCENT, EDGE = "#1f2933", "#8a94a0", "#d6336c", "#d0d5dc"


def mid(w: dict) -> float:
    return (w["hours"][0] + w["hours"][1]) / 2


def main() -> None:
    control = json.loads((RES / "experiment_none_scan.json").read_text(encoding="utf-8"))["windows"]
    control = [w for w in control if w["payments"] >= 1000]  # the last stub windows hold a handful of payments
    fig, axes = plt.subplots(3, 2, figsize=(11, 8.2), sharex="col")
    for col, (name, title, target) in enumerate(SCENARIOS):
        data = json.loads((RES / f"experiment_{name}.json").read_text(encoding="utf-8"))
        scanned = json.loads((RES / f"experiment_{name}_scan.json").read_text(encoding="utf-8"))["windows"]
        windows = [w for w in scanned if w["payments"] >= 1000]
        start = data["from_step"]
        rows = [
            ("Flagged share of payments", "flag_rate", True, lambda v: v * 100),
            (f"PSI of {target}", "psi", False, lambda v: v),
            ("Average precision (needs labels)", "average_precision", False, lambda v: v),
        ]
        for r, (label, key, logy, f) in enumerate(rows):
            ax = axes[r][col]
            pick = (lambda w: w["psi"][target]) if key == "psi" else (lambda w, key=key: f(w[key]))
            ax.plot([mid(w) for w in control], [pick(w) for w in control], color=MUTED, lw=1.6, ls="--", label="same hours, no drift", zorder=3)
            ax.plot([mid(w) for w in windows], [pick(w) for w in windows], color=ACCENT, lw=2.2, marker="o", ms=3.5, label=name)
            ax.axvline(start, color=INK, lw=1, ls=":")
            if logy:
                ax.set_yscale("log")
            if r == 0:
                ax.set_title(title, fontsize=11, color=INK, loc="left")
                ax.set_ylabel("% of payments" if col == 0 else "")
            if r == 1:
                ax.axhline(0.25, color=EDGE, lw=1)
                ax.text(745, 0.27, "0.25: a different distribution", va="bottom", ha="right", fontsize=7.5, color=MUTED)
            if r == 2:
                ax.set_ylim(0, 1.03)
                ax.set_xlabel("PaySim hour")
                for t in data["retrains"]:
                    ax.axvline(t["now"], color=EDGE, lw=1)
                    ax.text(t["now"], 0.04, f" retrain\n at {t['now']}", fontsize=7.5, color=MUTED, va="bottom")
            if col == 0:
                ax.set_ylabel(label, fontsize=9)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            ax.tick_params(labelsize=8)
        axes[0][col].text(start, axes[0][col].get_ylim()[1], " drift starts", fontsize=8, color=INK, va="top")
    axes[0][0].legend(loc="lower left", fontsize=8, frameon=False)
    fig.tight_layout()
    out = ROOT / "docs" / "img" / "drift_retrain.png"
    fig.savefig(out, dpi=140)
    print("wrote", out)


if __name__ == "__main__":
    main()
