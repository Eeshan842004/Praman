# Gate log

> Decisions taken by the number, recorded when they were taken. A gate that is
> only written up when it passes is not a gate.

---

## Gate 1 — does a trained attribution model earn its place?

**Date:** 2026-08-29
**Decision:** **Ship heuristic attribution.** The LightGBM model is built,
tested, and not shipped.

### Reproduce

```bash
.\scripts\dev.ps1                 # OPA sidecar with the pinned bundle
uv run praman ablation --train-n 12000 --batch-n 5000 --seed 77
```

### The numbers

```
                              heuristic           ML        delta
terminating at T0                  0.9%         0.5%        -0.4pp
blocked by conf. floor             8.4%         4.8%        -3.6pp
actuations executed               2,442        2,556         +114
incremental per decline          Rs 44.93     Rs 47.89     +Rs 2.96
information capture (ICR)        0.9608       0.9029       -0.0579

held-out macro AUC ......  0.9566   (gate 0.70: PASS)
expected calibration err.  0.0355
multiclass Brier ........  0.2298
temperature .............  2.060    (the raw model was 2x overconfident)

recovery delta (paired) .  Rs 2.96   95% CI [Rs -0.19, Rs 6.56]
separates from zero .....  NO
```

### Why the heuristic ships anyway

The blueprint's gate is on AUC, and **the AUC gate passes** — 0.9566 against a
floor of 0.70. It was not the binding constraint, and a gate that only ever
tests the thing that passes is decoration.

The binding standard is the one the model is supposed to be justified by:
*"it moved X% of declines from terminate to legal action, worth Rs Y"* — not
"AUC went up". Measured that way, three things fail.

**1. It captures less information than the baseline it would replace.**
ICR 0.9029 vs 0.9608. This is less surprising than it looks: the taxonomy
heuristic is not a rule of thumb, it is the *exact* Bayes posterior given
(symbol, side signals). A learned model has to beat an analytically correct
baseline using extra features, and with this much data it does not.

**2. The recovery difference does not separate from zero.** +Rs 2.96 per
decline, 95% CI [-0.19, 6.56]. The runs are paired — same batch, same seeds,
same bundle, same arm assignment — so this interval is already much tighter than
either run's own. It still contains zero. Reporting "worth Rs 14,800 across the
batch" off that point estimate would be exactly the naive-gross-recovery move
this whole project exists to argue against.

**3. It buys actions with confidence rather than with accuracy.** This is the
finding worth saying out loud. The model triggers the Rego confidence floor
3.6pp *less often*, so more tiers become legal and 114 more actuations execute —
while its ICR says those posteriors are *worse* predictions of the true cause.
Sharper, not righter. That is S5 — a legal action on a false premise — arriving
through the single number the kernel actually reads. The confidence floor exists
to stop precisely this, and a confidently wrong posterior walks straight through
it.

A model that adds nothing is not free. It adds a training step, a serialised
artifact, a version to audit, and a second thing that can silently go wrong on
the money path.

### What this is not

Not a claim that ML cannot help here. The generator makes the cause depend on
month position, velocity, outage windows and amount deviation, so there *is*
signal beyond the decline code. The honest reading is that LightGBM did not
extract enough of it to overcome an exact analytic baseline, at this sample size,
with untuned hyperparameters (tuning is out of scope — see `BACKLOG.md`).

The decision is reversible and cheap to revisit: `praman ablation` re-runs it,
and the verdict is computed rather than asserted.

### Bug found while running this gate

The first pass reported model ICR **1.038** against a Bayes ceiling of 0.998 —
an impossible result, since the ceiling is derived from the generator's own
conditional. It was a real defect, not noise.

`Taxonomy.likelihood()` looked up the emission matrix by the raw rail string.
`upi_autopay` is a mandate executed on the UPI rail and has no matrix of its own,
so the lookup missed, fell through to the flat "carries no information" vector,
and **discarded the decline code for every AutoPay decline**. Two consequences:

- the posterior collapsed to the prior, max_posterior 0.26 fell under the 0.40
  confidence floor, and the kernel refused every automated tier on those
  payments — attribution was silently disarmed for a whole rail;
- H(C|X) was inflated, shrinking the ICR denominator until a model could appear
  to extract more information than exists.

Fixed by giving the taxonomy a `rail_key()` that maps a rail to its emission
family by prefix against the loaded matrix, so it is a data change rather than a
hardcoded list. An unknown rail still yields a flat likelihood, which is the
correct answer for one. Regression tests in `tests/test_taxonomy.py`, including
one asserting the Bayes ceiling cannot be beaten.

After the fix the heuristic scored 0.9608 (up from 0.9451) and the model 0.9029.
**The pre-fix numbers had the model ahead. The bug was flattering the model, and
fixing it reversed the decision.**

---

## Gate 1a — ICR ceiling audit (re-run of Gate 1 on a challenged denominator)

**Date:** 2026-09-02
**Decision:** **Gate 1 stands. Ship heuristic attribution.** The denominator was
already correct; the audit did not reverse the verdict, and it materially
strengthened the reasoning behind it.

### The challenge

> The taxonomy posterior sees {symbol, side_signals, region}. LightGBM
> additionally sees features. The simulator conditions cause ON features. If
> H(C|X) is computed with X = symbol + side only, the denominator is too small,
> both ICRs are inflated, and the comparison is unfair to the model that uses
> features.

