"""Serving a LightGBM model trained by training/paysim_lightgbm.ipynb.

`load_lightgbm_engine` pairs the model with the feature store that builds its inputs and
takes its default thresholds from the model's own metadata, where the notebook recorded
the score that gave a 0.1% (review) and a 0.01% (block) false-alarm rate on validation.
Those scores are tiny compared with the heuristic's, because the model's probabilities
follow the base rate of fraud in the data it was trained on.

`lightgbm` is imported inside `load`, so the rest of the package works without it.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .engine import ScoringEngine
from .payment_features import AMOUNT_SCALE, PaymentFeatureStore

ARTIFACT_DIR = Path(__file__).parent / "artifacts" / "paysim_v1"
MODEL_FILE = "lgbm_paysim.txt"
META_FILE = "metadata.json"

EXPLAIN_MIN = 0.5  # a feature must add at least this much log-odds to be quoted as a reason


def _explain(name: str, value: float) -> str:
    """One plain sentence for a feature that pushed the score up."""
    if name == "drains_account":
        return "payment empties the sender's account"
    if name == "amount_to_oldbal":
        return f"amount is {value:.1f}x the sender's balance"
    if name == "amount_log":
        return "unusually large amount"
    if name == "orig_old_zero":
        return "sender's balance was zero"
    if name == "dest_old_zero":
        return "recipient account was empty before"
    if name == "dest_is_merchant":
        return "recipient is a person, not a merchant"
    if name == "dest_prior_in":
        return f"recipient already received {int(value)} payments"
    if name == "dest_prior_in_sum_log":
        return "recipient has pooled a lot of money"
    if name == "hour":
        return f"hour {int(value)} of the day carried more fraud in training data"
    if name == "type":
        return "payment type is one fraud favours"
    return f"{name} = {value:.2f}"


class LightGBMScorer:
    def __init__(self, booster, meta: dict) -> None:
        self.booster = booster
        self.meta = meta
        self.feature_names: list[str] = list(meta["features"])
        thresholds = meta["thresholds_chosen_on_validation"]
        self.default_review: float = thresholds["fpr_0.001"]
        self.default_block: float = thresholds["fpr_0.0001"]
        self._explain_from = self.default_review  # explaining costs more than scoring

    @classmethod
    def load(cls, directory: str | Path = ARTIFACT_DIR) -> "LightGBMScorer":
        try:
            import lightgbm as lgb
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                'the lightgbm scorer needs LightGBM: pip install -e ".[model]"'
            ) from exc
        directory = Path(directory)
        meta = json.loads((directory / META_FILE).read_text(encoding="utf-8"))
        # Read the text and fix line endings ourselves: Git on Windows can turn the model file's
        # LF into CRLF (core.autocrlf), and LightGBM refuses to parse a CRLF model.
        text = (directory / MODEL_FILE).read_bytes().decode("utf-8").replace("\r\n", "\n")
        booster = lgb.Booster(model_str=text)
        if list(booster.feature_name()) != list(meta["features"]):
            raise ValueError("model file and metadata disagree about the feature list")
        return cls(booster, meta)

    def unused_features(self) -> list[str]:
        """Features no tree splits on: whatever value they get, the score is the same."""
        counts = self.booster.feature_importance("split")
        return [name for name, n in zip(self.feature_names, counts) if n == 0]

    def _reasons(self, contrib: np.ndarray, x_row: np.ndarray) -> list[str]:
        order = np.argsort(contrib)[::-1][:3]
        return [_explain(self.feature_names[i], float(x_row[i])) for i in order if contrib[i] >= EXPLAIN_MIN]

    def score_many(self, rows: list[np.ndarray]) -> list[tuple[float, list[str]]]:
        """`score` for many payments at once, one prediction call; only flagged ones pay for an explanation."""
        x = np.vstack(rows)
        p = self.booster.predict(x)
        out: list[tuple[float, list[str]]] = [(float(v), []) for v in p]
        hot = np.flatnonzero(p >= self._explain_from)
        if len(hot):
            contrib = self.booster.predict(x[hot], pred_contrib=True)[:, :-1]
            for k, i in enumerate(hot):
                out[i] = (out[i][0], self._reasons(contrib[k], x[i]))
        return out

    def score_batch(self, x: np.ndarray) -> np.ndarray:
        """Probabilities for many rows at once; `score` row by row gives the same numbers."""
        return self.booster.predict(x)

    def score(self, x: np.ndarray) -> tuple[float, list[str]]:
        p = float(self.booster.predict(x, num_threads=1)[0])
        if p < self._explain_from:
            return p, []
        contrib = self.booster.predict(x, pred_contrib=True, num_threads=1)[0][:-1]  # last is the bias
        return p, self._reasons(contrib, x[0])


def load_lightgbm_engine(
    directory: str | Path = ARTIFACT_DIR,
    block: float | None = None,
    review: float | None = None,
    amount_scale: float = AMOUNT_SCALE,
    redis_client=None,
    namespace: str = "ps",
) -> ScoringEngine:
    """With `redis_client` the history features live in Redis (see redis_features), otherwise in this process."""
    scorer = LightGBMScorer.load(directory)
    categories = scorer.meta["type_categories"]
    if redis_client is None:
        store = PaymentFeatureStore(scorer.feature_names, categories, amount_scale=amount_scale)
    else:
        from .redis_features import RedisPaymentFeatureStore

        store = RedisPaymentFeatureStore(
            redis_client, scorer.feature_names, categories, scorer.unused_features(), namespace, amount_scale
        )
    return ScoringEngine(
        store,
        scorer,
        block_threshold=scorer.default_block if block is None else block,
        review_threshold=scorer.default_review if review is None else review,
    )
