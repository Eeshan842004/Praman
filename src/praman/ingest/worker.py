"""Off the request path: turn accepted deliveries into ledger decisions.

Everything that thinks lives here. The endpoint acknowledges in single-digit
milliseconds; this is where normalisation, the posterior, the ladder and the OPA
call happen, and where the DECISION row is written.

Law #7 is the load-bearing detail. Every compliance counter below is derived
from ACTUATION rows with `executed = 1` -- never from deliveries, never from
decisions. A webhook redelivery is an observation; a decision policy refused is
not an attempt. Counting either would inflate the attempt count against a
payment, which is the regulatory failure (S2) the ingest design exists to stop.

WHAT RAZORPAY DOES NOT TELL US, and what we do about it. Four policy inputs
cannot be derived from a `payment.failed` payload:

    afa_completed              not exposed. Defaults False -> high-value
                               e-mandates DENY.
    ms_since_pre_debit_notice  the merchant sends the notice, so only the
                               merchant knows. Defaults 0 -> e-mandates DENY
                               until that log is wired in.
    has_alternate_instrument   needs a REST lookup of the customer's saved
                               instruments. Defaults False -> T2 DENIES.
    bin_attempts_1h            the BIN is genuinely not in the payload (only
                               last4 and issuer), so this counter cannot be
                               computed here at all.

The first three default to the value that DENIES. Defaulting the other way
would manufacture authorisation out of missing data, which is the S3 failure.

The fourth is different and worth being honest about: 0 does NOT deny, so BIN
velocity is simply unenforceable from webhook data alone. That is an enforcement
GAP, not a safe default, and it is recorded in LIMITATIONS rather than hidden
behind a plausible-looking zero. Closing it needs the merchant's own attempt log
keyed by BIN.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from praman.ingest.fixtures import error_object
from praman.ingest.store import mark_processed, pending
from praman.kernel.counters import WINDOW_7D, WINDOW_30D
from praman.kernel.ladder import DeclineContext, evaluate_ladder
from praman.kernel.opa_client import PolicyClient
from praman.ledger.chain import append, connect
from praman.ledger.records import DecisionRecord
from praman.measure.assign import DEFAULT_HOLDOUT_PCT, assign_arm
from praman.metrics import ATTRIBUTION_CONFIDENCE, DECISIONS
from praman.taxonomy import load_taxonomy
from praman.taxonomy.normalise import normalise_razorpay

ATTRIBUTION_VERSION = "taxonomy-v1"

# IST is UTC+5:30 with no daylight saving, so the offset is a constant. The NPCI
# AutoPay blackout window is defined in IST -- getting this wrong would misapply
# a real regulatory rule by five and a half hours.
IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000
_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000


def _rail(method: str | None, entity: dict[str, Any]) -> str:
    if method == "upi":
        return "upi_autopay" if entity.get("recurring") else "upi"
    return method or "card"


def _customer_key(entity: dict[str, Any]) -> str:
    """The randomisation unit, post-redaction.

    Falls through the identifiers Razorpay actually populates. Every one of them
    is already a keyed digest by the time the worker sees it -- redact()
    pseudonymises rather than deletes precisely so this key still exists. A
    payment with no identifier at all becomes its own cluster, which is the
    conservative choice: it cannot leak treatment into another customer's arm.
    """
    for field in ("customer_id", "email", "contact", "vpa"):
        value = entity.get(field)
        if value:
            return str(value)
    return f"anon_payment_{entity.get('id', 'unknown')}"


def _actuations(
    conn: sqlite3.Connection,
    column: str,
    value: str,
    now_ms: int,
    window_ms: int | None = None,
    tier: str | None = None,
) -> int:
    """Count EXECUTED actuations. Law #7, in SQL."""
    sql = (
        "SELECT COUNT(*) FROM ledger WHERE entry_type = 'ACTUATION' "
        f"AND executed = 1 AND {column} = ?"  # a literal from this module, never input
    )
    params: list[Any] = [value]
    if window_ms is not None:
        sql += " AND ts_ms BETWEEN ? AND ?"
        params += [now_ms - window_ms, now_ms]
    if tier is not None:
        sql += " AND tier = ?"
        params.append(tier)
    return int(conn.execute(sql, params).fetchone()[0])


def _covariate(conn: sqlite3.Connection, customer_id: str, amount_paise: int, ts_ms: int) -> float:
    """Expected recovery: amount x this customer's prior recovery rate.

    Strictly pre-treatment by construction -- only OUTCOME rows written STRICTLY
    BEFORE this decision are read. CUPED is unbiased only under that condition,
    so it is enforced in the query rather than asserted in prose.
    """
    row = conn.execute(
        "SELECT AVG(recovered) FROM ledger WHERE entry_type = 'OUTCOME' "
        "AND customer_id = ? AND ts_ms < ?",
        (customer_id, ts_ms),
    ).fetchone()
    rate = row[0] if row and row[0] is not None else 0.5
    return float(amount_paise) * float(rate)


