# Praman — 5-Minute Demo Script

> **This document is the scope contract.**
> If a feature does not appear in a beat below, it does not get built. Every new
> idea goes to `BACKLOG.md`.
>
> Hard limit: **5:00**. Overrun is cut from Beat 6 first, then Beat 1.

> **What changed, and why.** This script used to lead with a rupee figure. It no
> longer does — not because the figure was wrong, but because a rupee figure is
> not a claim this system can support, and leading with one puts us in exactly
> the company we are criticising. The headline is now the *estimator*, and the
> money numbers appear underneath it with their intervals attached.

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
- The top features behind this decision

**Say:**
> "This is a real Razorpay test-mode decline, over a real webhook, HMAC-verified.
> The normaliser doesn't map this code to one cause — it emits a likelihood vector,
> because code 05 genuinely *is* ambiguous. The model produces the posterior. That
> ambiguity is the product.
>
> Nothing in this request path thinks. No model, no OPA, no LLM. We verify the
> signature, deduplicate, acknowledge, and *then* decide — because a slow webhook
> becomes a duplicate delivery, becomes an inflated attempt counter, becomes a
> network fine."

---

## Beat 3 — The gate (1:15 – 2:15)

**Show the S3 regulatory deadlock case.** This is the highest-signal beat.

```
pay_TESTdeadlock01  ₹22,000  UPI e-mandate  cause=INSUFFICIENT_FUNDS (p=0.79)
  T1 silent_retry   DENY   [npci_autopay_blackout_window, rbi_afa_required,
                            rbi_pre_debit_notice_not_elapsed]
  T2 rail_switch    DENY   [no_alternate_instrument, npci_autopay_blackout_window,
                            rbi_afa_required, rbi_pre_debit_notice_not_elapsed]
  T3 customer_nudge DENY   [npci_autopay_blackout_window, nudge_fatigue_7d,
                            rbi_afa_required, rbi_pre_debit_notice_not_elapsed]
  T4 human_escalate ALLOW  []                    → merchant ops queue
  bundle_revision: bd45b0c7e5ce66a3
```

**Say:**
> "One payment. Four different regulators independently forbid an action — RBI's AFA
> rule, RBI's 24-hour pre-debit notice, NPCI's AutoPay blackout window, and our own
> contact-fatigue cap. We evaluate every tier, we never short-circuit, and we record
> every deny reason. No incumbent shows a merchant *why* they can't act.
>
> And look at T4. Escalating to the merchant's own ops queue is not a debit and not
> a customer contact, so none of those rules has jurisdiction over it. That matters:
> if every tier could deny, this payment would have no legal terminal state at all —
> the system would be stuck. 'We can't even tell a human' is not a state a payments
> system is allowed to reach."

**Then:** `opa test policy/ -v --coverage --threshold 90` → **28/28, coverage 100%**.

> "The model proposes. The policy decides. The LLM never touches the money path.
> And if OPA is unreachable, we fail closed — deny, escalate to a human."

---

## Beat 4 — What we actually claim (2:15 – 3:15) ★ THE DIFFERENTIATOR

**Show:** `praman report --powered-n 5000`

The result is deliberately in three tiers, hardest claim first.

```
==========================================================================
PRIMARY . the estimator, not the outcome
==========================================================================
  200 worlds from the payments simulator . sealed true effect
  80/20 cluster-randomised at the customer

  Praman (CUPED + customer-level cluster bootstrap)
    bias vs sealed truth ......  -3.4%
    95% CI coverage ...........  95.5%   (nominal 95%)
    CUPED variance reduction ..  38%

  Industry-standard gross recovery (no holdout)
    bias vs sealed truth ......  +58.7%   <- what incumbents report
    interval coverage .........  8.5%     (it ships no interval)
==========================================================================
```

**Say:**
> "I can't validate against reality — no public dataset has real decline codes. So I
> don't. I validate the *estimator* instead. Two hundred simulated worlds, each with
> a sealed true effect the pipeline cannot read.
>
> Coverage is the number to watch, and it's a genuinely hard test: it fails if your
> interval is too narrow *or* too wide. Ours is nominal. The naive gross-recovery
> estimator the entire industry reports is fifty-nine percent biased and
> ships no interval at all.
>
> That's the thesis. Not 'we recovered X'. 'Here is how wrong X is when you measure
> it the way everyone measures it.'"

**Then the two lower tiers, briefly:**

```
SECONDARY    . one powered batch (n=5,000)
  incremental per decline ...  Rs 34.79
  95% CI ....................  [Rs 15.91, Rs 52.71]
  excludes zero .............  YES
  sealed truth ..............  Rs 33.64   covered: YES

ILLUSTRATIVE . the underpowered batch (n=3,000), kept on purpose
  95% CI ....................  [Rs -44.54, Rs 57.80]
  sealed truth ..............  Rs 35.98   covered: YES
  naive gross recovery ......  Rs 86.95   inside our interval: NO
```

