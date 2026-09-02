# Limitations

> Stated before anyone has to ask. Every claim Praman makes has a boundary, and
> naming the boundary is part of the claim.

This file is a scoring asset, not an apology. A system that tells you where it
stops being trustworthy is more trustworthy than one that does not.

---

## 1. No public dataset contains real ISO 8583 DE-39 decline codes

This is a confirmed gap, not a shortcut. IEEE-CIS carries only `isFraud`. PaySim
and Sparkov carry none. There exists no public corpus of `(observed_code, true_cause)`
pairs anywhere.

**Consequence:** all decline-code modelling is **synthetic by construction**.
**Mitigation:** transaction *features* are resampled from real IEEE-CIS rows — only
the causal label layer is synthesised. Eight genuinely labelled Razorpay test-mode
failures anchor the pipeline to real API traffic.
**What this does not license:** any claim about real-world accuracy.

## 2. We never claim model accuracy

Reporting accuracy against labels we authored would be circular — the number would
measure our generator, not the world.

Instead we report the **Information Capture Ratio**: because we authored the
generative model, the Bayes-optimal posterior is exactly computable, so the
*achievable* information is exactly computable too. We report the fraction of it
the model actually extracts, in bits.

**What this proves:** the pipeline and its calibration.
**What it does not prove:** that these numbers transfer to production traffic.

## 3. We never claim recovered revenue

The industry reports gross recovery. Gross recovery silently includes every
customer who would have paid anyway.

We do not report a rupee figure as an achievement. We validate the **estimator**
against 200 simulated worlds with sealed, known ground truth, and report bias,
RMSE, and 95% CI coverage — alongside the same statistics for the naive
gross-recovery estimator the industry uses.

**What this proves:** our uncertainty quantification is real.
**What it does not prove:** that we recovered money for anyone.

## 4. Hash chaining is tamper-EVIDENT, not tamper-PREVENTING

The ledger detects modification. It does not stop a sufficiently privileged
actor from attempting one.

**And a chain alone proves less than it appears to.** It shows nothing was
changed *after* the fact; it cannot show the record was *true when written*. A
writer that bypassed OPA and recorded its own verdict appends through the normal
path and hashes perfectly. That is why every decision stores the exact policy
input it was judged on, and why `praman verify` re-POSTs each one to OPA loaded
with that entry's pinned bundle and compares the verdict. The two checks are
independent and both are required; either alone is a weaker claim than it
sounds.

`praman tamper` must explicitly `DROP TRIGGER` on the append-only guard before it
can mutate a row — a privileged and visible act — and the chain still catches it.
That is the correct security property to claim, and the only one we claim.

**Production hardening (out of scope here):** periodic Merkle root anchoring to an
external append-only store.

## 5. All actuation is simulated. No money moves.

Recovery actions execute against the simulator's counterfactual outcome model, not
against live rails. The Razorpay integration is test mode only.

**Why:** live actuation on a hackathon timeline is an unbounded liability with no
demonstrative benefit. The policy kernel, the ledger, and the estimator are
identical either way.

## 6. OPA bounds LEGALITY, not CORRECTNESS

**This is the most important limitation in the document.**

The policy kernel guarantees that no action is taken which violates a network,
regulatory, or internal rule. It cannot guarantee the action was the *right* one.

A confidently-wrong cause produces a **legal action on a false premise**: if the
classifier miscalls an `INSTRUMENT_DISABLED` payment as `TECHNICAL_DECLINE`, the
policy correctly permits a retry, three attempts burn, and the customer never
receives the nudge that would actually have fixed it.

**Mitigated by, not eliminated by:**
- a confidence floor encoded in Rego (`low_confidence` deny reason), so a guess
  cannot reach an automated tier;
- expected-cost routing over a committed loss matrix rather than `argmax`;
- a per-payment attempt ceiling that holds regardless of what the model believes;
- a batch circuit breaker that halts on anomalous tier distribution.

Calibration is therefore a **functional** requirement, not a presentational one.
We report Brier score and Expected Calibration Error, not just AUC.

## 7. Conflicting public sources on Mastercard retry caps

Vendor sources disagree (10 vs 35 attempts per 30 days). We default to the
conservative figure and expose it as configuration rather than asserting a number
we cannot verify against the rulebook.

The same applies to the Visa cap, which moved from 15 to 20 on 25 May 2025 for
Categories 2/3/4. Every threshold lives in `policy/config/data.json`, is covered by
the bundle revision, and is therefore auditable and changeable without a code
change.

## 8. Synthetic generators degrade behavioural signal

