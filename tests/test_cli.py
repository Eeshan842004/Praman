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
from praman.ledger.canonical import prob_str
from praman.ledger.chain import append, connect


def _seed(path: Path, n: int = 25) -> None:
    conn = connect(path)
    try:
        for i in range(1, n + 1):
            append(
                conn,
                {
                    "ts_ms": 1787000000000 + i,
                    "payment_id": f"pay_TEST{i:06d}",
                    "customer_id": f"cust_{i % 7:04d}",
                    "arm": "holdout" if i % 10 == 0 else "treatment",
                    "cause": "INSUFFICIENT_FUNDS",
                    "posterior": prob_str(0.56),
                    "tier": "T1",
                    "opa_allow": 1,
                    "deny_reasons": "[]",
                    "bundle_revision": "4ca4787c0a1eea75",
                    "decision_id": f"dec_{i:06d}",
                    "amount_paise": 100000 + i,
                    "payload_json": '{"redacted":true}',
                },
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
