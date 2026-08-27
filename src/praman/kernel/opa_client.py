"""Client for the OPA policy sidecar.

Two architectural laws are implemented here and nowhere else.

Law #9 -- FAIL CLOSED. Every failure path returns deny. An unreachable policy
engine means "we could not obtain authorisation", which is not the same as
"authorised", and the difference is a compliance violation with no decision
record behind it. The one genuinely dangerous bug this file exists to prevent
is treating an empty 200 as an allow: querying an undefined Rego path returns
`{}` with status 200, and reading `.get("allow", True)` there would silently
authorise everything.

Law #6 -- the bundle revision is whatever OPA REPORTS. A locally computed hash
proves we hashed a file; it does not prove that file is what evaluated the
input. `retry.rego` echoes `data.revision.revision`, and we store that.

OPERATIONAL REQUIREMENT: OPA must run with decision logging enabled
(`--set=decision_logs.console=true`). It only mints a `decision_id` when
decision logs are on, and that id is the join key between our ledger and OPA's
own independently-written log -- the second evidence stream. With logging off
the client still works, but every decision records a null id and the dual-control
property is silently lost. Both docker-compose.yml and scripts/dev.ps1 enable it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from praman.config import settings
from praman.metrics import OPA_FAILURES, OPA_LATENCY

OPA_UNAVAILABLE = "opa_unavailable"
UNKNOWN_REVISION = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allow: bool
    deny_reasons: list[str] = field(default_factory=list)
    bundle_revision: str = UNKNOWN_REVISION
    decision_id: str | None = None
    # Distinguishes "policy said no" from "we could not ask". Both deny, but the
    # ledger must be able to tell them apart -- one is a compliance outcome, the
    # other is an outage.
    failed_closed: bool = False


def _deny_closed() -> PolicyDecision:
    return PolicyDecision(
        allow=False,
        deny_reasons=[OPA_UNAVAILABLE],
        bundle_revision=UNKNOWN_REVISION,
        decision_id=None,
        failed_closed=True,
    )


class PolicyClient:
    """Synchronous OPA client.

    Sync because the batch runner is sync and the policy call is sub-millisecond
    against a local sidecar. The webhook path in Phase 1 will need an async
    variant; the parsing below is deliberately factored out so it can be shared.
    """

    def __init__(
        self,
        base_url: str | None = None,
        decision_path: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = (
            f"{(base_url or settings.opa_url).rstrip('/')}"
            f"/v1/data/{(decision_path or settings.opa_decision_path).strip('/')}"
        )
        self._client = httpx.Client(
            timeout=timeout or settings.opa_timeout_seconds,
            transport=transport,
        )

    def evaluate(self, tier_input: dict[str, Any]) -> PolicyDecision:
        try:
            with OPA_LATENCY.time():
                resp = self._client.post(self._url, json={"input": tier_input})
            resp.raise_for_status()
            body = resp.json()
        except Exception:
            OPA_FAILURES.inc()
            return _deny_closed()

        return self._parse(body)

    @staticmethod
    def _parse(body: Any) -> PolicyDecision:
        if not isinstance(body, dict) or "result" not in body:
            # An undefined Rego path returns 200 with an empty body. That is not
            # an allow -- the policy did not evaluate.
            OPA_FAILURES.inc()
            return _deny_closed()

        result = body.get("result")
        if not isinstance(result, dict):
            OPA_FAILURES.inc()
            return _deny_closed()

        reasons = result.get("deny_reason") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]

        return PolicyDecision(
            # Default False, never True: absence of an explicit allow is a deny.
            allow=bool(result.get("allow", False)),
            deny_reasons=sorted(str(r) for r in reasons),
            bundle_revision=str(result.get("bundle_revision") or UNKNOWN_REVISION),
            decision_id=body.get("decision_id"),
            failed_closed=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PolicyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["OPA_UNAVAILABLE", "UNKNOWN_REVISION", "PolicyClient", "PolicyDecision"]
