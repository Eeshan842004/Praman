# Praman

**A provable revenue-recovery kernel for Indian payments.**

> *Praman* (प्रमाण) — Sanskrit: **proof; valid evidence; the means by which
> knowledge is justified.** The name is the thesis.

Razorpay AI Buildathon 2026 · Track 03 — AI Revenue Recovery

---

## The problem

Roughly half of all `05 — Do Not Honor` declines are insufficient-funds refusals in
disguise. The issuer sees the balance, the risk score, the velocity, the device.
The merchant sees two digits.

That is the well-known problem. Here is the one nobody talks about:

**Every payments incumbent can recover failed revenue. None of them can prove how
much of it they actually caused.** The industry reports *gross* recovery — which
silently absorbs every customer who would have retried anyway. Razorpay's own
Optimizer performance report asks *"How do you know your payments router is
actually creating value?"* and answers it with correlational GMV analysis, not a
controlled counterfactual.

Praman closes both gaps.

## What it does

1. **Infers the true cause.** The normaliser does not map a decline code to one
   cause — it emits a likelihood vector over nine canonical causes across card
   (ISO 8583 DE-39), Razorpay, and NPCI UPI rails. Code `05` genuinely *is*
   ambiguous; that ambiguity is the product. The model produces the posterior.
2. **Acts under a deterministic policy.** A versioned OPA/Rego bundle is the sole
   authority. Deny by default. Every tier is evaluated, none short-circuited, and
   the complete deny-set is recorded.
3. **Proves what it did.** A SHA-256 hash-chained, append-only ledger records every
   decision *before* actuation, stamped with the bundle revision OPA itself
   reported. `praman verify` replays the entire history against those pinned
   bundles.
4. **Measures honestly.** It does not claim recovered revenue. It validates its
   *estimator* against 200 simulated worlds with sealed ground truth, reporting
   bias, RMSE, and 95% CI coverage — beside the same statistics for the naive
   gross-recovery estimator everyone else reports.

## Architectural law

The eleven non-negotiable laws live in [`CLAUDE.md`](CLAUDE.md). The three that
matter most:

```
1.  The LLM never authorises money. Ever. It parses and it explains.
3.  OPA is the sole authority. Deny by default. allow if count(deny_reason) == 0.
4.  Nothing is actuated before it is recorded. Ledger write precedes side effect.
```

## Quickstart

### With Docker

```bash
cp .env.example .env      # fill in Razorpay test keys + Gemini key
docker compose up --build
curl localhost:8000/healthz
```

### Without Docker (Windows)

```powershell
.\scripts\bootstrap.ps1   # fetches opa.exe + cloudflared.exe into tools/
uv sync
.\scripts\dev.ps1         # OPA sidecar on :8181, FastAPI on :8000
.\scripts\dev.ps1 -Tunnel # + public HTTPS tunnel for Razorpay webhooks
```

### Verify the evidence yourself

The ledger is committed. You do not have to trust the demo video:

```bash
uv run praman verify --ledger data/ledger.db
```

## Policy

```bash
opa test policy/ -v --coverage --threshold 90
./scripts/build_bundle.sh          # or scripts\build_bundle.ps1
```

Bundles are **immutable and committed** under `dist/`. They are evidence, not build
artifacts — `praman verify` replays historical decisions against the exact bundle
that authorised them.

Every threshold lives in `policy/config/data.json` and is covered by the bundle
revision. Zero magic numbers in code. The Visa cap moved 15 → 20 on 25 May 2025;
VAMP's merchant threshold drops to 150 bps on 1 April 2026. Rules move, so they are
configuration, not constants.

## Status

| Phase | Deliverable | State |
|---|---|---|
| 0 | Foundations, policy kernel, toolchain | ✅ |
| 1 | Ingest + canonical taxonomy | ◻ |
| 2 | Ledger + replay attestation | ◻ |
| 3 | Causal simulator with sealed potential outcomes | ◻ |
| 4 | Attribution + Information Capture Ratio | ◻ |
| 5 | Policy bundle attestation | ◻ |
| 6 | Orchestrator + logical clock | ◻ |
| 7 | Estimator validation harness | ◻ |
| 8 | Dashboard + explanations | ◻ |

## Honesty

[`LIMITATIONS.md`](LIMITATIONS.md) states what this does **not** prove, including
the one that matters most: **OPA bounds legality, not correctness.** A confidently
wrong cause produces a legal action on a false premise. That is mitigated by a
confidence floor and a per-payment ceiling — not eliminated.

## Licence

MIT
