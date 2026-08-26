# Praman — Architectural Laws (non-negotiable)

> These eleven laws govern every line of code in this repository. They are not
> style preferences. Each exists because violating it produces a specific, named
> failure documented in §1 of the blueprint. If generated code violates a law,
> **the code is wrong — not the law.**

```
1.  The LLM never authorises money. Ever. It parses and it explains.
2.  An LLM output may narrow authority. It may never widen it.
3.  OPA is the sole authority. Deny by default. allow if count(deny_reason) == 0.
4.  Nothing is actuated before it is recorded. Ledger write precedes side effect.
5.  No floats in the ledger. Money = integer paise. Probability = 6-dp string. Time = epoch ms int.
6.  Every decision stores the bundle revision OPA reported, not one we computed.
7.  Counters increment on actuation, never on ingest.
8.  Randomisation is deterministic: blake2b(experiment_id | customer_id).
9.  Fail closed. OPA unreachable = deny = T4.
10. Praman is a webhook CONSUMER and a REST CALLER that sits beside Razorpay.
    It is never an MCP server and never exposes itself as an agent tool surface.
11. Every threshold lives in policy config. Zero magic numbers in code.
```

Laws 1–9 are verbatim from blueprint §2.1. Law 10 fixes the deployment topology.
Law 11 is verbatim from the blueprint's Phase 0 `CLAUDE.md` block.

---

## Implementation notes on the laws

**Law 10 — what "sits beside Razorpay" means concretely.**
Praman has exactly two integration surfaces with Razorpay and no others:

| Direction | Surface | Detail |
|---|---|---|
| Inbound | Webhook consumer | `POST /webhooks/razorpay` receives `payment.failed`. HMAC-verified. Praman never polls. |
| Outbound | REST caller | Praman calls the Razorpay REST API to actuate a recovery action. |

Praman does **not** implement the Model Context Protocol, does not register as a
tool provider, and does not expose its decision engine to an external agent. The
kernel's authority must never be reachable by anything that can be prompted.
`razorpay-mcp-server` may be consumed as a *client* convenience during
development; it is never part of the actuation path.

**Law 11 — where the thresholds actually live.**
Path is `policy/config/data.json`, not `policy/config.json`. OPA resolves data
documents by directory: a file at `policy/config/data.json` loads as `data.config.*`,
which is what `retry.rego` reads. A file literally named `policy/config.json` would
merge into the root of `data` and every `data.config.*` reference would evaluate to
undefined — silently turning every threshold check into a no-op. Same rule for
`policy/revision/data.json` → `data.revision.revision`.

---

## Failure modes these laws prevent

| Law | Prevents | Named failure |
|---|---|---|
| 4, 5 | Unreplayable ledger; hash chain breaks on re-serialisation | S1, C4 |
| 7 | Slow webhook → duplicate delivery → inflated attempt counter → **regulatory violation** | S2 |
| 3 | One loosely-written positive rule leaks a violation through | S3 |
| 6 | Policy drift silently breaks replay attestation | S4 |
| 1, 2, 10 | Confidently-wrong or prompt-injected cause routes a hard decline into a soft tier | S5, S6 |
| 8 | Treatment leaks into holdout via a shared customer → SUTVA violation | S7 |
| 9 | A retry executed because the policy engine was down — worst possible outcome | S3 |

---

## Invariants that must hold at all times

```
verify(ledger)                     == True          # chain intact, always
praman_policy_violations_total     == 0             # the compliance story
opa unreachable                    ⇒ tier == "T4"   # fail closed
assign_arm(exp, cust) is pure      == True          # same input, same arm, forever
canonical_bytes(x) == canonical_bytes(x)            # byte-stable across runs
float in ledger payload            ⇒ TypeError      # no exceptions
attempts_30d                       counts actuations, not deliveries
```

---

## Working doctrine

1. **One phase per session.** `/clear` between phases. Context rot is real.
2. **Tests before implementation.** Write the failing test first, then implement.
3. **Never accept code you cannot explain.** You will be on camera answering "why?"
   for five minutes. If you cannot explain it, delete it.
4. **Commit after every green test run.** Small commits are the undo button and the
   build narrative.
5. **`BACKLOG.md` absorbs every new idea.** No exceptions after Day 0.

---

## Sacrifice order (if the schedule slips)

Cut in **this order**, never out of order:

1. Next.js dashboard (Jinja2 already ships)
2. Hazard timing model (fixed per-cause delays instead)
3. T2 rail switch (ship T0/T1/T3/T4)
4. SHAP explanations
5. The ML classifier itself (heuristic attribution per Laumans)

**Never sacrifice:** the ledger, the policy kernel, or the estimator validation
harness. A policy kernel with heuristic attribution, a verifiable ledger, and a
validated causal estimator is a *complete and honest* submission. A sophisticated
model with an unvalidated estimator is the submission everyone else is building.

---

## Local toolchain (this machine)

Docker is not installed. OPA runs as a local sidecar binary; `docker-compose.yml`
is authored and committed for reproducibility on a judge's machine and verified in
CI, but the local dev loop uses `scripts/dev.ps1`.

| Tool | Version | Location |
|---|---|---|
| Python | 3.12.14 | pinned via `.python-version` |
| uv | 0.12.6 | on PATH |
| OPA | 1.19.1 (Rego v1) | `tools/opa.exe` (gitignored) |
| cloudflared | 2026.8.2 | `tools/cloudflared.exe` (gitignored) |
