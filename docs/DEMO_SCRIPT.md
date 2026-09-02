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
> digits. Most recovery vendors report gross recovery, which silently includes every
> customer who would have paid anyway.
>
> A few do better — Checkout.com runs control groups, Slicker runs fifty-fifty A/B
> tests with p-values. So the method is validated, not novel, and I'm not going to
> pretend otherwise. What none of them does is hand the *merchant* the
> counterfactual. Their control group is internal; you take the number on trust.
> And a p-value tells you an effect probably isn't zero — it doesn't tell you your
> confidence interval actually contains the answer. That's the gap I built for."

**On screen:** decline code `05`, then a fan-out to the five causes it could mean.

---

## Beat 2 — Live ingest, real Razorpay traffic (0:30 – 1:15)

**Do:** Fire a real Razorpay test-mode failure. Webhook lands.

**Show:**
- Terminal: `200 OK` — measured p99 **1.46 ms** over a 200-request burst
- Dashboard `/decision/{seq}`: the **posterior over all nine causes**, not an argmax
- Nine real Razorpay test-mode failures are committed in `fixtures/razorpay/`

**Say:**
> "This is a real Razorpay test-mode decline, over a real webhook, HMAC-verified.
> The normaliser doesn't map this code to one cause — it emits a likelihood vector,
> because code 05 genuinely *is* ambiguous. That ambiguity is the product.
>
> Nothing in this request path thinks. No model, no OPA, no LLM. We verify the
> signature, deduplicate, acknowledge, and *then* decide — because a slow webhook
> becomes a duplicate delivery, becomes an inflated attempt counter, becomes a
> network fine. That 20 ms budget is a compliance budget, not a performance one."

---

## Beat 2b — Gate 1: the model I built and didn't ship (1:15 – 1:35)

**Show:** `praman ablation` — the same 5,000 declines, attribution swapped.

```
                              heuristic           ML        delta
blocked by conf. floor             8.4%         4.8%        -3.6pp
actuations executed               2,442        2,556         +114
incremental per decline          Rs 44.93     Rs 47.89     +Rs 2.96
information capture (ICR)        0.9608       0.9029       -0.0579

held-out macro AUC ......  0.9566   (gate 0.70: PASS)
features-blind ceiling ..  0.9667   heuristic reaches 99.4% of it   (this batch)
recovery delta (paired) .  Rs 2.96   95% CI [Rs -0.19, Rs 6.56]   -> straddles zero
VERDICT: ship the HEURISTIC.
```

**Say:**
> "I trained LightGBM. Held-out AUC 0.9566, well past my gate of 0.70. I'm not
> shipping it, and this is the slide I'd most want you to remember.
>
> Look at the top two rows together. The model trips the confidence floor 3.6 points
> *less* often, so it acts 114 more times — while its information capture goes *down*.
> It's sharper, not righter. It's buying actions with confidence instead of accuracy,
> and that is exactly the failure the confidence floor exists to prevent: a legal
> action on a false premise, walking straight through the one number the kernel reads.
>
> And the extra recovery? Two rupees ninety-six, interval minus nineteen paise to six
> fifty-six. It straddles zero. Quoting 'worth fifteen thousand rupees' off that point
> estimate would be the exact move I'm criticising everyone else for.
>
> And before you ask whether I stacked the comparison: I audited the ceiling. The
> denominator already conditions on everything the model can see. On this batch
> features carry three point three percent of the total available information —
> that was the whole prize — and the heuristic, which is the *exact* Bayes
> posterior for its inputs, already reaches ninety-nine point four percent of its
> own theoretical maximum.
>
> A model that adds nothing isn't free — it's a training step, an artifact, a version
> to audit, and a second thing that can go wrong on the money path. The number decided,
> not me. It's in `docs/GATE_LOG.md`."

---

## Beat 3 — The gate (1:35 – 2:20)

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
  incremental per decline ...  Rs 39.03
  95% CI ....................  [Rs 20.00, Rs 56.71]
  excludes zero .............  YES
  sealed truth ..............  Rs 37.14   covered: YES

ILLUSTRATIVE . the underpowered batch (n=3,000), kept on purpose
  95% CI ....................  [Rs -40.82, Rs 60.63]
  sealed truth ..............  Rs 38.29   covered: YES
  naive gross recovery ......  Rs 89.82   inside our interval: NO
```

**Say:**
> "Two batches. The second one is underpowered and I'm showing it anyway.
>
> I know it's underpowered because I computed the minimum detectable effect *before*
> running it — at n=3,000 that's Rs 41.09, and the true effect is
> Rs 36.20. The effect is smaller than the smallest thing that batch could
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
$ ./scripts/verify.sh                       # one command, fresh clone, no setup
+ 2988 entries . chain intact (2988 entries, head a2ff7d7d...)
+ 1200/1200 decisions reproduced against 2 pinned bundle(s)
    bundle 4ca4787c0a1eea75 : decision entries 1-988     (400 decisions)
    bundle bd45b0c7e5ce66a3 : decision entries 991-2986  (800 decisions)
+ 0 policy violations across 588 actuations
ATTESTATION PASS

$ praman tamper --ledger data/ledger.db --entry 447 --set amount_paise=99999900
  ! dropping append-only trigger to modify entry 447 (privileged act)
$ ./scripts/verify.sh
x CHAIN BROKEN at entry 447 (expected 90d05627... got dc160faa...) -> 2541 subsequent entries invalidated
  entry 447 of 2988
ATTESTATION FAIL
```

**Say:**
> "This is one command on a fresh clone. It fetches the policy engine itself; the
> ledger is committed. You are not trusting this video.
>
> Two independent checks, and the difference between them is the whole point.
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
- [ ] `praman power` takes ~25 minutes — demo `praman power --load
      docs/power_curve.json`, which renders the saved measurement instantly.
- [ ] `praman prewarm` before rolling, so no beat waits on a Gemini round trip.
      1,200 decisions collapse to 50 archetypes, so it is 50 calls.
- [ ] The committed `data/ledger.db` is the one to attest on camera — 2,988
      entries, two bundles, ~15 s. Do not use the 23 MB full run.
- [ ] `git checkout data/ledger.db` after the tamper beat.
- [ ] Terminal font ≥ 16pt. Dark theme. No personal info on screen.
- [ ] Rotate/revoke any key visible in any frame.
- [ ] Record twice. Keep the second take.
- [ ] Hard-check runtime ≤ 5:00 before upload.

## Cut order if over time

1. Beat 6 (architecture) — the repo's `ARCHITECTURE.md` carries it
2. Beat 1 (problem setup) — compress to 15s
3. **Never cut Beat 4 or Beat 5.**
