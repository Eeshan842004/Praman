"""LightGBM attribution, calibrated.

GATE 1 is the point of this module, and it is a decision, not a formality. The
taxonomy heuristic already reaches an ICR around 0.94 against a Bayes ceiling of
~1.0, so there is roughly a tenth of a bit on the table. A model that does not
take it is not free: it adds a training step, a serialised artifact, a version
to audit, and a second thing that can silently go wrong. If held-out macro AUC
comes in under GATE1_MIN_AUC we ship the heuristic and say so on camera.

CALIBRATION IS FUNCTIONAL HERE, NOT PRESENTATIONAL. The Rego kernel reads
`max_posterior` against a confidence floor, so an overconfident model does not
merely look wrong -- it changes which actions are LEGAL. A raw LightGBM softmax
is systematically overconfident on nine-way problems, and shipping it would
quietly widen the set of payments the policy is willing to act on.

Temperature scaling is used rather than isotonic or Platt calibration, for a
property that matters more than a marginal fit improvement: dividing the logits
by a single positive scalar CANNOT change the argmax. So calibration moves
confidence without moving the predicted cause, AUC is identical before and
after, and the gate and the calibration cannot be traded off against each other.
One parameter, fitted on data the model never trained on.

Splitting is by CUSTOMER, not by row. Payments repeat per customer, so a row
split puts the same person on both sides and the held-out score measures
memorisation.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from praman.attribution.featurize import FEATURE_SPEC, align_categories, to_frame
from praman.sim.generator import DeclineBatch
from praman.taxonomy import CAUSES

# The blueprint's gate. Below this, heuristic attribution ships.
GATE1_MIN_AUC = 0.70

# Fraction of CUSTOMERS held out for scoring, and of the remainder used to fit
# the temperature. The calibration split must be disjoint from training or the
# temperature is fitted on the model's own memorised confidence.
HOLDOUT_FRACTION = 0.25
CALIBRATION_FRACTION = 0.20

_EPS = 1e-12


@dataclass(slots=True)
class ModelMetrics:
    macro_auc: float
    ece: float
    brier: float
    temperature: float
    n_train: int
    n_holdout: int
    # Filled by the ablation/tests: the comparison this phase exists to make.
    model_icr: float | None = None
    heuristic_icr: float | None = None

    def record_comparison(self, model_icr: float, heuristic_icr: float) -> None:
        self.model_icr = float(model_icr)
        self.heuristic_icr = float(heuristic_icr)

    @property
    def passes_gate1(self) -> bool:
        return self.macro_auc >= GATE1_MIN_AUC

    def render(self) -> str:
        lines = [
            f"  held-out macro AUC ....  {self.macro_auc:.4f}   "
            f"(gate {GATE1_MIN_AUC}: {'PASS' if self.passes_gate1 else 'FAIL'})",
            f"  expected calib. error .  {self.ece:.4f}",
            f"  multiclass Brier ......  {self.brier:.4f}",
            f"  temperature ...........  {self.temperature:.3f}"
            "   (>1 means the raw model was overconfident)",
            f"  train / holdout rows ..  {self.n_train:,} / {self.n_holdout:,}",
        ]
        if self.model_icr is not None and self.heuristic_icr is not None:
            delta = self.model_icr - self.heuristic_icr
            lines += [
                f"  ICR model .............  {self.model_icr:.4f}",
                f"  ICR heuristic .........  {self.heuristic_icr:.4f}",
                f"  ICR delta .............  {delta:+.4f}",
            ]
        return "\n".join(lines)


def expected_calibration_error(probs: np.ndarray, truth: np.ndarray, bins: int = 15) -> float:
    """Gap between confidence and accuracy, averaged over confidence bins.

    Measured on max(posterior) specifically, because that is the number the Rego
    confidence floor actually tests. Calibrating some other summary would be
    calibrating something the kernel never reads.
    """
    confidence = probs.max(axis=1)
    correct = (probs.argmax(axis=1) == truth).astype(float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    for lo, hi in itertools.pairwise(edges):
        mask = (confidence > lo) & (confidence <= hi)
        if not mask.any():
            continue
        total += mask.mean() * abs(correct[mask].mean() - confidence[mask].mean())
    return float(total)


def multiclass_brier(probs: np.ndarray, truth: np.ndarray) -> float:
    onehot = np.zeros_like(probs)
    onehot[np.arange(truth.size), truth] = 1.0
    return float(((probs - onehot) ** 2).sum(axis=1).mean())


def _fit_temperature(probs: np.ndarray, truth: np.ndarray) -> float:
    """One scalar, fitted by minimising held-out negative log-likelihood.

    Bounded search rather than gradient descent: the objective is smooth and
    one-dimensional, so a scan is exact enough and cannot fail to converge
    during a build.
    """
    from scipy.optimize import minimize_scalar

    logits = np.log(np.clip(probs, _EPS, None))
    rows = np.arange(truth.size)

    def nll(log_t: float) -> float:
        scaled = logits / np.exp(log_t)
        scaled -= scaled.max(axis=1, keepdims=True)
        p = np.exp(scaled)
        p /= p.sum(axis=1, keepdims=True)
        return float(-np.log(np.clip(p[rows, truth], _EPS, None)).mean())

    result = minimize_scalar(nll, bounds=(np.log(0.25), np.log(8.0)), method="bounded")
    return float(np.exp(result.x))


def _apply_temperature(probs: np.ndarray, temperature: float) -> np.ndarray:
    logits = np.log(np.clip(probs, _EPS, None)) / temperature
    logits -= logits.max(axis=1, keepdims=True)
    out = np.exp(logits)
    return out / out.sum(axis=1, keepdims=True)


@dataclass(slots=True)
class AttributionModel:
    booster: Any
    reference: pd.DataFrame
    temperature: float
    metrics: ModelMetrics
    importance: list[tuple[str, float]] = field(default_factory=list)
    holdout_batch: DeclineBatch | None = None

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        """Calibrated P(cause | features), rows summing to 1."""
        aligned = align_categories(frame, self.reference)
        raw = np.asarray(self.booster.predict_proba(aligned), dtype=float)
        return _apply_temperature(raw, self.temperature)

    def explain(self, frame: pd.DataFrame, top_k: int = 3) -> list[list[tuple[str, Any]]]:
        """The features this model relies on most, with this row's values.

        NOT a per-prediction Shapley attribution -- Shapley is cut (see
        BACKLOG.md). This is the model's global gain ranking paired with the
        actual values for each row, which answers "what does this model look at,
        and what did it see here?" without a second explainability stack.

        The binding explanation of any action is the policy trace anyway:
        `tier_evaluations` records every deny reason for every tier, and that is
        the part that actually decided.
        """
        names = [name for name, _ in self.importance[:top_k]]
        return [[(name, row[name]) for name in names] for _, row in frame.iterrows()]

    def save(self, path: str | Path) -> None:
        import joblib

        joblib.dump(
            {
                "booster": self.booster,
                "reference": self.reference,
                "temperature": self.temperature,
                "metrics": self.metrics,
                "importance": self.importance,
            },
            Path(path),
        )

    @classmethod
    def load(cls, path: str | Path) -> AttributionModel:
        import joblib

        blob = joblib.load(Path(path))
        return cls(
            booster=blob["booster"],
            reference=blob["reference"],
            temperature=blob["temperature"],
            metrics=blob["metrics"],
            importance=blob["importance"],
        )


def _split_by_customer(batch: DeclineBatch, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Train / calibration / holdout indices, grouped by customer.

    Splitting rows would put the same customer on both sides. Payments repeat
    per customer and share a latent reliability, so the held-out score would be
    measuring memorisation rather than generalisation.
    """
    rng = np.random.default_rng(seed)
    customers = np.array([d.customer_id for d in batch.declines])
    unique = np.unique(customers)
    rng.shuffle(unique)

    n_hold = max(1, int(len(unique) * HOLDOUT_FRACTION))
    n_calib = max(1, int(len(unique) * CALIBRATION_FRACTION))
    hold_ids = set(unique[:n_hold])
    calib_ids = set(unique[n_hold : n_hold + n_calib])

    holdout = np.array([c in hold_ids for c in customers])
    calibration = np.array([c in calib_ids for c in customers])
    train = ~(holdout | calibration)
    return train, calibration, holdout


