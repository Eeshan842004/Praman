# Praman — 5-Minute Demo Script

> **This document is the scope contract.**
> Written Day 0, before any code. If a feature does not appear in a beat below,
> it does not get built. Every new idea goes to `BACKLOG.md`.
>
> Hard limit: **5:00**. Overrun is cut from Beat 6 first, then Beat 1.

---

## Beat 1 — The problem (0:00 – 0:30)

**Say:**
> "Roughly half of all 'Do Not Honor' declines are insufficient funds in disguise.
> The issuer knows the balance, the risk score, the velocity. The merchant sees two
> digits. And here's the part nobody talks about: no recovery vendor on earth —
> Stripe, Adyen, or Razorpay's own Optimizer — can tell you how much revenue they
> actually *caused*. They all report gross recovery, which silently includes every
> customer who would have paid anyway."

**On screen:** decline code `05`, then a fan-out to the five causes it could mean.

---

## Beat 2 — Live ingest, real Razorpay traffic (0:30 – 1:15)

**Do:** Fire a real Razorpay test-mode failure. Webhook lands.

**Show:**
- Terminal: `200 OK` in under 20 ms (ack-before-think)
- Dashboard: observed tuple → likelihood vector → **calibrated posterior over 9 causes**
- SHAP top-3 features for this decision

**Say:**
> "This is a real Razorpay test-mode decline, over a real webhook, HMAC-verified.
> The normaliser doesn't map this code to one cause — it emits a likelihood vector,
> because code 05 genuinely *is* ambiguous. The model produces the posterior. That
> ambiguity is the product."

---

## Beat 3 — The gate (1:15 – 2:15)

**Show the S3 regulatory deadlock case.** This is the highest-signal beat.

```
pay_TESTdeadlock01  ₹22,000  UPI e-mandate  cause=INSUFFICIENT_FUNDS (p=0.79)
  T1 silent_retry   DENY  [rbi_afa_required, npci_autopay_blackout_window]
  T2 rail_switch    DENY  [no_alternate_instrument]
  T3 customer_nudge DENY  [nudge_fatigue_7d, rbi_pre_debit_notice_not_elapsed]
  T4 human_escalate ALLOW → merchant ops queue
  bundle_revision: 4e9c1a...  decision_id: 0f2b...
```

**Say:**
> "One payment. Four different regulators independently forbid an action — RBI's AFA
> rule, RBI's 24-hour pre-debit notice, NPCI's AutoPay blackout window, and our own
> contact-fatigue cap. We evaluate every tier, we never short-circuit, and we record
> every deny reason. No incumbent shows a merchant *why* they can't act."

**Then:** `opa test policy/ -v --coverage` → green, ≥90%.

> "The model proposes. The policy decides. The LLM never touches the money path.
> And if OPA is unreachable, we fail closed — deny, escalate to human."

---

## Beat 4 — Estimator validation (2:15 – 3:15) ★ THE DIFFERENTIATOR

**Show:** `praman validate-estimator`

```
ESTIMATOR VALIDATION · 200 simulated worlds · N=1,000 · 90/10 cluster-randomised
──────────────────────────────────────────────────────────────────────────────
Praman estimator (CUPED + customer-level cluster bootstrap)
  mean bias vs true ATE ..............  +1.2%
  RMSE ...............................  ₹14,200
  95% CI coverage ....................  94.5%   (nominal 95%)
  CUPED variance reduction ...........  41%

Industry-standard gross-recovery estimator (no holdout)
  mean bias vs true ATE ..............  +88.1%   ← what incumbents report
  95% CI coverage ....................   0.0%
──────────────────────────────────────────────────────────────────────────────
```

**Say:**
> "I can't validate against reality — no public dataset has real decline codes. So I
> don't. I validate the *estimator* instead. Two hundred simulated worlds, each with
> a sealed true effect the pipeline cannot read. Our estimator lands within 1.2% with
> 94.5% CI coverage — that coverage number is a genuinely hard test to pass. The naive
> gross-recovery estimator that the entire industry reports? Eighty-eight percent
> biased, zero percent coverage.
>
> That's the thesis. Not 'we recovered X'. 'Here is how wrong X is when you measure it
> the way everyone measures it.'"

**Critical framing:** never claim recovered revenue. Claim estimator quality.

---

## Beat 5 — The proof (3:15 – 4:00)

```
$ praman verify --ledger data/ledger.db
✓ 1,000 entries · chain intact
✓ 1,000/1,000 decisions reproduced across 2 pinned bundles
    bundle 4e9c1a… : entries 1–612    (visa_cap=15)
    bundle 8b31f7… : entries 613–1000 (visa_cap=15, +nudge_fatigue rule)
✓ 0 policy violations · 0 Cat-1 retries · 0 MAC-03 retries
ATTESTATION PASS

$ praman tamper --entry 447 --set amount_paise=99999900
  ⚠ dropping append-only trigger (privileged act, logged)
$ praman verify --ledger data/ledger.db
✗ CHAIN BROKEN at entry 447 → 553 subsequent entries invalidated
ATTESTATION FAIL
```

**Say:**
> "Every decision replays against the exact policy bundle OPA reported at the time —
> and the ledger spans two revisions, so this proves I can audit a policy *change*,
> not just a policy. To tamper, I had to drop the append-only trigger — a privileged,
> visible act — and the chain caught me anyway.
>
> Honest limit: this is tamper-*evident*, not tamper-*preventing*. It detects. It does
> not stop a root user."

---

## Beat 6 — Architecture (4:00 – 4:30)

**On screen:** the six-layer topology.

**Say:**
> "Ingest acks in under 20 milliseconds and thinks afterwards — because a slow webhook
> becomes a duplicate delivery, becomes an inflated attempt counter, becomes a Visa
> excessive-reattempt fee. Counters increment on actuation, never on ingest. Inference
> proposes. OPA disposes. The ledger records before anything actuates."

---

## Beat 7 — Honest limitations (4:30 – 5:00)

**On screen:** `LIMITATIONS.md`

**Say:**
> "What this does not prove. Labels are synthetic — only eight real Razorpay failures
> anchor reality. No money moves. And the one that matters most: OPA bounds *legality*,
> not *correctness*. A confidently wrong cause produces a legal action on a false
> premise. I mitigate that with a confidence floor and a per-payment ceiling. I don't
> eliminate it.
>
> Praman's whole idea is one sentence: we can't validate against reality, so we
> validate our estimators against worlds where we know the truth — and we report
> exactly how much of the knowable we recover."

---

## Recording checklist

- [ ] Record **locally** via `cloudflared` tunnel. Never depend on a cold start.
- [ ] Pre-warm the Gemini archetype cache before rolling.
- [ ] Terminal font ≥ 16pt. Dark theme. No personal info on screen.
- [ ] Rotate/revoke any key visible in any frame.
- [ ] Record twice. Keep the second take.
- [ ] Hard-check runtime ≤ 5:00 before upload.

## Cut order if over time

1. Beat 6 (architecture) — the repo's `ARCHITECTURE.md` carries it
2. Beat 1 (problem setup) — compress to 15s
3. **Never cut Beat 4 or Beat 5.**
