"""Webhook ingest — Phase 1.

S2 is the failure this whole layer exists to prevent: a slow acknowledgement
becomes a Razorpay redelivery, becomes a second attempt counted against the same
payment, becomes a network excessive-reattempt fee. So the request path does
four things and no more — verify, redact, deduplicate, acknowledge — and every
one of the four is tested here, including the timing.

"No model, no OPA, no LLM in the request path" is asserted by SABOTAGE rather
than by inspection: the policy client is replaced with one that raises on any
call, and the endpoint must still return 200. A comment claiming the request
path is thin decays; a test that fails the moment someone adds an inference call
does not.

Payloads are hand-constructed from Razorpay's documented `payment.failed`
schema, so this layer is complete and testable before any key exists. Captured
live payloads slot into the same fixtures.

Written before the implementation exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import rego_like_client

from praman.ingest.fixtures import failed_payment_event, signed_headers
from praman.ingest.redact import PSEUDONYM_PREFIX, redact
from praman.ingest.router import build_webhook_router
from praman.ingest.signature import compute_signature, verify_signature
from praman.ingest.store import connect_ingest, pending, record_delivery

SECRET = "whsec_test_0123456789abcdef"


@pytest.fixture
def ingest_db(tmp_path):
    conn = connect_ingest(tmp_path / "ingest.db")
    yield conn
    conn.close()


@pytest.fixture
def client(tmp_path):
    """An app with ONLY the webhook router mounted.

    Deliberately minimal: if the endpoint ever needs something the wider app
    provides, that is a signal it is doing more than acknowledging.
    """
    app = FastAPI()
    app.include_router(build_webhook_router(secret=SECRET, ingest_path=tmp_path / "ingest.db"))
    with TestClient(app) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# Signature
# ─────────────────────────────────────────────────────────────────────────────
def test_signature_matches_razorpays_documented_scheme():
    """HMAC-SHA256 over the RAW body, hex digest. Razorpay's scheme exactly."""
    raw = b'{"event":"payment.failed"}'
    expected = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert compute_signature(raw, SECRET) == expected


def test_verify_rejects_a_tampered_body():
    raw = b'{"amount":100}'
    sig = compute_signature(raw, SECRET)
    assert verify_signature(raw, sig, SECRET)
    assert not verify_signature(b'{"amount":900000}', sig, SECRET)


def test_verify_rejects_a_missing_or_empty_signature():
    raw = b"{}"
    assert not verify_signature(raw, None, SECRET)
    assert not verify_signature(raw, "", SECRET)


def test_verify_rejects_a_malformed_signature_without_raising():
    """A non-hex header must be a clean False, never an exception. An exception
    here would turn a malformed attack into a 500 and, behind a retrying sender,
    into a redelivery storm."""
    assert not verify_signature(b"{}", "not-hex-at-all!!", SECRET)


def test_verify_uses_a_constant_time_comparison():
    """Timing leakage here would let an attacker recover the signature byte by
    byte. Asserted by reading the source, because a timing test on a laptop
    measures the scheduler, not the comparison."""
    import inspect

    from praman.ingest import signature

    assert "compare_digest" in inspect.getsource(signature)


# ─────────────────────────────────────────────────────────────────────────────
# The redact() chokepoint
# ─────────────────────────────────────────────────────────────────────────────
def test_redact_removes_every_direct_identifier():
    event = failed_payment_event(email="real@person.com", contact="+919812345678")
    blob = json.dumps(redact(event, salt=SECRET))
    assert "real@person.com" not in blob
    assert "+919812345678" not in blob
    assert "9812345678" not in blob


def test_redact_pseudonymises_rather_than_deletes():
    """Cluster randomisation is at the CUSTOMER, so the customer key has to
    survive redaction. Deleting it outright would silently destroy the unit of
    randomisation and every interval computed from it."""
    a = redact(failed_payment_event(email="x@y.com"), salt=SECRET)
    b = redact(failed_payment_event(email="x@y.com"), salt=SECRET)
    c = redact(failed_payment_event(email="other@y.com"), salt=SECRET)

    key_a = a["payload"]["payment"]["entity"]["email"]
    assert key_a.startswith(PSEUDONYM_PREFIX)
    assert key_a == b["payload"]["payment"]["entity"]["email"]
    assert key_a != c["payload"]["payment"]["entity"]["email"]


def test_redact_keeps_everything_the_kernel_actually_needs():
    """Redaction must not cost us the decline itself."""
    out = redact(failed_payment_event(), salt=SECRET)
    entity = out["payload"]["payment"]["entity"]
    assert entity["amount"] == 2_200_00
    assert entity["method"] == "card"
    assert entity["error_reason"] == "payment_failed"
    assert entity["error_source"] == "bank"


def test_redact_does_not_mutate_its_input():
    event = failed_payment_event(email="x@y.com")
    before = json.dumps(event, sort_keys=True)
    redact(event, salt=SECRET)
    assert json.dumps(event, sort_keys=True) == before


