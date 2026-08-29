"""Gate 1: the ablation that decides whether the model ships.

The verdict logic is what gets tested hardest here, because the verdict is the
deliverable. A model is justified by moving declines from terminate into a legal
action worth measurable money -- not by a ranking statistic -- so the rule has to
refuse a model that ranks well and changes nothing, and it has to refuse a model
whose recovery difference is a point estimate with no interval.

The failure mode with its own test is the interesting one: a model can act MORE
often while knowing LESS, because sharper posteriors clear the Rego confidence
floor more easily. That is S5 arriving through the exact number the kernel
reads, and the ablation has to be able to name it.
"""

from __future__ import annotations

from pathlib import Path

from praman.attribution.ablation import Ablation, ArmSummary
from praman.measure.harness import Estimate
from praman.slice_runner import RunResult


def _arm(label: str, *, t0: int, low_conf: int, actuated: int, tau: float, icr: float, n=1000):
    return ArmSummary(
        label=label,
        result=RunResult(
            experiment_id="t",
            ledger_path=Path("x"),
            n_declines=n,
            n_actuated=actuated,
            tier_counts={"T0": t0},
            low_confidence_declines=low_conf,
            estimate=Estimate(
                tau_hat=tau,
                ci_lo=tau - 100,
                ci_hi=tau + 100,
                variance_reduction=0.3,
                n_treatment=800,
                n_holdout=200,
                n_clusters_treatment=400,
                n_clusters_holdout=100,
            ),
        ),
        icr=icr,
    )


def _delta(tau: float, lo: float, hi: float) -> Estimate:
    return Estimate(
        tau_hat=tau,
        ci_lo=lo,
        ci_hi=hi,
        variance_reduction=0.3,
        n_treatment=800,
        n_holdout=200,
        n_clusters_treatment=400,
        n_clusters_holdout=100,
    )


def _ablation(*, model_icr: float, delta: Estimate, model_low_conf: int = 48, auc=0.95):
    return Ablation(
        heuristic=_arm("heuristic", t0=90, low_conf=84, actuated=2442, tau=4493.0, icr=0.9608),
        model=_arm("ml", t0=50, low_conf=model_low_conf, actuated=2556, tau=4789.0, icr=model_icr),
        n=1000,
        gate_auc=auc,
        gate_min_auc=0.70,
        delta=delta,
    )


# ─────────────────────────────────────────────────────────────────────────────
# The verdict
# ─────────────────────────────────────────────────────────────────────────────
def test_a_model_that_captures_less_information_does_not_ship():
    """Even with a higher AUC and more actuations. The heuristic is the exact
    Bayes posterior given (symbol, side signals), so losing to it on information
    means the model is a worse estimator of the thing being estimated."""
    ab = _ablation(model_icr=0.9029, delta=_delta(296.0, 50.0, 500.0))
    assert not ab.model_earns_its_place
    assert "ship the HEURISTIC" in ab.render()


def test_a_recovery_delta_that_straddles_zero_does_not_ship():
    """ "Worth Rs Y" off a point estimate with no interval is the naive
    gross-recovery move this project exists to argue against."""
    ab = _ablation(model_icr=0.99, delta=_delta(296.0, -19.0, 656.0))
    assert not ab.delta_excludes_zero
    assert not ab.model_earns_its_place
    assert "does not separate from zero" in ab.render()


def test_a_model_that_wins_on_every_measured_axis_does_ship():
    ab = _ablation(model_icr=0.99, delta=_delta(296.0, 50.0, 500.0))
    assert ab.model_earns_its_place
    out = ab.render()
    assert "ship the model" in out
    assert "worth Rs" in out


def test_the_auc_gate_alone_is_never_enough():
    """A model can rank well and be worse where the kernel consumes it."""
    ab = _ablation(model_icr=0.90, delta=_delta(296.0, 50.0, 500.0), auc=0.99)
    assert ab.gate_auc >= ab.gate_min_auc
    assert not ab.model_earns_its_place


def test_failing_the_auc_gate_blocks_a_model_that_wins_elsewhere():
    ab = _ablation(model_icr=0.99, delta=_delta(296.0, 50.0, 500.0), auc=0.55)
    assert not ab.model_earns_its_place
    assert "FAIL" in ab.render()


# ─────────────────────────────────────────────────────────────────────────────
# Buying actions with confidence rather than accuracy
# ─────────────────────────────────────────────────────────────────────────────
def test_it_names_a_model_that_is_sharper_but_not_righter():
    """The S5 pattern: fewer confidence-floor blocks AND less information.
    More actions, resting on worse posteriors."""
    ab = _ablation(model_icr=0.9029, delta=_delta(296.0, -19.0, 656.0), model_low_conf=48)
    assert ab.buys_confidence_not_accuracy
    out = ab.render()
    assert "buying actions with confidence rather than with accuracy" in out
    assert "S5" in out


def test_a_model_that_is_both_sharper_and_righter_is_not_flagged():
    ab = _ablation(model_icr=0.99, delta=_delta(296.0, 50.0, 500.0), model_low_conf=48)
    assert not ab.buys_confidence_not_accuracy


def test_less_information_but_fewer_actions_is_not_the_same_failure():
    """The pattern needs BOTH halves. A model that is merely worse, without
    also being more confident, is a different problem."""
    ab = _ablation(model_icr=0.90, delta=_delta(296.0, 50.0, 500.0), model_low_conf=120)
    assert not ab.buys_confidence_not_accuracy


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────
def test_the_report_states_the_comparison_is_controlled():
    out = _ablation(model_icr=0.99, delta=_delta(296.0, 50.0, 500.0)).render()
    assert "identical batch" in out
    assert "only the" in out


def test_the_report_always_shows_the_interval_on_the_delta():
    """A rupee figure without an interval must not be printable from here."""
    out = _ablation(model_icr=0.99, delta=_delta(296.0, 50.0, 500.0)).render()
    assert "95% CI" in out
    assert "separates from zero" in out


def test_worth_paise_scales_with_the_batch():
    ab = _ablation(model_icr=0.99, delta=_delta(296.0, 50.0, 500.0))
    assert ab.worth_paise == ab.delta_tau * ab.n
