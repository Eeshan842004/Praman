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
   reported — together with the exact policy input it was judged on. `praman verify`
   does two independent things: it recomputes the chain, and it re-POSTs every
   stored input to OPA loaded with that entry's pinned bundle and compares the
   verdict. The chain proves nothing was changed afterwards; the replay proves the
   record was true when written. A chain alone cannot catch a writer that bypassed
   the policy engine and recorded its own verdict.
4. **Measures honestly.** It does not claim recovered revenue. It validates its
   *estimator* against 200 simulated worlds with sealed ground truth, reporting
   bias, RMSE, and 95% CI coverage — beside the same statistics for the naive
   gross-recovery estimator everyone else reports. Batch size is set by a power
   calculation before the run, not chosen after seeing the result.
5. **Knows when its own model is not worth shipping.** Gate 1 trained LightGBM,
   measured it against the heuristic on the same batch, and shipped the
   heuristic — the model was more confident and less informative. The reasoning
   is in [`docs/GATE_LOG.md`](docs/GATE_LOG.md).

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

You do not have to trust the demo video — generate the evidence and attest it:

```bash
.\scripts\dev.ps1                      # OPA sidecar on :8181
uv run praman run-batch --n 3000       # writes data/ledger.db
uv run praman verify --ledger data/ledger.db
```

`verify` recomputes the hash chain **and** replays every recorded decision against
the committed bundle in `dist/` that authorised it. It needs the OPA binary
(`tools/opa.exe`, fetched by `scripts/bootstrap.ps1`); without it the chain is
still checked and the replay is reported as skipped rather than silently assumed.

Then break it on purpose:

```bash
uv run praman tamper --ledger data/ledger.db --entry 447 --set amount_paise=99999900
uv run praman verify  --ledger data/ledger.db     # -> ATTESTATION FAIL
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
| 1 | Canonical taxonomy + likelihood matrix | ✅ (ingest pending Razorpay keys) |
| 2 | Ledger + replay attestation | ✅ |
| 3 | Causal simulator with sealed potential outcomes | ✅ |
| 4 | Attribution + Information Capture Ratio | ✅ (Gate 1: heuristic ships — [`GATE_LOG.md`](docs/GATE_LOG.md)) |
| 5 | Policy bundle attestation + replay | ✅ |
| 6 | Orchestrator + logical clock | ✅ (escalation ladder T0–T4) |
| 7 | Estimator validation harness | ✅ (scored on the real simulator) |
| 8 | Dashboard + explanations | ◻ |

## Honesty

[`LIMITATIONS.md`](LIMITATIONS.md) states what this does **not** prove, including
the one that matters most: **OPA bounds legality, not correctness.** A confidently
wrong cause produces a legal action on a false premise. That is mitigated by a
confidence floor and a per-payment ceiling — not eliminated.

## Licence

MIT
