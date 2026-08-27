"""`praman verify` and `praman tamper`.

This is the attestation demo, so it has to behave correctly under a hostile
reading: exit codes must be honest, the broken entry must be named exactly, and
tampering must require visibly defeating the append-only trigger first.

Written before the implementation exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from praman.cli import main
from praman.ledger.chain import append, connect
from praman.ledger.records import DecisionRecord
from praman.taxonomy import CAUSES


def _seed(path: Path, n: int = 25) -> None:
    conn = connect(path)
    try:
        for i in range(1, n + 1):
            append(
                conn,
                DecisionRecord(
                    ts_ms=1787000000000 + i,
                    experiment_id="praman-v1",
                    holdout_pct=10,
                    payment_id=f"pay_TEST{i:06d}",
                    customer_id=f"cust_{i % 7:04d}",
                    arm="holdout" if i % 10 == 0 else "treatment",
                    attempt_no=1,
                    rail="card",
                    symbol="05",
                    region="IN",
                    cause="INSUFFICIENT_FUNDS",
                    posterior=dict.fromkeys(CAUSES, 1 / 9),
                    attribution_source="heuristic",
                    attribution_version="taxonomy-v1",
                    tier="T1",
                    tier_evaluations={"T1": []},
                    opa_allow=True,
                    deny_reasons=[],
                    policy_input={},
                    bundle_revision="4ca4787c0a1eea75",
                    decision_id=f"dec_{i:06d}",
                    amount_paise=100000 + i,
                    cuped_covariate=0.5,
                    covariate_asof_ms=1786900000000,
                    payload={"redacted": True},
                ).to_row(),
            )
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# verify
# ─────────────────────────────────────────────────────────────────────────────
def test_verify_passes_on_clean_ledger(tmp_path, capsys):
    p = tmp_path / "ledger.db"
    _seed(p)
    rc = main(["verify", "--ledger", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ATTESTATION PASS" in out
    assert "25" in out


def test_verify_reports_the_bundle_revisions_it_spans(tmp_path, capsys):
    """Grouping by revision is what proves we can audit a policy CHANGE, not
    just a policy."""
    p = tmp_path / "ledger.db"
    _seed(p)
    main(["verify", "--ledger", str(p)])
    assert "4ca4787c0a1eea75" in capsys.readouterr().out


def test_verify_on_missing_ledger_fails_cleanly(tmp_path, capsys):
    rc = main(["verify", "--ledger", str(tmp_path / "nope.db")])
    assert rc != 0
    assert "ATTESTATION PASS" not in capsys.readouterr().out


def test_verify_on_empty_ledger_passes(tmp_path, capsys):
    p = tmp_path / "empty.db"
    connect(p).close()
    assert main(["verify", "--ledger", str(p)]) == 0
    assert "ATTESTATION PASS" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────────────
# tamper -> verify  (the 15 seconds that carry the whole audit claim)
# ─────────────────────────────────────────────────────────────────────────────
def test_tamper_then_verify_fails_at_the_exact_entry(tmp_path, capsys):
    p = tmp_path / "ledger.db"
    _seed(p, n=25)

    assert (
        main(["tamper", "--ledger", str(p), "--entry", "13", "--set", "amount_paise=99999900"]) == 0
    )
    tamper_out = capsys.readouterr().out
    # Defeating immutability must be loud, not quiet.
    assert "trigger" in tamper_out.lower()

    rc = main(["verify", "--ledger", str(p)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "ATTESTATION FAIL" in out
    assert "CHAIN BROKEN at entry 13" in out
    assert "12" in out  # 25 - 13 = 12 subsequent entries invalidated


def test_tamper_rejects_a_field_outside_the_hashed_set(tmp_path, capsys):
    p = tmp_path / "ledger.db"
    _seed(p)
    rc = main(["tamper", "--ledger", str(p), "--entry", "5", "--set", "seq=999"])
    assert rc != 0


def test_tamper_rejects_a_nonexistent_entry(tmp_path):
    p = tmp_path / "ledger.db"
    _seed(p, n=5)
    assert main(["tamper", "--ledger", str(p), "--entry", "999", "--set", "cause=X"]) != 0


@pytest.mark.parametrize("field", ["cause", "tier", "bundle_revision", "posterior"])
def test_tamper_detects_every_hashed_field(tmp_path, capsys, field):
    p = tmp_path / "ledger.db"
    _seed(p, n=10)
    main(["tamper", "--ledger", str(p), "--entry", "4", "--set", f"{field}=TAMPERED"])
    capsys.readouterr()
    assert main(["verify", "--ledger", str(p)]) != 0
    assert "CHAIN BROKEN at entry 4" in capsys.readouterr().out


def test_verify_is_idempotent_and_read_only(tmp_path, capsys):
    """Running verify must never alter the evidence it is verifying."""
    p = tmp_path / "ledger.db"
    _seed(p, n=12)
    before = p.read_bytes()
    main(["verify", "--ledger", str(p)])
    main(["verify", "--ledger", str(p)])
    capsys.readouterr()
    assert main(["verify", "--ledger", str(p)]) == 0
    assert p.read_bytes() == before


def test_unknown_command_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["definitely-not-a-command"])
    assert exc.value.code != 0


# ─────────────────────────────────────────────────────────────────────────────
# validate-estimator
# ─────────────────────────────────────────────────────────────────────────────
def test_validate_estimator_reports_coverage_and_exits_zero(capsys):
    """200 worlds, not fewer.

    The [0.92, 0.97] band is roughly +/-2 Monte Carlo standard errors at 200
    worlds. At 40 worlds SE is ~3.4%, so the band would reject a *perfect*
    estimator about a third of the time -- the gate would be measuring its own
    noise. Anything cheaper than 200 is not a test, it is a coin flip.
    """
    rc = main(["validate-estimator", "--worlds", "200", "--boot", "800"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "coverage" in out.lower()
    assert "ESTIMATOR VALIDATION" in out


def test_validate_estimator_warns_when_too_few_worlds_to_judge(capsys):
    """Below ~100 worlds the gate cannot distinguish a bad estimator from noise,
    and it must say so rather than return a confident verdict."""
    main(["validate-estimator", "--worlds", "30", "--boot", "200"])
    assert "monte carlo" in capsys.readouterr().out.lower()


def test_validate_estimator_fails_when_coverage_is_off(capsys, monkeypatch):
    """The command is a GATE, not a report. If the interval stops covering, the
    exit code has to say so -- otherwise CI would happily ship broken statistics."""
    import praman.cli as cli
    from praman.measure.harness import ValidationReport

    broken = ValidationReport(
        n_worlds=200,
        holdout_pct=10,
        coverage=0.55,
        mean_bias_pct=0.2,
        rmse=1.0,
        mean_ci_width=2.0,
        mean_variance_reduction=0.4,
        naive_mean_bias_pct=90.0,
        naive_coverage=0.0,
        mean_true_ate=8.0,
    )
    monkeypatch.setattr(cli, "validate_estimator", lambda **_: broken)
    assert main(["validate-estimator", "--worlds", "200"]) != 0
    assert "coverage" in capsys.readouterr().out.lower()
