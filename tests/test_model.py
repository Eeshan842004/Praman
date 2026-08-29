"""Phase 4 GATE 1: does a trained model earn its place?

The taxonomy heuristic already reaches ICR 0.944 against a Bayes ceiling of
~1.0. So the honest question is not "is the model good" but "is the model worth
shipping at all" -- there are only about 0.1 bits left on the table.

These tests answer it with numbers rather than assumption. If LightGBM cannot
beat the heuristic, that IS the Gate 1 result, we ship the heuristic, and we say
so on camera. A model that adds nothing is not a neutral cost: it adds a
training step, a serialised artifact, a version to audit, and a second thing
that can silently go wrong.

Calibration is a FUNCTIONAL requirement here, not presentation. The policy
kernel consumes `max_posterior` against a confidence floor, so a miscalibrated
model does not merely look wrong -- it silently changes which actions are legal.

Written before the implementation exists.
"""

from __future__ import annotations

import numpy as np
import pytest

from praman.attribution.bayes import heuristic_posterior, information_report
from praman.sim.generator import generate_batch
from praman.taxonomy import CAUSES

# These tests are a written SPEC for Phase 4 item 5, which is scheduled last.
# They skip until the modules exist rather than breaking collection, so the
# suite stays green and the spec stays visible.
pytest.importorskip("praman.attribution.featurize", reason="Phase 4 model not built yet")
pytest.importorskip("praman.attribution.model", reason="Phase 4 model not built yet")

from praman.attribution.featurize import FEATURE_SPEC, to_frame
from praman.attribution.model import (
    GATE1_MIN_AUC,
    AttributionModel,
    train_attribution_model,
)


@pytest.fixture(scope="module")
def data():
    return generate_batch(n=12000, seed=31)


@pytest.fixture(scope="module")
def trained(data):
    return train_attribution_model(data, seed=0)


# ─────────────────────────────────────────────────────────────────────────────
# Featurisation
# ─────────────────────────────────────────────────────────────────────────────
def test_frame_has_every_declared_feature(data):
    df = to_frame(data.declines)
    assert set(df.columns) == set(FEATURE_SPEC)
    assert len(df) == len(data.declines)


def test_frame_never_leaks_the_sealed_fields(data):
    """The model must not be able to see the answer, nor either potential
    outcome. This is the single most important property of the feature layer."""
    leaked = {"latent_cause", "y0_recovered", "y1_recovered", "cuped_covariate"}
    assert not (leaked & set(to_frame(data.declines).columns))


def test_symbol_is_categorical_not_ordinal(data):
    """Decline codes have no order. Encoding '05' < '51' would invent one."""
    assert str(to_frame(data.declines)["symbol"].dtype) == "category"


# ─────────────────────────────────────────────────────────────────────────────
# GATE 1
# ─────────────────────────────────────────────────────────────────────────────
def test_gate1_held_out_macro_auc(trained: AttributionModel):
    """The blueprint's gate. Below this we ship heuristic attribution and say so
    rather than pretending."""
    assert trained.metrics.macro_auc >= GATE1_MIN_AUC, (
        f"macro AUC {trained.metrics.macro_auc:.3f} < {GATE1_MIN_AUC}"
    )


def test_predictions_are_probability_distributions(trained, data):
    p = trained.predict_proba(to_frame(data.declines[:500]))
    assert p.shape == (500, len(CAUSES))
    assert np.allclose(p.sum(axis=1), 1.0)


def test_model_is_calibrated_enough_for_a_confidence_floor(trained):
    """OPA denies automated tiers below max_posterior 0.40. If the model is
    overconfident, actions become legal that should not be."""
    assert trained.metrics.ece < 0.10, f"ECE {trained.metrics.ece:.4f}"


def test_brier_score_is_reported(trained):
    assert 0.0 < trained.metrics.brier < 1.0


# ─────────────────────────────────────────────────────────────────────────────
# ★ Does it beat the heuristic it would replace?
# ─────────────────────────────────────────────────────────────────────────────
def test_model_icr_is_measured_against_the_heuristic(trained, data):
    """The decision this phase exists to make.

    Both are scored on the same ICR scale over the same held-out rows, so the
    comparison is like-for-like. Whatever the result, it gets reported.
    """
    held = trained.holdout_batch
    model_icr = information_report(held, trained.predict_proba(to_frame(held.declines))).icr
    heur_icr = information_report(held, heuristic_posterior(held)).icr

    assert 0.0 <= model_icr <= 1.05
    # Recorded for the writeup either way -- this is a measurement, not a hope.
    trained.metrics.record_comparison(model_icr, heur_icr)


def test_model_does_not_exceed_the_bayes_ceiling(trained, data):
    """A ratio above 1.0 would mean the model extracted information that does
    not exist -- i.e. a leak from the sealed fields into the features."""
    held = trained.holdout_batch
    icr = information_report(held, trained.predict_proba(to_frame(held.declines))).icr
    assert icr <= 1.05, f"ICR {icr:.3f} exceeds the ceiling -- check for label leakage"


# ─────────────────────────────────────────────────────────────────────────────
# Explainability
# ─────────────────────────────────────────────────────────────────────────────
def test_top_features_are_available_per_prediction(trained, data):
    top = trained.explain(to_frame(data.declines[:5]), top_k=3)
    assert len(top) == 5
    assert all(len(row) == 3 for row in top)
    assert all(name in FEATURE_SPEC for row in top for name, _ in row)


def test_model_round_trips_through_disk(trained, tmp_path, data):
    path = tmp_path / "model.joblib"
    trained.save(path)
    reloaded = AttributionModel.load(path)
    df = to_frame(data.declines[:200])
    assert np.allclose(trained.predict_proba(df), reloaded.predict_proba(df))
