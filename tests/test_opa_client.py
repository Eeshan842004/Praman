"""OPA client (law #9: fail closed).

An unreachable policy engine must mean DENY, never ALLOW. A retry executed
because OPA happened to be down is a compliance violation with no authorising
decision behind it -- the worst outcome the system can produce, strictly worse
than not retrying at all.

The client also carries law #6: `bundle_revision` is whatever OPA REPORTS. We
never substitute a locally computed hash, because that would prove we hashed a
file, not that OPA evaluated it.

Written before the implementation exists.
"""

from __future__ import annotations

import json

import httpx
import pytest

from praman.kernel.opa_client import (
    OPA_UNAVAILABLE,
    UNKNOWN_REVISION,
    PolicyClient,
    PolicyDecision,
)

TIER_INPUT = {
    "cause_class": "soft",
    "network_category": 2,
    "merchant_advice_code": "01",
    "npci_retry_remark": "reinitiate_same_crn",
    "attempts_30d": 3,
    "attempts_this_payment": 1,
    "bin_attempts_1h": 2,
    "tier": "T1",
    "customer_nudges_7d": 0,
    "is_emandate": False,
    "amount_paise": 50000,
    "afa_completed": False,
    "ms_since_pre_debit_notice": 999999999,
    "rail": "card",
    "ist_hour": 15,
    "max_posterior": "0.810000",
    "has_alternate_instrument": True,
}


def _client(handler) -> PolicyClient:
    return PolicyClient(transport=httpx.MockTransport(handler))


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────
def test_allow_is_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "decision_id": "dec-123",
                "result": {"allow": True, "deny_reason": [], "bundle_revision": "abc123"},
            },
        )

    d = _client(handler).evaluate(TIER_INPUT)
    assert d.allow is True
    assert d.deny_reasons == []
    assert d.bundle_revision == "abc123"
    assert d.decision_id == "dec-123"
    assert d.failed_closed is False


def test_deny_reasons_are_sorted_for_stable_hashing():
    """The ledger hashes them. Unsorted reasons would make two identical
    decisions produce different bytes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "allow": False,
                    "deny_reason": ["mac_03", "bin_velocity", "rbi_afa_required"],
                    "bundle_revision": "abc123",
                }
            },
        )

    d = _client(handler).evaluate(TIER_INPUT)
    assert d.deny_reasons == ["bin_velocity", "mac_03", "rbi_afa_required"]


def test_input_is_wrapped_under_the_input_key():
    """OPA's Data API expects {"input": {...}}. Sending the bare object makes
    every rule evaluate against undefined and silently ALLOW nothing."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"result": {"allow": True, "deny_reason": []}})

    _client(handler).evaluate(TIER_INPUT)
    assert set(seen) == {"input"}
    assert seen["input"]["tier"] == "T1"


# ─────────────────────────────────────────────────────────────────────────────
# LAW #9 -- every failure mode must DENY
# ─────────────────────────────────────────────────────────────────────────────
def test_connection_refused_fails_closed():
    """The kill-OPA case. Nothing is listening; the answer must still be deny."""
    d = PolicyClient(base_url="http://127.0.0.1:1").evaluate(TIER_INPUT)
    assert d.allow is False
    assert d.deny_reasons == [OPA_UNAVAILABLE]
    assert d.bundle_revision == UNKNOWN_REVISION
    assert d.failed_closed is True


@pytest.mark.parametrize("status", [400, 404, 500, 502, 503])
def test_http_error_fails_closed(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="boom")

    d = _client(handler).evaluate(TIER_INPUT)
    assert d.allow is False and d.failed_closed is True


def test_timeout_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    d = _client(handler).evaluate(TIER_INPUT)
    assert d.allow is False and d.failed_closed is True


def test_malformed_body_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    d = _client(handler).evaluate(TIER_INPUT)
    assert d.allow is False and d.failed_closed is True


def test_missing_result_key_fails_closed():
    """An undefined Rego path returns 200 with an EMPTY body. That is not an
    allow -- it means the policy did not evaluate, and treating it as allow
    would be the single most dangerous bug in the system."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    d = _client(handler).evaluate(TIER_INPUT)
    assert d.allow is False and d.failed_closed is True


def test_result_without_allow_key_denies():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"deny_reason": []}})

    assert _client(handler).evaluate(TIER_INPUT).allow is False


# ─────────────────────────────────────────────────────────────────────────────
# LAW #6 -- the revision must come FROM OPA
# ─────────────────────────────────────────────────────────────────────────────
def test_revision_is_taken_from_opas_response_not_computed_locally():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "allow": True,
                    "deny_reason": [],
                    "bundle_revision": "revision-only-opa-knows",
                }
            },
        )

    assert _client(handler).evaluate(TIER_INPUT).bundle_revision == "revision-only-opa-knows"


def test_missing_revision_is_marked_unknown_not_guessed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"allow": True, "deny_reason": []}})

    assert _client(handler).evaluate(TIER_INPUT).bundle_revision == UNKNOWN_REVISION


# ─────────────────────────────────────────────────────────────────────────────
# The decision is ledger-ready
# ─────────────────────────────────────────────────────────────────────────────
def test_decision_is_immutable():
    d = PolicyDecision(
        allow=True, deny_reasons=[], bundle_revision="r", decision_id=None, failed_closed=False
    )
    with pytest.raises((AttributeError, TypeError)):
        d.allow = False  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# Integration: against a REAL OPA sidecar. Skipped when it is not running.
# ─────────────────────────────────────────────────────────────────────────────
def _opa_up(url: str = "http://127.0.0.1:8181") -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=1.0).status_code == 200
    except Exception:
        return False


requires_opa = pytest.mark.skipif(not _opa_up(), reason="OPA sidecar not running on :8181")


@pytest.mark.integration
@requires_opa
def test_live_opa_allows_a_clean_soft_decline():
    d = PolicyClient().evaluate(TIER_INPUT)
    assert d.allow is True
    assert d.failed_closed is False
    assert d.bundle_revision not in ("", UNKNOWN_REVISION)
    assert d.decision_id  # OPA mints one per query


@pytest.mark.integration
@requires_opa
def test_live_opa_returns_the_full_deadlock_deny_set():
    """S3 against the real engine: four regulators, one payment, no
    short-circuit."""
    d = PolicyClient().evaluate(
        {
            **TIER_INPUT,
            "tier": "T3",
            "customer_nudges_7d": 2,
            "is_emandate": True,
            "amount_paise": 2200000,
            "rail": "upi_autopay",
            "ist_hour": 11,
            "ms_since_pre_debit_notice": 1000,
        }
    )
    assert d.allow is False
    assert d.deny_reasons == [
        "npci_autopay_blackout_window",
        "nudge_fatigue_7d",
        "rbi_afa_required",
        "rbi_pre_debit_notice_not_elapsed",
    ]


@pytest.mark.integration
@requires_opa
def test_live_opa_revision_matches_the_committed_bundle():
    """Law #6 end to end: what OPA reports must equal what we pinned on disk."""
    import json as _json
    from pathlib import Path

    on_disk = _json.loads(Path("policy/revision/data.json").read_text(encoding="utf-8"))["revision"]
    assert PolicyClient().evaluate(TIER_INPUT).bundle_revision == on_disk