**Say:**
> "Two batches. The second one is underpowered and I'm showing it anyway.
>
> I know it's underpowered because I computed the minimum detectable effect *before*
> running it — at n=3,000 that's Rs 41.04, and the true effect is
> Rs 33.79. The effect is smaller than the smallest thing that batch could
> resolve. So a null result there is arithmetic. It isn't evidence of anything, and
> I'd have been wrong to present it as either a success or a failure.
>
> The power curve chose 5,000 — I didn't. And notice what I did *not* do: I never
> touched the effect size. Turning up the recovery rates until the experiment
> succeeds would be tuning the generator to flatter the product.
>
> Look at the underpowered row. Our interval is embarrassingly wide, and it contains
> the truth. The naive estimate is a confident point — and it's outside our interval
> entirely. Honest and wide beats confident and wrong."

**Critical framing:** never claim recovered revenue. Claim estimator quality.

---

## Beat 5 — The proof (3:15 – 4:00)

```
$ praman verify --ledger data/ledger.db
+ 22631 entries . chain intact (22631 entries, head 1987c167...)
+ 9200/9200 decisions reproduced against 2 pinned bundle(s)
    bundle 4ca4787c0a1eea75 : entries 1-2964      (1200 decisions)
    bundle bd45b0c7e5ce66a3 : entries 2967-22630  (8000 decisions)
+ 0 policy violations across 4231 actuations . arms: {'holdout': 1834, 'treatment': 7366}
ATTESTATION PASS

$ praman tamper --ledger data/ledger.db --entry 447 --set amount_paise=99999900
  ! dropping append-only trigger to modify entry 447 (privileged act)
$ praman verify --ledger data/ledger.db
x CHAIN BROKEN at entry 447 (expected bd3c89c4... got 1d45544f...) -> 22184 subsequent entries invalidated
  entry 447 of 22631
ATTESTATION FAIL
```

**Say:**
> "Two independent checks, and the difference between them is the whole point.
>
> The hash chain proves nothing was changed after the fact. It cannot prove the
> record was ever *true* — someone who holds the append path can bypass the policy
> engine, write their own verdict, and the chain is still perfect.
>
> So verify does a second thing. Every decision stores the exact input OPA judged,
> and verify re-POSTs each one to OPA loaded with the committed bundle that
> authorised it, then compares the verdict. That's not a hash check. That's
> re-deriving nine thousand decisions from the policy itself.
>
> And look at the two spans. This ledger crosses a policy change — the first
> twelve hundred decisions were authorised by a bundle where human escalation
> could be denied, the rest by the one where it can't. Each span replays against
> *its own* bundle. So this doesn't just audit a policy. It audits a policy
> *change*, which is the thing that actually happens in production.
>
> To tamper I had to drop the append-only trigger — a privileged, visible act —
> and the chain caught me anyway.
>
> Honest limit: this is tamper-*evident*, not tamper-*preventing*. It detects. It
> does not stop a root user."

---

## Beat 6 — Architecture (4:00 – 4:30)

**On screen:** the six-layer topology.

**Say:**
> "Ingest acks in under 20 milliseconds and thinks afterwards. Counters increment on
> actuation, never on ingest — a decision the policy refused is not an attempt.
> Inference proposes. OPA disposes. The ledger records before anything actuates."

---

## Beat 7 — Honest limitations (4:30 – 5:00)

**On screen:** `LIMITATIONS.md`

**Say:**
> "What this does not prove. Labels are synthetic — I resample real transaction
> features and synthesise only the causal layer on top. No money moves. And the one
> that matters most: OPA bounds *legality*, not *correctness*. A confidently wrong
> cause produces a legal action on a false premise. I mitigate that with a confidence
> floor and a per-payment ceiling. I don't eliminate it.
>
> Praman's whole idea is one sentence: we can't validate against reality, so we
> validate our estimators against worlds where we know the truth — and we report
> exactly how much of the knowable we recover."

---

## Recording checklist

- [ ] Record **locally** via `cloudflared` tunnel. Never depend on a cold start.
- [ ] `praman power` takes ~20 minutes — run it beforehand and demo `--load
      docs/power_curve.json`, which renders the saved measurement instantly.
- [ ] Pre-generate `data/ledger.db` before rolling; `verify` replays every decision
      through OPA and that takes time proportional to the batch.
- [ ] Terminal font ≥ 16pt. Dark theme. No personal info on screen.
- [ ] Rotate/revoke any key visible in any frame.
- [ ] Record twice. Keep the second take.
- [ ] Hard-check runtime ≤ 5:00 before upload.

## Cut order if over time

1. Beat 6 (architecture) — the repo's `ARCHITECTURE.md` carries it
2. Beat 1 (problem setup) — compress to 15s
3. **Never cut Beat 4 or Beat 5.**
