"""Feature store for the PaySim-trained model.

Same contract as `features.FeatureStore`: `features(tx)` is read-only and describes the
world *before* `tx`, `update(tx)` folds it in afterwards. The columns and their
definitions mirror `build_features` in training/paysim_lightgbm.ipynb; the model's
metadata lists the names and their order, and `PaymentFeatureStore` refuses to start
if they do not match, so a retrained model with other columns fails loudly.

Two things the model needs that a live feed does not hand over for free:

* Time. PaySim counts hours ("steps"). A transaction that carries its own `step` (a
  replay of the PaySim log) uses it. Otherwise the simulator runs one simulated hour per
  second (`SIM_HOUR_MS`), so `hour` cycles every 24 s of wall-clock time.
* Currency. PaySim amounts are in an unspecified currency, mostly in the tens of
  thousands; the simulator's payments are in the tens. `AMOUNT_SCALE` multiplies every
  money value so the two sit in the same range. It is a calibration guess, and the
  skew check is where it gets measured instead of assumed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .schemas import Transaction

SIM_HOUR_MS = 1000
AMOUNT_SCALE = 1500.0
RATIO_CAP = 100.0
DRAIN_TOLERANCE = 0.01

NAN = float("nan")


@dataclass(slots=True)
class History:
    """The per-account state a payment's history features are made from, as it stood before the payment."""

    out_count: float = 0.0  # payments the sender made earlier
    dst_count: float = 0.0  # payments the recipient received earlier
    dst_total: float = 0.0  # their total, in model units
    src_count: float = 0.0  # payments the sender itself received earlier
    src_last_step: float | None = None  # hour of the last one, None if there was none
    src_last_amount: float = 0.0


@dataclass(slots=True)
class _Inbound:
    count: int = 0
    total: float = 0.0  # scaled money received so far
    last_step: int = 0
    last_amount: float = 0.0


class PaymentFeatureStore:
    def __init__(
        self,
        feature_names: Sequence[str],
        type_categories: Sequence[str],
        amount_scale: float = AMOUNT_SCALE,
        sim_hour_ms: int = SIM_HOUR_MS,
    ) -> None:
        unknown = set(feature_names) - set(self._columns())
        if unknown:
            raise ValueError(f"model expects features this store cannot build: {sorted(unknown)}")
        self.feature_names = tuple(feature_names)
        self.type_categories = tuple(type_categories)
        self._type_code = {name: float(i) for i, name in enumerate(type_categories)}
        self.amount_scale = amount_scale
        self.sim_hour_ms = sim_hour_ms
        self._out_count: dict[str, int] = {}
        self._inbound: dict[str, _Inbound] = {}

    @staticmethod
    def _columns() -> tuple[str, ...]:
        return (
            "type", "amount_log", "amount_to_oldbal", "drains_account", "orig_old_zero",
            "dest_old_zero", "dest_is_merchant", "hour", "orig_prior_out", "dest_prior_in",
            "dest_prior_in_sum_log", "recv_count", "steps_since_recv", "amount_to_last_recv",
        )  # fmt: skip

    def _step(self, tx: Transaction) -> int:
        return tx.step if tx.step is not None else tx.ts // self.sim_hour_ms

    def _history(self, tx: Transaction) -> History:
        """What was known about the two accounts before `tx`. A subclass can keep this elsewhere."""
        sent_to = self._inbound.get(tx.dst)
        received = self._inbound.get(tx.src)
        return History(
            out_count=float(self._out_count.get(tx.src, 0)),
            dst_count=float(sent_to.count) if sent_to else 0.0,
            dst_total=sent_to.total if sent_to else 0.0,
            src_count=float(received.count) if received else 0.0,
            src_last_step=float(received.last_step) if received else None,
            src_last_amount=received.last_amount if received else 0.0,
        )

    def columns(self, tx: Transaction) -> dict[str, float]:
        return self.assemble(tx, self._history(tx))

    def assemble(self, tx: Transaction, h: History) -> dict[str, float]:
        scale = self.amount_scale
        amount = tx.amount * scale
        step = self._step(tx)

        if tx.src_balance is None:
            to_oldbal = drains = orig_zero = NAN
        else:
            old_orig = tx.src_balance * scale
            to_oldbal = min(max(amount / (old_orig + 1.0), 0.0), RATIO_CAP)
            drains = float(old_orig > 0 and amount >= old_orig - DRAIN_TOLERANCE)
            orig_zero = float(old_orig == 0)
        dest_zero = NAN if tx.dst_balance is None else float(tx.dst_balance == 0)

        if h.src_last_step is None:
            since, to_last = NAN, NAN
        else:
            since = float(step - h.src_last_step)
            to_last = RATIO_CAP if h.src_last_amount <= 0 else min(amount / h.src_last_amount, RATIO_CAP)

        return {
            "type": self._type_code.get(tx.kind or "", NAN),
            "amount_log": math.log1p(amount),
            "amount_to_oldbal": to_oldbal,
            "drains_account": drains,
            "orig_old_zero": orig_zero,
            "dest_old_zero": dest_zero,
            "dest_is_merchant": float(tx.dst_merchant),
            "hour": float(step % 24),
            "orig_prior_out": h.out_count,
            "dest_prior_in": h.dst_count,
            "dest_prior_in_sum_log": math.log1p(h.dst_total),
            "recv_count": h.src_count,
            "steps_since_recv": since,
            "amount_to_last_recv": to_last,
        }

    def check_model(self, scorer) -> None:
        """Raise ValueError if `scorer` was trained on other columns than this store builds."""
        if tuple(scorer.feature_names) != self.feature_names:
            raise ValueError("the new model uses different features than the running feature store builds")
        if tuple(scorer.meta["type_categories"]) != self.type_categories:
            raise ValueError("the new model codes the payment type differently")

    def features(self, tx: Transaction) -> np.ndarray:
        cols = self.columns(tx)
        return np.array([[cols[name] for name in self.feature_names]], dtype=np.float64)

    def update(self, tx: Transaction) -> None:
        amount = tx.amount * self.amount_scale
        self._out_count[tx.src] = self._out_count.get(tx.src, 0) + 1
        inbound = self._inbound.get(tx.dst)
        if inbound is None:
            inbound = self._inbound[tx.dst] = _Inbound()
        inbound.count += 1
        inbound.total += amount
        inbound.last_step = self._step(tx)
        inbound.last_amount = amount

    def flag(self, account: str, ts: int) -> None:
        """Blocking an account does not change this model's inputs."""