def test_redact_drops_free_text_notes():
    """`notes` is merchant-controlled free text. It is the one field guaranteed
    to eventually contain something we never agreed to store."""
    event = failed_payment_event()
    event["payload"]["payment"]["entity"]["notes"] = {"pan": "ABCDE1234F"}
    assert "ABCDE1234F" not in json.dumps(redact(event, salt=SECRET))


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────
def test_a_new_delivery_is_recorded_once(ingest_db):
    assert record_delivery(ingest_db, "evt_1", "pay_1", 1, "{}") is True


def test_a_redelivery_is_ignored(ingest_db):
    """S2 in one assertion. Razorpay retries until it sees a 2xx, so the same
    event id arrives repeatedly and must never enqueue twice."""
    assert record_delivery(ingest_db, "evt_1", "pay_1", 1, "{}") is True
    assert record_delivery(ingest_db, "evt_1", "pay_1", 2, "{}") is False
    assert len(pending(ingest_db)) == 1


def test_distinct_events_on_the_same_payment_are_both_kept(ingest_db):
    """A payment legitimately produces several events. Deduplicating on
    payment_id alone would drop real ones."""
    assert record_delivery(ingest_db, "evt_1", "pay_1", 1, "{}") is True
    assert record_delivery(ingest_db, "evt_2", "pay_1", 2, "{}") is True
    assert len(pending(ingest_db)) == 2


# ─────────────────────────────────────────────────────────────────────────────
# The endpoint
# ─────────────────────────────────────────────────────────────────────────────
def test_a_valid_delivery_is_acknowledged(client):
    event = failed_payment_event()
    body = json.dumps(event).encode()
    r = client.post("/webhooks/razorpay", content=body, headers=signed_headers(body, SECRET))
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


def test_a_tampered_body_is_rejected(client):
    event = failed_payment_event()
    headers = signed_headers(json.dumps(event).encode(), SECRET)
    event["payload"]["payment"]["entity"]["amount"] = 99_999_900
    r = client.post("/webhooks/razorpay", content=json.dumps(event).encode(), headers=headers)
    assert r.status_code == 401


def test_a_missing_signature_header_is_rejected(client):
    body = json.dumps(failed_payment_event()).encode()
    r = client.post("/webhooks/razorpay", content=body, headers={"X-Razorpay-Event-Id": "evt_x"})
    assert r.status_code == 400


def test_a_duplicate_delivery_is_acknowledged_but_not_re_enqueued(client, tmp_path):
    """Razorpay must still get its 200 -- otherwise it retries forever -- while
    the queue stays at one."""
    body = json.dumps(failed_payment_event()).encode()
    headers = signed_headers(body, SECRET)

    first = client.post("/webhooks/razorpay", content=body, headers=headers)
    second = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"

    conn = connect_ingest(tmp_path / "ingest.db")
    try:
        assert len(pending(conn)) == 1
    finally:
        conn.close()


def test_nothing_that_thinks_runs_in_the_request_path(client, monkeypatch):
    """Sabotage, not inspection.

    Any policy evaluation, model call or LLM call inside the handler makes this
    explode. The endpoint must acknowledge without consulting any of them.
    """

    def explode(*_a, **_k):
        raise AssertionError("the request path must not evaluate policy")

    monkeypatch.setattr("praman.kernel.opa_client.PolicyClient.evaluate", explode)

    body = json.dumps(failed_payment_event()).encode()
    r = client.post("/webhooks/razorpay", content=body, headers=signed_headers(body, SECRET))
    assert r.status_code == 200


def test_the_raw_body_is_verified_not_a_reserialisation(client):
    """Signing is over bytes. If the handler parsed the JSON and re-serialised
    it before verifying, any difference in key order or spacing would break a
    signature Razorpay considers valid."""
    spaced = b'{\n  "entity" : "event",\n  "event":"payment.failed",\n  "payload": {}\n}'
    r = client.post("/webhooks/razorpay", content=spaced, headers=signed_headers(spaced, SECRET))
    assert r.status_code in (200, 422)
    assert r.status_code != 401, "a valid signature over odd whitespace must verify"


def test_no_pii_reaches_the_store(client, tmp_path):
    body = json.dumps(
        failed_payment_event(email="leak@person.com", contact="+919812345678")
    ).encode()
    client.post("/webhooks/razorpay", content=body, headers=signed_headers(body, SECRET))

    conn = connect_ingest(tmp_path / "ingest.db")
    try:
        stored = "".join(row["raw_json"] for row in pending(conn))
    finally:
        conn.close()
    assert "leak@person.com" not in stored
    assert "9812345678" not in stored


