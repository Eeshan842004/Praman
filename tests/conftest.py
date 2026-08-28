"""Shared test fixtures."""

from __future__ import annotations

import json

import httpx

from praman.kernel.opa_client import PolicyClient


def rego_like_client() -> PolicyClient:
    """A stand-in that mirrors the frozen retry.rego closely enough to exercise
    the ladder, so the suite runs without a live sidecar.

    It is NOT a substitute for the real thing: the integration tests run the
    identical code path against OPA itself, and they are what prove the rego and
    this mock agree.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        inp = json.loads(request.content)["input"]
        deny: list[str] = []
        if inp["cause_class"] == "hard":
            deny.append("hard_decline")
        if inp.get("network_category") == 1:
            deny.append("visa_cat1")
        if inp.get("merchant_advice_code") in ("03", "21"):
            deny.append("mac_" + inp["merchant_advice_code"])
        if inp.get("npci_retry_remark") == "do_not_reinitiate":
            deny.append("npci_no_retry")
        if inp["attempts_30d"] >= 15:
            deny.append("visa_network_cap")
        if inp["attempts_this_payment"] >= 3:
            deny.append("per_payment_cap")
        if inp["bin_attempts_1h"] >= 10:
            deny.append("bin_velocity")
        if inp["tier"] == "T3" and inp["customer_nudges_7d"] >= 2:
            deny.append("nudge_fatigue_7d")
        if inp["is_emandate"] and inp["amount_paise"] > 1_500_000 and not inp["afa_completed"]:
            deny.append("rbi_afa_required")
        if inp["is_emandate"] and inp["ms_since_pre_debit_notice"] < 86_400_000:
            deny.append("rbi_pre_debit_notice_not_elapsed")
        if inp["rail"] == "upi_autopay" and 10 <= inp["ist_hour"] < 13:
            deny.append("npci_autopay_blackout_window")
        if inp["tier"] in ("T0", "T1", "T2", "T3") and float(inp["max_posterior"]) < 0.40:
            deny.append("low_confidence")
        if inp["tier"] == "T2" and not inp["has_alternate_instrument"]:
            deny.append("no_alternate_instrument")
        return httpx.Response(
            200,
            json={
                "decision_id": "mock-decision",
                "result": {
                    "allow": len(deny) == 0,
                    "deny_reason": sorted(deny),
                    "bundle_revision": "mockrev00000001",
                },
            },
        )

    return PolicyClient(transport=httpx.MockTransport(handler))
