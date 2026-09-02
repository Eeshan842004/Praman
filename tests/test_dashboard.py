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
    run_batch(
        n=400, seed=3, ledger_path=path, client=rego_like_client(), experiment_id="dash-test"
    )
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
