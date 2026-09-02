"""Three views over the committed ledger.

It reads the SAME append-only file `praman verify` attests. There is no
analytics store and no cache of derived numbers, so the dashboard cannot drift
from the evidence -- if a page shows it, an auditor can re-derive it from the
same rows.

Nothing here evaluates policy, calls a model, or writes. A page load is a read
of decisions that were made, recorded and attested before the request existed.
`tests/test_dashboard.py` enforces that by sabotage.

Jinja2 and a Tailwind CDN script, deliberately: no build step, no React, no
charting library. The tier distribution is a row of divs whose widths are
percentages, which is all a distribution bar has ever been.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from praman.config import settings
from praman.explain.cache import ArchetypeCache
from praman.explain.from_ledger import summary_at
from praman.explain.service import ExplanationService
from praman.kernel.ladder import proposed_tiers_for
from praman.ledger.chain import connect, verify
from praman.measure.from_ledger import estimate_from_ledger, naive_gross_from_ledger
from praman.taxonomy import CAUSES

TEMPLATES = Path(__file__).with_name("templates")

TIER_ACTION = {
    "T0": "terminate",
    "T1": "silent retry",
    "T2": "rail switch",
    "T3": "customer nudge",
    "T4": "human escalate",
}

# Kept here rather than indexed by loop position in the template: the bar must
# stay the same colour for a tier even when a tier is absent from a batch.
TIER_COLOUR = {
    "T0": "var(--t0)",
    "T1": "var(--t1)",
    "T2": "var(--t2)",
    "T3": "var(--t3)",
    "T4": "var(--t4)",
}


# A posterior at or above this is not ambiguous, and the page must not describe
# it as such. Sharpness where the decline code is informative is as much the
# thesis as width where it is not -- the taxonomy is meant to do both.
SHARP_POSTERIOR = 0.95


def _tier_states(summary) -> list[dict]:
    """What happened at every tier, with THREE distinct outcomes.

    Rendering only allow/deny conflates two different things and misreads the
    audit trail. A tier can be:

        taken          the ladder proposed it, policy allowed it, we acted
        denied         policy refused it, with reasons
        not preferred  policy allowed it, but a higher-ranked tier also
                       allowed and won. Legal, not chosen.
        not proposed   the ladder never asked -- the cause is not retryable, or
                       is a hard decline. Policy permission is irrelevant here.

    Without the last two, "T1 ALLOW" beside a T3 outcome reads as ignoring a
    permitted action. The real reason is preference order: T3 is the default
    tier for an authentication failure, because asking the customer to redo the
    OTP is the correct fix, and it is tried first.

    Eligibility comes from `proposed_tiers_for`, the same function the kernel
    uses -- never a copy of the rule.
    """
    proposed = proposed_tiers_for(summary.cause)
    rank = {tier: i + 1 for i, tier in enumerate(proposed)}
    out = []

    for tier in ("T1", "T2", "T3", "T4"):
        reasons = sorted(summary.tier_evaluations.get(tier, []))
        row = {"tier": tier, "reasons": reasons, "rank": rank.get(tier)}

        if tier == summary.tier:
            row |= {"state": "taken", "note": "the action we took"}
        elif reasons:
            row |= {"state": "deny", "note": ""}
        elif tier == "T4":
            # Never "proposed": escalation is the terminal fallback, always
            # evaluated and unconditionally legal. It is not a missed action.
            row |= {"state": "fallback", "note": "terminal fallback, always legal"}
        elif tier in rank:
            row |= {
                "state": "not-preferred",
                "note": f"legal, but ranked #{rank[tier]} for this cause",
            }
        else:
            row |= {
                "state": "not-proposed",
                "note": "the ladder never asked: this cause is not retryable",
            }
        out.append(row)
    return out


def _estimator_artifact() -> dict | None:
    """The 200-world validation, measured once and saved.

    Recomputing it per request would take minutes; quoting it from memory would
    be fabrication. So it is an artifact on disk, or the page says it is missing.
    """
    path = Path(__file__).resolve().parents[3] / "docs" / "estimator_validation.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _experiments(conn: sqlite3.Connection) -> list[dict]:
    """Per-experiment effect estimates, read straight out of the ledger."""
    names = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT experiment_id FROM ledger WHERE entry_type = 'DECISION' "
            "ORDER BY experiment_id"
        )
    ]
    out = []
    for name in names:
        row: dict = {"name": name}
        try:
            estimate = estimate_from_ledger(conn, name, n_boot=600)
            row |= {
                "tau": estimate.tau_hat / 100,
                "lo": estimate.ci_lo / 100,
                "hi": estimate.ci_hi / 100,
                "excludes_zero": estimate.ci_lo > 0 or estimate.ci_hi < 0,
                "naive": naive_gross_from_ledger(conn, name) / 100,
                "clusters": min(estimate.n_clusters_treatment, estimate.n_clusters_holdout),
            }
        except (ValueError, ZeroDivisionError):
            # An arm with no clusters, e.g. a span too small to randomise.
            # Say so rather than rendering a number that is not there.
            row["unavailable"] = True
        out.append(row)
    return out


def build_dashboard_router(
    ledger_path: str | Path | None = None,
    explain_cache: Path | None = None,
) -> APIRouter:
    router = APIRouter()
    templates = Jinja2Templates(directory=str(TEMPLATES))
    path = Path(ledger_path or settings.ledger_abspath)

    # No LLM client: a page load must not make a network call. Cached prose
    # renders when a prewarm produced it, and the deterministic template
    # otherwise -- so the page is complete either way.
    explain = ExplanationService(
        cache=ArchetypeCache(explain_cache or settings.explain_cache_abspath), client=None
    )

    def _open() -> sqlite3.Connection:
        if not path.exists():
            raise HTTPException(status_code=503, detail=f"no ledger at {path}")
        return connect(path)

    @router.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        conn = _open()
        try:
            tiers = Counter(
                r[0] for r in conn.execute("SELECT tier FROM ledger WHERE entry_type = 'DECISION'")
            )
            total = sum(tiers.values()) or 1
            violations = conn.execute(
                "SELECT COUNT(*) FROM ledger a WHERE a.entry_type = 'ACTUATION' "
                "AND a.executed = 1 AND NOT EXISTS ("
                "  SELECT 1 FROM ledger d WHERE d.seq = a.decision_seq "
                "  AND d.entry_type = 'DECISION' AND d.opa_allow = 1)"
            ).fetchone()[0]
            actuations = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE entry_type = 'ACTUATION' AND executed = 1"
            ).fetchone()[0]
            entries = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
            recent = [
                {"seq": r[0], "payment_id": r[1], "tier": r[2], "cause": r[3]}
                for r in conn.execute(
                    "SELECT seq, payment_id, tier, cause FROM ledger "
                    "WHERE entry_type = 'DECISION' ORDER BY seq DESC LIMIT 25"
                )
            ]
            experiments = _experiments(conn)
            bundles = conn.execute(
                "SELECT COUNT(DISTINCT bundle_revision) FROM ledger WHERE entry_type = 'DECISION'"
            ).fetchone()[0]
        finally:
            conn.close()

        return templates.TemplateResponse(
            request,
            "overview.html",
            {
                "tiers": [
                    {
                        "tier": t,
                        "action": TIER_ACTION[t],
                        "count": tiers.get(t, 0),
                        "pct": 100.0 * tiers.get(t, 0) / total,
                        "colour": TIER_COLOUR[t],
                    }
                    for t in ("T0", "T1", "T2", "T3", "T4")
                ],
                "total": total,
                "violations": violations,
                "actuations": actuations,
                "entries": entries,
                "recent": recent,
                "experiments": experiments,
                "primary": _estimator_artifact(),
                "bundles": bundles,
                "page": "batch",
            },
        )

    @router.get("/decision/{seq}", response_class=HTMLResponse)
    async def decision(request: Request, seq: int) -> HTMLResponse:
        conn = _open()
        try:
            summary = summary_at(conn, seq)
            if summary is None:
                raise HTTPException(status_code=404, detail=f"no decision at entry {seq}")
            row = conn.execute(
                "SELECT posterior_vector, symbol, arm, opa_allow, decision_id "
                "FROM ledger WHERE seq = ?",
                (seq,),
            ).fetchone()
        finally:
            conn.close()

        vector = json.loads(row[0] or "{}")
        posterior = sorted(
            ({"cause": c, "p": float(vector.get(c, 0.0))} for c in CAUSES),
            key=lambda d: d["p"],
            reverse=True,
        )
        explanation = explain.explain(summary)

        return templates.TemplateResponse(
            request,
            "decision.html",
            {
                "s": summary,
                "posterior": posterior,
                "tier_states": _tier_states(summary),
                "is_sharp": summary.confidence >= SHARP_POSTERIOR,
                "symbol": row[1],
                "arm": row[2],
                "opa_allow": bool(row[3]),
                "decision_id": row[4],
                "explanation": explanation,
                "tier_action": TIER_ACTION,
                "tiers": ["T1", "T2", "T3", "T4"],
                "page": "decision",
            },
        )

    @router.get("/attestation", response_class=HTMLResponse)
    async def attestation(request: Request) -> HTMLResponse:
        conn = _open()
        try:
            ok, broken_at, message = verify(conn)
            spans = [
                {"revision": r[0], "count": r[1], "lo": r[2], "hi": r[3]}
                for r in conn.execute(
                    "SELECT bundle_revision, COUNT(*), MIN(seq), MAX(seq) FROM ledger "
                    "WHERE entry_type = 'DECISION' GROUP BY bundle_revision ORDER BY MIN(seq)"
                )
            ]
            entries = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
            decisions = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE entry_type = 'DECISION'"
            ).fetchone()[0]
            unreplayable = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE entry_type = 'DECISION' "
                "AND (policy_input_json IN ('{}', '') OR policy_input_json IS NULL)"
            ).fetchone()[0]
        finally:
            conn.close()

        return templates.TemplateResponse(
            request,
            "attestation.html",
            {
                "ok": ok,
                "broken_at": broken_at,
                "message": message,
                "spans": spans,
                "entries": entries,
                "decisions": decisions,
                "unreplayable": unreplayable,
                "ledger": str(path),
                "page": "attestation",
            },
        )

    return router


__all__ = ["TIER_ACTION", "TIER_COLOUR", "build_dashboard_router"]
