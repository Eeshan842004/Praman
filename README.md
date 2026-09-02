# Praman

**A provable revenue-recovery kernel for Indian payments.**

> *Praman* (प्रमाण) — Sanskrit: **proof; valid evidence; the means by which
> knowledge is justified.** The name is the thesis.

Razorpay AI Buildathon 2026 · Track 03 — AI Revenue Recovery

---

## The problem

Roughly half of all `05 — Do Not Honor` declines are insufficient-funds refusals
in disguise: the issuer sees the balance, the risk score, the velocity and the
device, and the merchant sees two digits. That much is well known. The part
nobody talks about is that **every payments incumbent can recover failed revenue
and none of them can prove how much of it they actually caused** — the industry
reports *gross* recovery, which silently absorbs every customer who would have
retried anyway. Praman infers the cause as a distribution rather than a guess,
acts only where a versioned policy engine permits, records every decision before
acting, and measures the effect against a sealed counterfactual.

## Verify it yourself — the first 30 seconds

The evidence ledger is committed. You do not have to trust the demo video, and
you do not have to set up a Python environment:

```bash
git clone https://github.com/Eeshan842004/Praman && cd Praman
./scripts/verify.sh                # scripts\verify.ps1 on Windows
```

```
+ 2988 entries . chain intact (2988 entries, head a2ff7d7d...)
+ 1200/1200 decisions reproduced against 2 pinned bundle(s)
    bundle 4ca4787c0a1eea75 : entries 1-988     (400 decisions)
    bundle bd45b0c7e5ce66a3 : entries 991-2986  (800 decisions)
+ 0 policy violations across 588 actuations
ATTESTATION PASS
```

That is two independent checks. The **chain** recomputes
`sha256(prev_hash || canonical_bytes(row))` over every entry and proves nothing
was changed after the fact. The **replay** re-POSTs every recorded policy input
to OPA loaded with the exact bundle that authorised it, and proves the record was
*true when written* — which a hash chain structurally cannot do, because a writer
that bypasses the policy engine and records its own verdict still hashes
perfectly.

Two spans, so this audits a policy **change**, not just a policy. Then break it
on purpose:

```bash
uv run praman tamper --ledger data/ledger.db --entry 447 --set amount_paise=99999900
./scripts/verify.sh                # -> CHAIN BROKEN at entry 447, ATTESTATION FAIL
git checkout data/ledger.db        # restore
```

## What we claim, in three tiers

Hardest claim first. Every number an incumbent reports lives in the third tier,
without an interval.

| Tier | Measurement | Result |
|---|---|---|
| **Primary** — the estimator, scored against 200 worlds with sealed truth | bias vs sealed truth | **−3.4%** |
| | 95% CI coverage | **95.5%** (nominal 95%) |
| | naive gross-recovery bias | **+58.7%** ← what incumbents report |
| | naive interval coverage | **8.5%** (it ships no interval) |
| **Secondary** — one adequately powered batch (n=5,000) | incremental per decline | **₹39.03**, 95% CI [₹20.00, ₹56.71] |
| | excludes zero / covers sealed truth ₹37.14 | **yes / yes** |
| **Illustrative** — the underpowered batch (n=3,000), kept on purpose | 95% CI vs truth ₹38.29 | [−₹40.82, ₹60.63] — **covers it** |
| | naive gross recovery | **₹89.82**, outside our interval, +135% wrong |

We do not claim recovered revenue. We claim the **estimator recovers the truth**,
and Primary is the evidence. Coverage is a hard test: it fails if the interval is
too narrow *or* too wide.

The third tier is deliberate. Our interval there is embarrassingly wide and it
contains the answer; the naive estimate is precise, ships no interval, and misses.
And we knew that batch was underpowered *before* running it — the minimum
detectable effect at n=3,000 is ₹41.09 against a true effect of ₹36.20, so the
null was arithmetic, not evidence. A power curve chose n=5,000; we never touched
the effect size, and a hash test fails if anyone does.

## The gate we did not pass

We trained LightGBM. Held-out macro AUC **0.9566**, well past the 0.70 gate.
**We are not shipping it**, and this is the result we would most want read.

