# What broke

> Every defect found and fixed while building Praman, with what it would have
> cost if it had shipped. Written because the interesting part of this project
> is not that it works — it is the specific ways it did not, and how each one
> was caught.

**For the application form, in three sentences:**

> The single most important claim in my submission — "every decision replays
> against the policy that authorised it" — was false. `praman verify` printed
> `3000/3000 decisions reproduced` from a database `GROUP BY` with no policy
> engine call anywhere in the command, all 1,370 actuations had stored an empty
> policy input so there was nothing to replay even in principle, and CI never
> installed OPA so the tests covering it silently skipped while the build showed
> green. I found it by auditing my own claim instead of trusting it, built the
> replay for real, and added a `--require-replay` flag so a skipped check can
> never again print `ATTESTATION PASS`.

---

## The trilogy

Three defects, one claim. Individually each is a bug; together they meant the
central promise of the submission was decorative, and each one independently hid
the other two.

### D1 · `verify` claimed reproduction it never performed

**Severity: critical.** The line was:

```
+ 3000/3000 decisions reproduced across 1 pinned bundle(s)
```

It came from `SELECT bundle_revision, COUNT(*) … GROUP BY bundle_revision`.
Nothing was reproduced. No OPA call existed anywhere in the command. The word
"reproduced" was doing all the work and none of it was earned.

**What it would have cost:** the demo's highest-value beat, delivered to judges
as a fact. Anyone who read the source would have found a counting query dressed
as an attestation.

**How it was caught:** by being asked to confirm the claim rather than assume
it — the audit was the fix.

**Fixed in** `cb711bc`. `ledger/replay.py` now starts OPA against the committed
bundle each row is pinned to and re-POSTs every stored input through the *same*
`PolicyClient` production uses.

### D2 · All 1,370 actuations stored an empty policy input

**Severity: critical.** `evaluate_ladder` built its result object separately at
each `return`, and the success path omitted `policy_inputs`. So every T1/T2/T3
decision — *every decision that ever led to an actuation* — stored `{}`.

On the live ledger: 1,701 of 3,000 decisions empty, including **all 1,370
actuations**. The rows that authorised money movement were the only rows that
could not be re-derived.

**What it would have cost:** replay would have been impossible even after D1 was
fixed. The field existed, was hashed, and was empty exactly where it mattered.

**Fixed by** giving the function one outcome constructor, so omitting the
evidence is now unrepresentable rather than merely corrected.

### D5 · CI never installed OPA, so the replay tests silently skipped

**Severity: major.** `tests/test_replay.py` skips when the OPA binary is absent.
The CI Python job never installed it. A skipped test and a passing test are
indistinguishable in pytest's summary line, so the build showed green while the
submission's central claim was untested.

**What it would have cost:** the regression that reintroduced D1 or D2 would
have shipped, and CI would have said nothing.

**Fixed** by installing OPA in the Python job and adding `PRAMAN_REQUIRE_OPA`,
which turns the skip into a failure. Locally a missing binary should skip; in CI
it must be a red build.

---

## D10 · `upi_autopay` silently discarded the decline code

**Severity: critical. This one reversed a shipping decision.**

The emission matrix is keyed by rail and holds `card` and `upi`. AutoPay is a
mandate executed *on* the UPI rail and has no matrix of its own, so
`Taxonomy.likelihood()` looked up `upi_autopay`, missed, and fell through to the
flat "this observation carries no information" vector.

Two consequences, both silent:

1. **Attribution was disarmed for an entire rail.** The posterior collapsed to
   the prior — `max_posterior` 0.26, under the 0.40 confidence floor — so the
   kernel refused every automated tier on those payments *because their decline
   code had been thrown away*. Every component reported success.
2. **The information ceiling was wrong.** H(C|X) was inflated, shrinking the ICR
   denominator until a trained model scored **1.038** against a ceiling of
   0.998 — extracting more information than exists.

**How it was caught:** an ICR above 1.0 is impossible. Treating that as an error
rather than as good news is the whole of it.

**What it would have cost:** Gate 1 would have gone the other way. Before the
fix the model led the heuristic (0.9451); after it, the heuristic leads
(0.9608 vs 0.9029). **The bug was flattering the model, and fixing it reversed
the decision about what to ship.**