def _subset(batch: DeclineBatch, mask: np.ndarray) -> DeclineBatch:
    """A DeclineBatch over a subset, carrying its own sealed cause_probs rows.

    Keeping `cause_probs` aligned is what lets the Bayes ceiling stay EXACT on a
    subset. Recomputing it would turn an exact ratio into an estimate and the
    ICR would stop meaning what it says.
    """
    idx = np.flatnonzero(mask)
    return DeclineBatch(
        declines=[batch.declines[i] for i in idx],
        seed=batch.seed,
        cause_probs=batch.cause_probs[idx],
    )


def train_attribution_model(
    batch: DeclineBatch,
    seed: int = 0,
    n_estimators: int = 400,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
) -> AttributionModel:
    """Train, calibrate, and score on customers the model has never seen."""
    from lightgbm import LGBMClassifier

    train_mask, calib_mask, hold_mask = _split_by_customer(batch, seed)

    frame = to_frame(batch.declines)
    truth = np.array([CAUSES.index(d.latent_cause) for d in batch.declines])

    booster = LGBMClassifier(
        objective="multiclass",
        num_class=len(CAUSES),
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        num_leaves=num_leaves,
        min_child_samples=30,
        subsample=0.9,
        subsample_freq=1,
        colsample_bytree=0.9,
        random_state=seed,
        n_jobs=-1,
        verbose=-1,
    )
    booster.fit(frame[train_mask], truth[train_mask])

    # Temperature on the CALIBRATION split -- disjoint from training, so the
    # fit is not just re-learning the model's own memorised confidence.
    raw_calib = np.asarray(booster.predict_proba(frame[calib_mask]), dtype=float)
    temperature = _fit_temperature(raw_calib, truth[calib_mask])

    raw_hold = np.asarray(booster.predict_proba(frame[hold_mask]), dtype=float)
    calibrated = _apply_temperature(raw_hold, temperature)
    y_hold = truth[hold_mask]

    from sklearn.metrics import roc_auc_score

    present = np.unique(y_hold)
    macro_auc = float(
        roc_auc_score(
            y_hold,
            calibrated[:, present] / calibrated[:, present].sum(axis=1, keepdims=True),
            multi_class="ovr",
            average="macro",
            labels=present,
        )
    )

    importance = sorted(
        zip(FEATURE_SPEC, booster.feature_importances_, strict=True),
        key=lambda kv: kv[1],
        reverse=True,
    )

    return AttributionModel(
        booster=booster,
        reference=frame,
        temperature=temperature,
        importance=[(n, float(v)) for n, v in importance],
        holdout_batch=_subset(batch, hold_mask),
        metrics=ModelMetrics(
            macro_auc=macro_auc,
            ece=expected_calibration_error(calibrated, y_hold),
            brier=multiclass_brier(calibrated, y_hold),
            temperature=temperature,
            n_train=int(train_mask.sum()),
            n_holdout=int(hold_mask.sum()),
        ),
    )


__all__ = [
    "CALIBRATION_FRACTION",
    "GATE1_MIN_AUC",
    "HOLDOUT_FRACTION",
    "AttributionModel",
    "ModelMetrics",
    "expected_calibration_error",
    "multiclass_brier",
    "train_attribution_model",
]