def process_pending(
    ingest_conn: sqlite3.Connection,
    ledger_path: str | Path,
    client: PolicyClient | None = None,
    experiment_id: str = "praman-v1",
    holdout_pct: int = DEFAULT_HOLDOUT_PCT,
    region: str = "IN",
    limit: int = 1000,
    now_ms: Callable[[], int] | None = None,
) -> list[int]:
    """Drain the delivery queue into ledger DECISION rows.

    Returns the sequence numbers written, and records each one against its
    delivery -- that link is what lets an auditor walk from a webhook Razorpay
    sent to the decision it caused.
    """
    tax = load_taxonomy()
    client = client or PolicyClient()
    ledger = connect(ledger_path)
    written: list[int] = []

    try:
        for row in pending(ingest_conn, limit):
            event = json.loads(row["raw_json"])
            entity = event["payload"]["payment"]["entity"]

            rail = _rail(entity.get("method"), entity)
            obs = normalise_razorpay(error_object(event), rail=rail)
            posterior = tax.posterior(obs, region=region)
            cause = max(posterior, key=lambda c: posterior[c])
            ATTRIBUTION_CONFIDENCE.observe(posterior[cause])

            created = int(entity.get("created_at") or 0)
            ts_ms = created * 1000 if created else int(row["received_at_ms"])
            customer_id = _customer_key(entity)
            payment_id = str(entity["id"])
            amount_paise = int(entity.get("amount") or 0)

            ctx = DeclineContext(
                cause=cause,
                max_posterior=posterior[cause],
                rail=rail,
                amount_paise=amount_paise,
                network_category=obs.network_category,
                merchant_advice_code=obs.merchant_advice_code,
                npci_retry_remark=obs.npci_retry_remark,
                attempts_30d=_actuations(ledger, "customer_id", customer_id, ts_ms, WINDOW_30D),
                attempts_this_payment=_actuations(ledger, "payment_id", payment_id, ts_ms),
                bin_attempts_1h=0,  # not derivable from the payload; see module docstring
                customer_nudges_7d=_actuations(
                    ledger, "customer_id", customer_id, ts_ms, WINDOW_7D, tier="T3"
                ),
                is_emandate=rail == "upi_autopay" or bool(entity.get("recurring")),
                afa_completed=bool(entity.get("afa_completed", False)),
                ms_since_pre_debit_notice=int(entity.get("ms_since_pre_debit_notice") or 0),
                ist_hour=int(((ts_ms + IST_OFFSET_MS) % _DAY_MS) // _HOUR_MS),
                has_alternate_instrument=bool(entity.get("has_alternate_instrument", False)),
            )

            ladder = evaluate_ladder(ctx, client)
            arm = assign_arm(experiment_id, customer_id, holdout_pct)
            DECISIONS.labels(tier=ladder.selected_tier, allow=str(ladder.is_action).lower()).inc()

            # Law #4: recorded BEFORE anything is actuated.
            digest = append(
                ledger,
                DecisionRecord(
                    ts_ms=ts_ms,
                    experiment_id=experiment_id,
                    holdout_pct=holdout_pct,
                    payment_id=payment_id,
                    customer_id=customer_id,
                    arm=arm,
                    attempt_no=ctx.attempts_this_payment + 1,
                    rail=rail,
                    symbol=obs.symbol,
                    region=region,
                    cause=cause,
                    posterior=posterior,
                    attribution_source="heuristic",
                    attribution_version=ATTRIBUTION_VERSION,
                    tier=ladder.selected_tier,
                    tier_evaluations=ladder.as_tier_evaluations(),
                    opa_allow=ladder.recorded_opa_allow,
                    deny_reasons=ladder.recorded_deny_reasons,
                    policy_input=ladder.recorded_policy_input,
                    bundle_revision=ladder.bundle_revision,
                    decision_id=ladder.decision_id,
                    amount_paise=amount_paise,
                    cuped_covariate=_covariate(ledger, customer_id, amount_paise, ts_ms),
                    covariate_asof_ms=ts_ms,
                    payload={"source": "razorpay_webhook", "redacted": True},
                ).to_row(),
            )
            seq = int(
                ledger.execute("SELECT seq FROM ledger WHERE entry_hash = ?", (digest,)).fetchone()[
                    0
                ]
            )
            mark_processed(ingest_conn, row["event_id"], row["payment_id"], seq)
            written.append(seq)
    finally:
        ledger.close()
        client.close()

    return written


__all__ = ["ATTRIBUTION_VERSION", "IST_OFFSET_MS", "process_pending"]
