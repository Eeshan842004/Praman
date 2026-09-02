# Architecture

One page. The topology, the separation of powers, and the three laws that carry
the most weight. The remaining eight are in [`CLAUDE.md`](CLAUDE.md).

## Six layers

```
        Razorpay
           │  payment.failed  (HMAC-SHA256 over the raw body)
           v
┌─────────────────────────────────────────────────────────────────────────┐
│ 1. INGEST            verify · redact · dedupe · ack.   p99 1.46 ms       │
│                      No model, no OPA, no LLM in the request path.       │
│                      Ack-before-think, because a slow ack becomes a      │
│                      redelivery becomes an inflated attempt counter      │
│                      becomes a network fine (S2).                        │
└─────────────────────────────────────────────────────────────────────────┘
           v  (off the request path, in the worker)
┌─────────────────────────────────────────────────────────────────────────┐
│ 2. NORMALISE         processor vocabulary -> one canonical symbol +      │
│                      a LIKELIHOOD VECTOR over 9 causes.                  │
│                      Code 05 genuinely is ambiguous. Mapping it to one   │
│                      cause would rebuild the lookup table we replace.    │
└─────────────────────────────────────────────────────────────────────────┘
           v
┌─────────────────────────────────────────────────────────────────────────┐
│ 3. ATTRIBUTE         prior x likelihood -> posterior over all 9 causes.  │
│                      PROPOSES. Holds no authority whatsoever.            │
└─────────────────────────────────────────────────────────────────────────┘
           v
┌─────────────────────────────────────────────────────────────────────────┐
│ 4. POLICY            OPA / Rego. Versioned, immutable, committed bundle. │
│                      Deny by default. EVERY tier evaluated, never        │
│                      short-circuited, complete deny-set recorded.        │
│                      DISPOSES. Sole authority over money.                │
└─────────────────────────────────────────────────────────────────────────┘
           v
┌─────────────────────────────────────────────────────────────────────────┐
│ 5. LEDGER            SHA-256 hash chain, append-only, 36 hashed fields.  │
│                      Written BEFORE the side effect it describes.        │
│                      REMEMBERS.                                          │
└─────────────────────────────────────────────────────────────────────────┘
           v
┌─────────────────────────────────────────────────────────────────────────┐
│ 6. ACTUATE           T0 terminate  T1 silent retry  T2 rail switch       │
│                      T3 customer nudge  T4 human escalate                │
│                      REST call back to Razorpay. Counters advance HERE.  │
└─────────────────────────────────────────────────────────────────────────┘
           │
           v  (offline, over the same ledger)
        MEASURE        cluster-randomised at the customer · CUPED ·
                       customer-level bootstrap · validated against
                       200 worlds with sealed ground truth
```

## Separation of powers

The design is a constitution, not a pipeline. Each component has exactly one
kind of power and cannot exercise another's.

| Component | May | May never | Enforced by |
|---|---|---|---|
| **Normaliser** | emit a likelihood vector | assert a single cause for an ambiguous code | no `cause_hint` for catch-all codes |
| **Attribution** | propose a posterior | authorise, or widen what is permitted | its output enters policy only as `input`, never as a verdict |
| **LLM** | parse and explain | authorise, or contradict the record | prose naming a different cause or tier is rejected in Python |
| **OPA** | permit or deny | act, or write to the ledger | it is queried; it has no side effects |
| **Orchestrator** | act on a permitted tier | act without a recorded decision | ledger write precedes actuation, asserted by sequence ordering |
| **Ledger** | record | judge, or be edited | append-only trigger; every column inside the hash |
| **Estimator** | measure | change what was done | reads the ledger; opens nothing else |

The property that matters: **no single component can both decide and act.**
Attribution proposes but cannot authorise. OPA authorises but cannot act. The
orchestrator acts but cannot authorise itself, and cannot act before recording.

## The three laws that carry the most weight

### 1. The LLM never authorises money

It parses and it explains. The explanation layer is handed a decision that has
already been made, recorded and attested — nothing it returns is parsed as a
tier, a deny reason or an amount. Output naming a cause or tier other than the
recorded one is rejected and a deterministic template renders instead.

*Prevents:* a confidently-wrong or prompt-injected cause routing a hard decline
into a soft tier (S5, S6).

### 3. OPA is the sole authority — deny by default

```rego
default allow := false
allow if count(deny_reason) == 0
```

Exactly one `allow` rule. Never `allow if <positive condition>`: one loosely
written positive rule leaks a compliance violation through. Thresholds live in
`policy/config/data.json`, covered by the bundle revision, so a rule change is a
config change with an audit trail rather than a code deploy.

*Prevents:* an action taken because no rule happened to forbid it (S3).

### 4. Nothing is actuated before it is recorded

The ledger write precedes the side effect, always. A decision that was actuated
but not recorded is precisely the thing an audit trail exists to make impossible.

*Prevents:* an unreplayable ledger and a broken hash chain (S1, C4).

## Two independent evidence checks

They defend against different attackers, which is why both exist.

| Check | Proves | Cannot prove |
|---|---|---|
| **Hash chain** | nothing was changed after the fact | that the record was ever true |
| **Replay** | the record was true when written — the stored input, re-evaluated against the pinned bundle, reproduces the recorded verdict | that nobody edited it afterwards |

A writer holding the append path can bypass the policy engine, record its own
verdict, and produce a perfect chain. Only replay catches that. Conversely, a
privileged actor who rewrites a row *and* re-derives every downstream hash
produces a valid chain and a divergent replay. Both are tested, including the
second attacker.

Honest limit: hash chaining is tamper-**evident**, not tamper-**preventing**. It
detects modification; it does not stop a root user. `praman tamper` must
explicitly drop the append-only trigger — a privileged, visible act — and the
chain still catches it.

## Deployment topology

Praman **sits beside** Razorpay with exactly two integration surfaces:

| Direction | Surface |
|---|---|
| Inbound | webhook consumer — `POST /webhooks/razorpay`, HMAC-verified. Never polls. |
| Outbound | REST caller — actuates a recovery action, and reads failed payments for fixtures. |

It is **not** an MCP server, does not register as a tool provider, and does not
expose its decision engine to anything that can be prompted. The kernel's
authority must not be reachable by a prompt.

## Storage

Three files, deliberately separate:

| File | Contents | Mutability |
|---|---|---|
| `data/ledger.db` | the evidence — every decision, actuation and outcome | append-only, hash-chained, committed |
| `data/ingest.db` | webhook delivery log and work queue | mutable `processed` flag |
| `data/explanations.db` | archetype-keyed explanation cache | mutable, regenerable |

The ledger is kept alone because it is the artifact the whole claim rests on.
Putting a mutable queue table beside it would invite exactly the question the
evidence file exists to close.
