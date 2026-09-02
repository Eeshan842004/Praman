"""The dashboard.

Three views, and each one exists to show something a terminal cannot:

    /              the batch at a glance -- tier mix, the three-tier result,
                   and the violations counter that has to read zero
    /decision/{n}  ONE decision in full: the posterior over all nine causes,
                   every deny reason across every tier, the bundle revision,
                   and the explanation
    /attestation   chain status and the bundle spans

The decision view is the one that earns its place. `tier_evaluations` records
what every tier was told, not just the tier we took, and a terminal dump of that
JSON is unreadable. Rendering the full conflict matrix is the only way the
regulatory-deadlock case is legible to anyone who is not already reading the
ledger schema.

It reads the SAME committed ledger `praman verify` attests. No separate
analytics store, so the dashboard cannot drift from the evidence.

Written before the implementation exists.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import rego_like_client

from praman.api.dashboard import build_dashboard_router
from praman.ledger.chain import connect
from praman.slice_runner import run_batch


@pytest.fixture(scope="module")
def ledger(tmp_path_factory):
    path = tmp_path_factory.mktemp("dash") / "ledger.db"
    run_batch(n=400, seed=3, ledger_path=path, client=rego_like_client(), experiment_id="dash-test")
    return path


@pytest.fixture(scope="module")
def client(ledger):
    app = FastAPI()
    app.include_router(build_dashboard_router(ledger_path=ledger))
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def any_decision_seq(ledger):
    conn = connect(ledger)
    try:
        return int(
            conn.execute(
                "SELECT seq FROM ledger WHERE entry_type = 'DECISION' ORDER BY seq LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) Batch overview
# ─────────────────────────────────────────────────────────────────────────────
def test_the_overview_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_the_overview_shows_the_tier_distribution(client):
    body = client.get("/").text
    for tier in ("T0", "T1", "T2", "T3", "T4"):
        assert tier in body


def test_the_overview_shows_the_violations_counter(client):
    """The single gauge the compliance story rests on. It must be visible and
    it must read zero."""
    body = client.get("/").text
    assert "policy violations" in body.lower()
    assert "0" in body


def test_the_overview_links_to_a_decision(client):
    assert "/decision/" in client.get("/").text


# ─────────────────────────────────────────────────────────────────────────────
# (b) Decision detail
# ─────────────────────────────────────────────────────────────────────────────
def test_a_decision_page_renders(client, any_decision_seq):
    assert client.get(f"/decision/{any_decision_seq}").status_code == 200


def test_the_decision_page_shows_all_nine_causes(client, any_decision_seq):
    """The posterior IS the product. Showing only the argmax would throw away
    the ambiguity the whole system exists to represent."""
    from praman.taxonomy import CAUSES

    body = client.get(f"/decision/{any_decision_seq}").text
    for cause in CAUSES:
        assert cause in body


def test_the_decision_page_shows_every_tier_not_just_the_chosen_one(client, any_decision_seq):
    """The deadlock case is only legible if the full conflict matrix survives.
    A page showing just the selected tier would hide the thing no incumbent
    shows a merchant: why they cannot act."""
    body = client.get(f"/decision/{any_decision_seq}").text
    for tier in ("T1", "T2", "T3", "T4"):
        assert tier in body


def test_the_decision_page_shows_the_bundle_revision(client, any_decision_seq):
    body = client.get(f"/decision/{any_decision_seq}").text
    assert "bundle" in body.lower()


def test_the_decision_page_carries_an_explanation(client, any_decision_seq):
    body = client.get(f"/decision/{any_decision_seq}").text
    assert "explanation" in body.lower()


def test_the_decision_page_names_its_ledger_entry(client, any_decision_seq):
    """The link back to the evidence. Without it the page is a claim rather
    than a view over an attested record."""
    assert str(any_decision_seq) in client.get(f"/decision/{any_decision_seq}").text


def test_a_missing_decision_is_a_404_not_a_500(client):
    assert client.get("/decision/99999999").status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# (c) Attestation
# ─────────────────────────────────────────────────────────────────────────────
def test_the_attestation_page_renders(client):
    assert client.get("/attestation").status_code == 200


def test_the_attestation_page_reports_chain_status(client):
    body = client.get("/attestation").text.lower()
    assert "chain" in body
    assert "intact" in body or "broken" in body


def test_the_attestation_page_lists_the_bundle_spans(client):
    """Two spans is what proves we audit a policy CHANGE, not just a policy."""
    assert "bundle" in client.get("/attestation").text.lower()


# ─────────────────────────────────────────────────────────────────────────────
# It never invents data
# ─────────────────────────────────────────────────────────────────────────────
def test_nothing_thinks_on_a_dashboard_request(client, any_decision_seq, monkeypatch):
    """No policy evaluation on a page load. The dashboard is a view over a
    decision that was already made and recorded, not a place one gets made."""

    def explode(*_a, **_k):
        raise AssertionError("the dashboard must not evaluate policy")

    monkeypatch.setattr("praman.kernel.opa_client.PolicyClient.evaluate", explode)
    assert client.get(f"/decision/{any_decision_seq}").status_code == 200
    assert client.get("/").status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# A reader must not have to reconcile surfaces in their head
# ─────────────────────────────────────────────────────────────────────────────
def test_the_overview_says_which_ledger_it_is_reading(client):
    """The dashboard reads the trimmed demo ledger; the writeup headlines the
    powered n=5,000 run. Both are valid and they differ, so the page has to say
    which one it is showing or the two look like a contradiction."""
    body = client.get("/").text.lower()
    assert "demo ledger" in body or "trimmed" in body
    assert "reproduce.md" in body


def test_the_bundle_span_column_says_it_counts_decision_entries(client):
    """Spans are MIN/MAX over DECISION rows, so the numbers skip the trailing
    actuation and outcome rows of the last decision in a span. Reading
    "entries 1-988" then "991-..." invites a question about 989 and 990 that
    the screen cannot answer."""
    body = client.get("/attestation").text.lower()
    assert "decision entries" in body
    assert "actuation" in body and "outcome" in body


# ─────────────────────────────────────────────────────────────────────────────
# The page must not contradict its own data
# ─────────────────────────────────────────────────────────────────────────────
def _seq_where(ledger, predicate_sql):
    conn = connect(ledger)
    try:
        row = conn.execute(
            f"SELECT seq FROM ledger WHERE entry_type = 'DECISION' AND {predicate_sql} LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else None


def test_a_sharp_posterior_is_not_described_as_ambiguous(client, ledger):
    """The copy said "the normaliser does not resolve it to one cause -- the
    ambiguity is the product" directly above a posterior reading 1.000.

    Sharpness where the code is informative is ALSO the thesis: the taxonomy is
    meant to be sharp when the rail says something definite and wide when it
    says nothing. Printing the ambiguity line over a degenerate posterior argues
    against the page's own table.
    """
    seq = _seq_where(ledger, "CAST(posterior AS REAL) >= 0.95")
    if seq is None:
        pytest.skip("no sharp posterior in this batch")
    body = client.get(f"/decision/{seq}").text.lower()
    assert "ambiguity is the product" not in body
    assert "unambiguous" in body or "correctly sharp" in body


def test_an_ambiguous_posterior_keeps_the_ambiguity_copy(client, ledger):
    """Code 05 is the case the whole product exists for, and the page has to
    say so when it is looking at one."""
    seq = _seq_where(ledger, "CAST(posterior AS REAL) < 0.60")
    if seq is None:
        pytest.skip("no ambiguous posterior in this batch")
    body = client.get(f"/decision/{seq}").text.lower()
    assert "ambiguity" in body


def test_a_tier_the_ladder_never_proposed_is_not_shown_as_a_missed_action(client, ledger):
    """Ladder eligibility and policy permission are different things.

    A non-retryable cause never proposes T1/T2, so policy may well allow them
    while the ladder never asked. Rendering that as a plain ALLOW next to a
    different outcome reads as ignoring a permitted action.
    """
    seq = _seq_where(ledger, "cause = 'EXPIRED_OR_INVALID_CREDENTIAL'")
    if seq is None:
        pytest.skip("no non-retryable cause in this batch")
    body = client.get(f"/decision/{seq}").text.lower()
    assert "not proposed" in body


def test_a_permitted_but_lower_ranked_tier_says_so(client, ledger):
    """The actual reason T1 showed ALLOW beside a T3 outcome: T3 is the cause's
    default tier and is tried first. Both were legal; preference decided."""
    seq = _seq_where(ledger, "cause = 'AUTH_FAILURE' AND tier = 'T3'")
    if seq is None:
        pytest.skip("no AUTH_FAILURE T3 decision in this batch")
    body = client.get(f"/decision/{seq}").text.lower()
    assert "not preferred" in body or "preference" in body
    assert "taken" in body


def test_the_taken_tier_is_marked_as_taken(client, any_decision_seq):
    assert "taken" in client.get(f"/decision/{any_decision_seq}").text.lower()


def test_the_per_batch_copy_does_not_argue_with_its_own_table(client):
    """It said the batches are "deliberately too small to power" while both rows
    showed EXCL. ZERO = yes. Underpowered means you may miss, not that you must."""
    body = client.get("/").text.lower()
    assert "too small to power" not in body
    assert "below the mde" in body or "arithmetic rather than evidence" in body
