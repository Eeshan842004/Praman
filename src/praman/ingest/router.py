"""POST /webhooks/razorpay — the acknowledgement path.

Four things happen here and nothing else:

    1. verify the HMAC over the RAW bytes
    2. redact
    3. INSERT OR IGNORE, enqueue only if the insert took
    4. return 200

No model. No OPA. No LLM. Not as a performance preference -- as the mitigation
for S2. Razorpay redelivers when an acknowledgement is slow, a redelivery that
reached the attempt counter would be an extra attempt against the payment, and
an extra attempt is a network excessive-reattempt fee. So the latency budget
(p99 < 20 ms) is a COMPLIANCE budget, and everything that thinks runs later,
in the worker, off the request path.

`tests/test_ingest.py` enforces that by sabotage: it replaces the policy client
with one that raises, and this endpoint must still answer 200.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from praman.ingest.fixtures import EVENT_ID_HEADER, SIGNATURE_HEADER
from praman.ingest.redact import redact
from praman.ingest.signature import verify_signature
from praman.ingest.store import connect_ingest, record_delivery
from praman.metrics import DUPLICATES, SIGNATURE_FAILURES, WEBHOOK_ACK


def _payment_id(event: dict[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    block = payload.get("payment")
    if not isinstance(block, dict):
        return None
    entity = block.get("entity")
    return entity.get("id") if isinstance(entity, dict) else None


def build_webhook_router(
    secret: str,
    ingest_path: str | Path,
    now_ms: Any = None,
) -> APIRouter:
    """Build the ingest router bound to one secret and one delivery log."""
    router = APIRouter()
    clock = now_ms or (lambda: int(time.time() * 1000))

    # One connection, reused. Opening SQLite per request would put file creation
    # and WAL setup inside the latency budget for no benefit. The lock is what
    # makes reuse safe when the server hands requests to a thread pool.
    conn: sqlite3.Connection = connect_ingest(ingest_path)
    lock = threading.Lock()

    @router.post("/webhooks/razorpay")
    async def razorpay_webhook(request: Request) -> Response:
        with WEBHOOK_ACK.time():
            # RAW bytes. Never request.json() before verifying -- the signature
            # covers the exact bytes sent, and a parse-then-reserialise would
            # reject signatures Razorpay considers perfectly valid.
            raw = await request.body()

            provided = request.headers.get(SIGNATURE_HEADER)
            if not provided:
                return JSONResponse({"error": "missing signature header"}, status_code=400)

            if not verify_signature(raw, provided, secret):
                # Deliberately 401, not 200. A bad signature is not a delivery
                # to retry, and answering 200 would tell an attacker their
                # forgery was accepted.
                SIGNATURE_FAILURES.inc()
                return JSONResponse({"error": "invalid signature"}, status_code=401)

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                return JSONResponse({"error": "body is not json"}, status_code=422)

            payment_id = _payment_id(event) if isinstance(event, dict) else None
            if not payment_id:
                return JSONResponse({"error": "no payment entity"}, status_code=422)

            event_id = request.headers.get(EVENT_ID_HEADER) or f"noeid_{payment_id}"

            # The chokepoint. Nothing reaches storage that has not been through
            # redact(), which is why it is called here and not in the worker.
            stored = json.dumps(redact(event, salt=secret), separators=(",", ":"))

            with lock:
                is_new = record_delivery(conn, event_id, payment_id, clock(), stored)

            if not is_new:
                # Still a 200. Razorpay retries until it sees one, so answering
                # anything else turns a duplicate into an unbounded retry loop.
                DUPLICATES.inc()
                return JSONResponse({"status": "duplicate", "payment_id": payment_id})

            return JSONResponse({"status": "accepted", "payment_id": payment_id})

    return router


__all__ = ["build_webhook_router"]
