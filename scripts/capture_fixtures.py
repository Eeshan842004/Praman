"""Capture real Razorpay test-mode failures as committed fixtures.

Praman is a webhook CONSUMER and a REST CALLER (law #10), and this is the REST
half: it reads failed payments straight off the API rather than waiting for a
tunnel. No webhook endpoint has to be publicly reachable to get real error
objects, which is why this runs before any tunnel exists.

WHAT IS COMMITTED. Every fixture goes through the same `redact()` chokepoint the
webhook path uses, so email, contact and VPA become keyed digests and merchant
free-text notes are dropped. The `error_*` fields, which are the entire point,
are kept verbatim. Nothing in `fixtures/` should ever contain a raw identifier.

WHY THIS MATTERS. Until now the ingest layer was tested against payloads
hand-built from Razorpay's published schema. That is a legitimate way to build,
but the claim it supports is "matches the documentation". These fixtures upgrade
it to "verified against live traffic" -- and if a live field contradicts the
documented shape, that contradiction is itself a finding worth writing down.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import httpx  # noqa: E402

from praman.config import settings  # noqa: E402
from praman.ingest.redact import redact  # noqa: E402

API = "https://api.razorpay.com/v1"
OUT = REPO / "fixtures" / "razorpay"


def fetch_payments(key_id: str, key_secret: str, count: int) -> list[dict]:
    """Read the payments list. Test mode only -- keys are `rzp_test_*`."""
    response = httpx.get(
        f"{API}/payments",
        params={"count": count},
        auth=(key_id, key_secret),
        timeout=30.0,
    )
    response.raise_for_status()
    return list(response.json().get("items", []))


def as_event(payment: dict) -> dict:
    """Wrap a REST payment in the webhook envelope.

    Razorpay flattens the error onto the payment entity in both shapes, so one
    envelope lets a captured REST payload and a live webhook body flow through
    exactly the same normaliser. If they needed different handling, the fixture
    would not be testing the code path that runs in production.
    """
    return {
        "entity": "event",
        "account_id": "acc_TESTCAPTURE",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": payment}},
        "created_at": payment.get("created_at"),
        "_source": "razorpay_rest_api",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    key_id = settings.razorpay_key_id
    key_secret = settings.razorpay_key_secret
    if not key_id or not key_secret:
        print("error: RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in .env", file=sys.stderr)
        return 1
    if not key_id.startswith("rzp_test_"):
        print(f"error: refusing to run against non-test keys ({key_id[:8]}...)", file=sys.stderr)
        return 1

    print(f"fetching up to {args.count} payments as {key_id} ...")
    try:
        payments = fetch_payments(key_id, key_secret, args.count)
    except httpx.HTTPStatusError as exc:
        print(f"error: Razorpay returned {exc.response.status_code}", file=sys.stderr)
        return 1

    failed = [p for p in payments if p.get("status") == "failed"]
    print(f"  {len(payments)} payments, {len(failed)} failed")
    if not failed:
        print("nothing to capture. Create some test-card failures first.")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # The webhook secret is the redaction salt, so pseudonyms match the ones the
    # live ingest path produces for the same customer.
    salt = settings.razorpay_webhook_secret or "praman-fixture-salt"

    index = []
    for payment in sorted(failed, key=lambda p: p.get("created_at") or 0):
        event = redact(as_event(payment), salt=salt)
        entity = event["payload"]["payment"]["entity"]
        path = out_dir / f"{entity['id']}.json"
        path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        index.append(
            {
                "payment_id": entity["id"],
                "method": entity.get("method"),
                "error_code": entity.get("error_code"),
                "error_source": entity.get("error_source"),
                "error_step": entity.get("error_step"),
                "error_reason": entity.get("error_reason"),
            }
        )
        print(
            f"  {entity['id']:<22} {entity.get('method', '?'):<6} "
            f"{entity.get('error_reason', '?'):<28} src={entity.get('error_source', '?')}"
        )

    (out_dir / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"\nwrote {len(index)} fixtures to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