Published work (arXiv, *Synthetic Tabular Generators Fail to Preserve Behavioral
Fraud Patterns*) shows generators break temporal, velocity, and multi-account
signals.

**Mitigation:** we do not synthesise features. We resample real feature rows and
synthesise only the causal label layer on top, then validate the result with KS
tests on continuous features and chi-square on categoricals.

## 9. Statistical design assumptions

- Randomisation is **cluster-randomised at customer level**, because subscription
  declines repeat per customer and payment-level randomisation would violate SUTVA
  (treatment leaking into holdout via a shared customer).
- Confidence intervals come from a **customer-level cluster bootstrap**. A
  payment-level bootstrap would understate variance whenever cluster sizes vary.
- The CUPED covariate is strictly **pre-treatment**, asserted in code.
- Reported coverage is measured, not assumed. If it falls outside [0.92, 0.97] the
  interval is wrong and the finding is withdrawn.
- **Batch size is chosen by a power calculation, not by convenience.** The minimum
  detectable effect at 80% power is computed *before* a headline batch runs, from
  the estimator's own measured standard error rather than an assumed ICC. At
  n=3,000 the MDE is ₹41.09 against a true ITT effect of ₹36.20 — that batch
  cannot resolve its own effect, and we say so rather than presenting the
  resulting null as a finding. It is still shown, deliberately, beside the
  powered run.
- **The 1/√n law describes this estimator only asymptotically.** The outcome is
  amount × Bernoulli over a lognormal amount, so the 20% holdout mean is driven
  by a handful of large recoveries and its variance is itself heavy-tailed. A
  fitted 1/√n curve missed the measured MDE at n=3,000 by 31% even at 8–12 runs
  per grid point. We therefore choose n from the measured grid and report the fit
  only as a cross-check, taking whichever rule is more conservative.
- **The effect size is fixed and hash-pinned.** Powering an experiment means
  choosing n, never choosing τ. `tests/test_power.py` fails if the simulator's
  recovery rates change, so tuning the generator until the interval excludes zero
  cannot happen quietly.

## 10. BIN velocity cannot be enforced from webhook data alone

`payment.failed` exposes the card's `last4`, network and issuer — **not the
BIN**. So `bin_attempts_1h` cannot be computed from a Razorpay payload, and the
worker passes 0.

This one deserves naming separately from the other missing fields, because it
behaves differently. `afa_completed` and `ms_since_pre_debit_notice` are also
absent, and both default to the value that **denies** — a high-value e-mandate
cannot be retried on data we never received. But 0 does not deny a velocity cap.
It satisfies it.

So this is an enforcement **gap**, not a conservative default, and it is stated
here rather than hidden behind a plausible-looking zero. Closing it requires the
merchant's own attempt log keyed by BIN, which the simulator has and the live
webhook path does not. In the batch pipeline the counter is computed properly
and the rule is enforced.

## 11. Live Razorpay payloads contradict their own documented shape

`fixtures/razorpay/` holds nine real test-mode failures pulled off the REST API
with the account's own keys, redacted through the same chokepoint the webhook
path uses. Capturing them found three places where the live payload differs from
what the published schema implies, all now asserted in `tests/test_fixtures.py`:

| Field | Documented / assumed | Live |
|---|---|---|
| `notes` | object `{}` | **empty list `[]`** when unset |
| `acquirer_data.rrn` | present | **absent** — a failed authorisation never reaches settlement, so there is no retrieval reference number |
| `card.name` | not mentioned | **present — the cardholder's name** |

The third is the one that mattered. `redact()` keeps an **allowlist** of card
fields, so `card.name` was dropped without anyone having to know it existed. A
denylist — "strip the fields we know are sensitive" — would have committed a
real person's name to a public repository, and no test built from our own
hand-written payload could have caught it, because our payload never had the
field. This is the concrete argument for allowlists over denylists at a
redaction boundary, and it was found by capturing real traffic rather than by
reasoning about it.

`notes: []` is harmless here only because `redact()` drops the field outright.
Any code that had assumed a mapping would have failed on live traffic while
passing every test.

**What this does license:** the statement that the ingest layer is verified
against live traffic, not only against the documented schema.
**What it does not:** any claim about production-scale traffic. Nine failures
from one test account is an integration check, not a sample.

## 12. Scope boundaries

Not built, deliberately: authentication, multi-tenancy, RBAC, real-time streaming,
live multi-processor integrations, mobile, or email/SMS delivery. Portability
across rails is demonstrated by the canonical taxonomy, not by N live integrations.