Measured on the same 5,000 declines with only the attribution swapped, the model
trips the Rego confidence floor 3.6 points *less* often — so it acts 114 more
times — while its information capture *falls* (ICR 0.9029 vs the heuristic's
0.9608). It is sharper, not righter: buying actions with confidence rather than
accuracy, which is a legal action on a false premise arriving through the exact
number the kernel reads. The extra recovery is ₹2.96 per decline, 95% CI
[−₹0.19, +₹6.56] — it straddles zero, so quoting "worth ₹14,800" off it would be
the naive move we spend this whole project arguing against.

An audit of the ICR ceiling confirmed the comparison was fair: the denominator
already conditions on the full information set, features carry only 3.89% of the
total available information, and the heuristic — which is the *exact* Bayes
posterior for its inputs — reaches 99.4% of its own information-theoretic
maximum. Full reasoning in [`docs/GATE_LOG.md`](docs/GATE_LOG.md).

## Architecture

```
Razorpay ──webhook──> INGEST      verify HMAC, redact, dedupe, ack.  p99 1.46 ms
                        │         nothing thinks here (S2)
                        v
                     NORMALISE    decline code -> likelihood vector over 9 causes
                        │         code 05 is genuinely ambiguous; that is the product
                        v
                     ATTRIBUTE    prior x likelihood -> posterior. Proposes only.
                        │
                        v
                     POLICY       OPA/Rego, versioned bundle, deny by default.
                        │         Evaluates EVERY tier, records the full deny-set.
                        v
                     LEDGER       hash-chained, append-only. Written BEFORE acting.
                        │
                        v
                     ACTUATE      T0 terminate · T1 retry · T2 rail switch
                                  T3 nudge · T4 human escalate
                        │
                        v
                     MEASURE      cluster-randomised at the customer, CUPED,
                                  bootstrap CI, validated against sealed truth
```

Separation of powers, and the three laws that matter most:

1. **The LLM never authorises money.** It parses and it explains. Prose naming a
   cause or tier other than the recorded one is rejected.
2. **OPA is the sole authority.** Deny by default; `allow if count(deny_reason) == 0`.
3. **Nothing is actuated before it is recorded.** The ledger write precedes the
   side effect.

All eleven laws are in [`CLAUDE.md`](CLAUDE.md); the topology is in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Running it

```bash
cp .env.example .env               # Razorpay test keys, optional Gemini key
.\scripts\bootstrap.ps1            # fetches opa.exe + cloudflared.exe
uv sync
.\scripts\dev.ps1                  # OPA sidecar on :8181, FastAPI on :8000
uv run uvicorn praman.api.app:app  # dashboard at http://127.0.0.1:8000/
```

| Command | What it does |
|---|---|
| `praman verify` | chain + replay attestation |
| `praman run-batch --n 5000` | the full pipeline over a batch of declines |
| `praman report --powered-n 5000` | the three-tier result |
| `praman power` | MDE-vs-n curve; picks the batch size |
| `praman ablation` | Gate 1: heuristic vs ML on the same batch |
| `praman icr-audit` | where the information about the cause lives |
| `praman explain --seq 11` | plain-English account of one recorded decision |
| `praman tamper` | corrupt one entry, to prove `verify` is not decorative |

Every figure in this README has a command that regenerates it, listed with its
runtime in [`docs/REPRODUCE.md`](docs/REPRODUCE.md).

## Honesty

[`LIMITATIONS.md`](LIMITATIONS.md) states what this does **not** prove — labels
are synthetic by construction, no money moves, BIN velocity is unenforceable from
webhook data, and the one that matters most: **OPA bounds legality, not
correctness.** A confidently wrong cause produces a legal action on a false
premise. That is mitigated by a confidence floor and a per-payment ceiling, not
eliminated.

[`docs/WHAT_BROKE.md`](docs/WHAT_BROKE.md) is the full defect register, including
the three defects that meant the central claim of this submission was decorative
until it was audited, and the bug that reversed the shipping decision on the ML
model.

## Status

| Phase | Deliverable | State |
|---|---|---|
| 0 | Foundations, policy kernel, toolchain | done |
| 1 | Canonical taxonomy + webhook ingest | done |
| 2 | Ledger + replay attestation | done |
| 3 | Causal simulator with sealed potential outcomes | done |
| 4 | Attribution + Information Capture Ratio | decided — heuristic ships |
| 5 | Policy bundle attestation | done |
| 6 | Orchestrator + escalation ladder | done |
| 7 | Estimator validation harness | done |
| 8 | Dashboard + explanations | done |

400 tests · 28/28 policy tests at 100% coverage · policy kernel frozen at 14 rules.

## Licence

MIT