**Fixed** with `Taxonomy.rail_key()`, matching by prefix against the *loaded*
matrix so a new rail family is a data change rather than a code change. An
unknown rail still yields a flat likelihood, which is the correct answer for one.

---

## D18 · The LLM stated a wrong amount on a merchant-facing page

**Severity: critical. This one shipped.** Every other defect in this register was
caught before or during the build. This one was live in the dashboard and was
found by review.

**What was on screen.** Decision `pay_SIM0000796` rendered a header of
**₹86.20** while its explanation read *"your UPI Autopay transaction of 79.89
rupees"*. A wrong number, in the one place the submission claims a model cannot
misdescribe a decision.

**Root cause — a correct cache with an incomplete invariant.** Explanations are
keyed per archetype:

```
sha256(cause | tier | sorted(deny_reasons) | confidence_bucket)
```

which is deliberate and right: 1,200 decisions collapse to 50 archetypes, so a
demo costs 50 API calls rather than 1,200. But the amount is not in that key —
correctly, since it does not change the *story*. One cache entry is therefore
served to roughly 24 different payments, and it carries the amount of whichever
payment happened to mint it. **15 of the 50 shipped archetypes contained an
amount.** Roughly 360 of 1,200 decisions rendered a wrong figure.

**Why the existing validation did not catch it.** The validator already rejected
prose naming a cause or tier other than the recorded one. Extending that to
numbers by comparing against the decision record *cannot work*, and this is the
part worth understanding:

> At generation time the number matches the record perfectly. It only becomes
> false **later**, when the cache serves that entry to a different payment. No
> check performed at generation time can see that.

So the invariant has to be a property of the text itself, independent of which
payment it describes.

**The fix: the model never emits digits. The template owns every number.**

An archetype is by definition the payment-*invariant* part of the story, so any
digit in archetype-level prose is a payment-specific claim that does not belong
there. `_validate` now rejects any response containing a digit, and the prompt
asks for none — the model is also no longer *given* the amount, the payment id
or the raw confidence, because it cannot echo a number it never received.
Confidence is passed as a band.

**The fix that was deliberately not chosen.** Adding the amount to the cache key
would also have removed the wrong number, and would have turned 50 API calls
into 1,200 — destroying the archetype design to solve a problem the numbers were
never the model's job to state.

**Audited, not assumed.** Every cached archetype was swept for other
payment-specific values — payment ids, dates, card last4, bundle revisions, bare
integers, tier tokens. Only amounts had leaked. The cache was flushed and
regenerated: **0 of 50 archetypes now contain a digit.**

**Tests added:** a model emitting an amount is rejected; a model emitting *any*
digit is rejected (confidence, counts, last4, rule counts); digit-free prose is
still accepted so the layer remains useful; and the end-to-end case — an entry
minted for one payment and served to another must never carry the first
payment's amount. Plus one that audits the **committed** cache file, because a
fix that leaves a poisoned artifact in the repo has fixed nothing: the dashboard
reads the cache, not the validator.

**Why it belongs in this register.** It is the exact failure mode §10 is about.
The cache was correct. The validation was correct as far as it went. The prose
was fluent and plausible. Nothing raised an error, no test failed, and the page
looked right. It fails by being believable — and unlike the others, it got all
the way to the shipped product before anyone read the number twice.

---

## The rest, by origin

### Pre-existing — found by audit