This is exactly the shape of defect D10, and it deserved the same treatment:
computed, not argued.

### Reproduce

```bash
uv run praman icr-audit --n 20000 --seed 101
```

### Result — the denominator already uses the full X

Measured on the **audit batch, n=20,000, seed=101**:

```
Entropy of the cause, conditioned on progressively more (bits):
  H(C) ................................   2.7498
  H(C | features) .....................   2.4176
  H(C | symbol, side, region) .........   0.6280
  H(C | symbol, side, region, features)   0.5421   <- what ships

Information available about the cause (bits):
  from features alone .................   0.3322
  from symbol + side alone ............   2.1218
  from everything .....................   2.2077
  features add beyond symbol+side .....   0.0859   (3.89% of the total)

features-blind ceiling on THIS batch    0.9611
  = (2.7498 - 0.6280) / (2.7498 - 0.5421)
```

`information_report` derives H(C|X) from `bayes_posterior`, which multiplies the
generator's own `cause_probs` — that is P(cause | features) — by the symbol and
side-signal likelihood. So the shipped denominator is **0.5421 bits**, which is
H(C | symbol, side, region, features). It is *not* 0.6280. `tests/test_entropy.py`
asserts both the match and the non-match, so this cannot silently regress.

**Both ICRs were already scored against the full ceiling. No recomputation was
required and the gate did not move.** If anything the framing was generous to the
model: it is scored against a denominator that includes information the heuristic
structurally cannot reach.

### What the audit did change: the heuristic was being judged against the wrong bar

A predictor that never reads the features cannot reach ICR 1.0 however good it
is, because a few percent of the available information is in features it does
not see. Its maximum is I(C; symbol, side) / I(C; everything) — the
**features-blind ceiling**, now computed and printed by `praman ablation`.

**Every ceiling-derived figure must name its batch.** H(C|X) is an average over
the rows in hand, so a different sample gives a slightly different conditional
and therefore a slightly different ceiling. Both of the following are correct;
mixing them is not:

| Batch | H(C\|sym,side,region) | Ceiling | Heuristic ICR | Ratio |
|---|---|---|---|---|
| audit, n=20,000 seed=101 | 0.6280 | 0.9611 | 0.9653 | 100.4% |
| **ablation, n=5,000 seed=77** | 0.6575 | **0.9667** | **0.9608** | **99.4%** |

The Gate 1 comparison is made on the ablation batch, so that is the row the
verdict quotes:

```
features-blind ICR ceiling .  0.9667   (heuristic reaches 99.4% of it)
information only ML can see.  3.33% of the total  (0.0721 bits)
```

So the heuristic's 0.9608 is not a 4-point shortfall against 1.0. It is **99.4%
of its own information-theoretic maximum on that batch**. It is not a rule of
thumb that happens to work; it is the exact Bayes posterior for its information
set, and it is essentially saturating it.

(The audit batch's 100.4% is above 100% by finite-sample noise — the heuristic's
empirical cross-entropy on those particular rows lands a hair below its own
conditional entropy. It is not evidence of anything, and it is the reason the
verdict quotes a single batch rather than whichever number reads best.)

`tests/test_entropy.py` now asserts that the ceiling equals the entropy-derived
ratio exactly, and that two batches give two ceilings — so this cannot silently
drift back into a mismatch.

### Why a model with strictly more information scored lower

Asked directly, and it survives as a **real finding, not an artifact**:

1. **The extra information is small.** Features add 0.0721 bits (3.33%) on the
   ablation batch and 0.0859 bits (3.89%) on the audit batch. Either way, a few
   percent of the total is the entire prize.
2. **The heuristic does not estimate the expensive part, it knows it.** The
   symbol-to-cause mapping is 96% of the available information, and the heuristic
   has it exactly — it is the same emission matrix the generator sampled from.
   LightGBM must *learn* that mapping from ~6,600 training rows across 9 classes.
3. **Estimation error on the 96% exceeds the 4% gain.** The model spends variance
   re-learning what the baseline already knows, to chase a few hundredths of a
   bit. Net ICR −0.0579.

This is ordinary bias–variance, stated in bits: a correctly-specified analytic
model beats a flexible one when the extra flexibility buys little and costs
estimation variance. It is not evidence that ML cannot help on this problem.

**Where it would flip.** The result is a property of this generator's emission
matrix being sharp. Make the decline codes more ambiguous — which is what real
`05` traffic looks like — and the symbol's share falls, the features' share
rises, and the model's opportunity grows. On real traffic with a genuinely
ambiguous code distribution this gate could legitimately go the other way, and
the honest claim is about *this* simulator, not about payments in general.

### Verdict, unchanged

```
held-out macro AUC .........  0.9566   (gate 0.70: PASS)
features-blind ceiling .....  0.9667   heuristic reaches 99.4%
ICR heuristic / ML .........  0.9608 / 0.9029   delta -0.0579
recovery delta (paired) ....  Rs 2.96  95% CI [-0.19, 6.56]  straddles zero
VERDICT ....................  ship the HEURISTIC
```

The superseded Gate 1 entry above is retained deliberately. It records the
verdict before this audit and before defect D10, and a gate log that only keeps
its final answer is not a log.
