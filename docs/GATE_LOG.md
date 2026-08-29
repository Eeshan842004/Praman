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