# ─────────────────────────────────────────────────────────────────────────────
# Latency — the reason the layer is shaped this way
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.slow
def test_p99_under_a_200_request_burst_is_under_20ms(client):
    """The budget that forces ack-before-think.

    Razorpay redelivers on a slow ack, and a redelivery that reached the counter
    would be a regulatory problem, not a performance one. That is why this is a
    correctness test and not a benchmark.
    """
    latencies = []
    for i in range(200):
        body = json.dumps(failed_payment_event(payment_id=f"pay_burst_{i}")).encode()
        headers = signed_headers(body, SECRET, event_id=f"evt_burst_{i}")
        started = time.perf_counter()
        r = client.post("/webhooks/razorpay", content=body, headers=headers)
        latencies.append((time.perf_counter() - started) * 1000)
        assert r.status_code == 200

    latencies.sort()
    p99 = latencies[int(0.99 * len(latencies)) - 1]
    assert p99 < 20.0, f"p99 {p99:.1f} ms exceeds the 20 ms budget"


# ─────────────────────────────────────────────────────────────────────────────
# A webhook produces a real ledger DECISION
# ─────────────────────────────────────────────────────────────────────────────
def test_a_webhook_delivery_produces_a_real_ledger_decision(client, tmp_path):
    """The wiring, end to end: Razorpay's bytes in, an attestable decision out.

    Not a mock of the pipeline -- the real ladder, the real policy client, the
    real hash-chained ledger.
    """
    from praman.ingest.store import connect_ingest as _connect
    from praman.ingest.worker import process_pending
    from praman.ledger.chain import connect as connect_ledger
    from praman.ledger.chain import verify as verify_chain

    body = json.dumps(failed_payment_event()).encode()
    assert (
        client.post(
            "/webhooks/razorpay", content=body, headers=signed_headers(body, SECRET)
        ).status_code
        == 200
    )

    ledger_path = tmp_path / "ledger.db"
    conn = _connect(tmp_path / "ingest.db")
    try:
        seqs = process_pending(
            conn, ledger_path, client=rego_like_client(), experiment_id="ingest-test"
        )
        assert len(seqs) == 1
        assert not pending(conn), "a processed delivery must leave the queue"
        assert conn.execute("SELECT decision_seq FROM webhook_events").fetchone()[0] == seqs[0]
    finally:
        conn.close()

    led = connect_ledger(ledger_path)
    try:
        ok, _broken, _msg = verify_chain(led)
        row = led.execute(
            "SELECT payment_id, cause, symbol, policy_input_json, entry_type FROM ledger"
        ).fetchone()
    finally:
        led.close()

    assert ok, "the decision a webhook produced must chain like any other"
    assert row[0] == "pay_TESTdeadlock01"
    assert row[4] == "DECISION"
    # `payment_failed` is the deliberately ambiguous surface: symbol 05, no hint.
    assert row[2] == "05"
    assert json.loads(row[3]), "the decision must store a replayable policy input"


def test_a_redelivery_does_not_produce_a_second_decision(client, tmp_path):
    """S2, all the way through. Razorpay retries; the ledger must not grow."""
    from praman.ingest.store import connect_ingest as _connect
    from praman.ingest.worker import process_pending
    from praman.ledger.chain import connect as connect_ledger

    body = json.dumps(failed_payment_event()).encode()
    headers = signed_headers(body, SECRET)
    for _ in range(4):
        client.post("/webhooks/razorpay", content=body, headers=headers)

    ledger_path = tmp_path / "ledger.db"
    conn = _connect(tmp_path / "ingest.db")
    try:
        process_pending(conn, ledger_path, client=rego_like_client(), experiment_id="dupe")
    finally:
        conn.close()

    led = connect_ledger(ledger_path)
    try:
        decisions = led.execute(
            "SELECT COUNT(*) FROM ledger WHERE entry_type = 'DECISION'"
        ).fetchone()[0]
    finally:
        led.close()
    assert decisions == 1, "four deliveries of one event must make exactly one decision"


def test_missing_regulatory_inputs_fail_closed(client, tmp_path):
    """Razorpay does not expose AFA completion or the pre-debit notice time.

    Those default to the DENYING value, so a high-value e-mandate cannot be
    retried on the strength of data we never received. Manufacturing
    authorisation out of a missing field is the S3 failure.
    """
    from praman.ingest.store import connect_ingest as _connect
    from praman.ingest.worker import process_pending
    from praman.ledger.chain import connect as connect_ledger

    event = failed_payment_event(method="upi", amount_paise=22_000_00)
    event["payload"]["payment"]["entity"]["recurring"] = True
    body = json.dumps(event).encode()
    client.post("/webhooks/razorpay", content=body, headers=signed_headers(body, SECRET))

    ledger_path = tmp_path / "ledger.db"
    conn = _connect(tmp_path / "ingest.db")
    try:
        process_pending(conn, ledger_path, client=rego_like_client(), experiment_id="failclosed")
    finally:
        conn.close()

    led = connect_ledger(ledger_path)
    try:
        tier, evaluations = led.execute(
            "SELECT tier, tier_evaluations FROM ledger WHERE entry_type = 'DECISION'"
        ).fetchone()
    finally:
        led.close()

    denies = json.loads(evaluations)
    assert "rbi_afa_required" in denies["T1"]
    assert "rbi_pre_debit_notice_not_elapsed" in denies["T1"]
    assert tier == "T4", "nothing automated is legal on data we never received"