| # | Defect | Severity | Consequence |
|---|---|---|---|
| **D3** | `opa_allow` recorded intent, not the policy verdict | major | T4 authorises without actuating, so every T0 and T4 row contradicted its own stored input under replay |
| **D4** | The escalation ladder had no legal terminal state | major | Under the four-regulator deadlock the orchestrator could reach "nothing is legal, not even telling a human" |
| **D6** | Bundle contents were not covered by the revision hash | major | The build scripts asserted `opa build` excludes test files. It does not — the committed bundle carried `retry_test.rego`, a file the revision deliberately excluded |
| **D7** | README promised a committed ledger that did not exist | major | `data/.gitignore` was `*`. A judge cloning the repo could not run the attestation demo at all |
| **D8** | `validate-estimator` silently defaulted to the toy world | moderate | The previously quoted +88% naive bias was a domain-free number; on the payments simulator it is +58.7% |
| **D9** | Stale report footer contradicted the numbers above it | moderate | Still read "a property of this toy world… Phase 3 re-derives the magnitude on the real decline simulator" while printing real-simulator results |
| **D14** | BIN velocity is unenforceable from webhook data | open by design | Razorpay's payload has no BIN. Unlike the other missing fields, `0` does not deny a velocity cap — it satisfies one. Documented in `LIMITATIONS.md` §10 rather than hidden behind a plausible zero |

### Introduced during the build, caught before shipping

Recorded separately because the distinction matters when judging the work.

| # | Defect | How it was caught |
|---|---|---|
| **D11** | The first power curve reported its own sampling noise — a CLI default of two replicates overrode the budget allocation, producing a standard error at n=3,000 *larger* than at n=2,000 | A fit-residual gate built into the same command refused to recommend a batch size |
| **D12** | In the new replay code, a partial replay could pass as a full attestation: rows under a missing bundle landed in `total` but in neither `reproduced` nor `unreplayable`, so the report printed a leading `+` and passed | Re-reading my own code before committing it |
| **D13** | The first ablation quoted "+₹2.96, worth ₹14,800 across the batch" with no interval — exactly the naive point-estimate move this project criticises | Noticing the double standard before publishing. A paired bootstrap put the interval at [−₹0.19, +₹6.56], which **straddles zero** and changed the verdict's justification |
| **D15** | `verify` printed `ATTESTATION PASS` while replay was skipped — the same unearned claim as D1, one layer out | Running the judge-facing script on a genuinely fresh clone |
| **D16** | `find_opa` could not resolve `tools/opa`: Git Bash appends `.exe` silently, so the shell resolved it and Python did not — replay would have been skipped on the exact platform the demo is recorded on | Same fresh-clone test |
| **D17** | The verify script installed the **dev** dependency group, so attestation depended on pytest, mypy and bandit installing cleanly — and on Windows mypy's wheel fails outright with a PE trampoline permissions error | Same fresh-clone test, third iteration |

### Found by capturing real traffic

Three ways live Razorpay payloads contradicted the payloads hand-built from the
published schema:

| Field | Assumed | Live |
|---|---|---|
| `notes` | object `{}` | **list `[]`** when unset |
| `acquirer_data.rrn` | present | **absent** — a failed authorisation never reaches settlement |
| `card.name` | not mentioned | **present — the cardholder's name** |

The third is the one that mattered. `redact()` keeps an **allowlist** of card
fields, so `card.name` was dropped without anyone having to know it existed. A
denylist would have committed a real person's name to a public repository, and
no test written against our own payload could have caught it, because our
payload never had the field.

---

## The pattern

Six of these — D1, D5, D12, D15, D18, and arguably D13 — are the same failure in
different clothes: **a claim that looked verified but was not**. A `GROUP BY`
that printed "reproduced". A skipped test indistinguishable from a passing one.
A partial replay printing a leading `+`. A chain-only check printing
`ATTESTATION PASS`. A point estimate printed without its interval. Fluent prose
stating a number that was true for a different payment.

None of them would have produced a red build, a stack trace, or a failing test.
They fail by looking correct. The countermeasures that actually caught them were
the ones that made silence impossible:

- `--require-replay`, so a skipped check cannot print `PASS`;
- `PRAMAN_REQUIRE_OPA`, so a skipped test cannot print green;
- `ok` requiring `reproduced == total`, so partial cannot print as complete;
- a Bayes ceiling test, so an impossible ICR fails rather than impresses;
- a fresh-clone run, which found three defects three iterations in a row that
  no amount of local testing had surfaced;
- a ban on the model emitting digits at all, so cached prose cannot carry a
  number that was only ever true for one payment.

D18 is the one that got through. It is in the register precisely because it
shipped: the countermeasures above were built for failures caught during the
build, and the one that reached the product was the one nobody thought to point
a check at.
