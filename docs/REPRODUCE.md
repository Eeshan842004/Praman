# Reproduce every number

> Every figure quoted in `README.md`, with the exact command that regenerates it
> and the runtime to expect. Each command below was run and its output compared
> against the value in this table before the table was committed.
>
> If a number here disagrees with the command, **the number is a bug**. Report it.

## Prerequisites

`uv` is the only one. It builds the Python environment on demand.
`scripts/verify.sh` fetches the OPA binary into `tools/` if it is missing;
everything else assumes `.\scripts\bootstrap.ps1` has done the same.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # or the PowerShell line in README
```

---

## Tier 1 — runs on a fresh clone, no setup, seconds

| Claim | Command | Expected | Runtime |
|---|---|---|---|
| Chain intact, 2,988 entries | `./scripts/verify.sh` | `+ 2988 entries . chain intact` | ~15 s |
| 1,200/1,200 decisions reproduced | `./scripts/verify.sh` | `+ 1200/1200 decisions reproduced against 2 pinned bundle(s)` | ~15 s |
| Two pinned bundle spans | `./scripts/verify.sh` | `4ca4787c0a1eea75` decision entries 1–988; `bd45b0c7e5ce66a3` decision entries 991–2986 | ~15 s |
| 0 policy violations | `./scripts/verify.sh` | `+ 0 policy violations across 588 actuations` | ~15 s |
| Tamper is detected | `praman tamper --ledger data/ledger.db --entry 447 --set amount_paise=99999900` then `./scripts/verify.sh` | `CHAIN BROKEN at entry 447` → `ATTESTATION FAIL`, exit 1 | ~15 s |

> The tamper command **modifies the committed ledger**. Restore it with
> `git checkout data/ledger.db`.

## Tier 2 — policy kernel, seconds

| Claim | Command | Expected |
|---|---|---|
| 28/28 policy tests, 100% coverage | `opa test policy/ -v --coverage --threshold 90` | `PASS: 28/28`, coverage 100 |
| Bundle revision is reproducible | `./scripts/build_bundle.sh` twice, then `git diff --exit-code policy/revision/data.json` | no diff |
| T4 is legal under every deny combination | `opa test policy/ -v` | `test_t4_is_allowed_when_every_rule_fires: PASS` |

## Tier 3 — the test suite, minutes

| Claim | Command | Expected | Runtime |
|---|---|---|---|
| 417 tests pass | `uv run pytest` | `417 passed` | ~4 min |
| Replay tests actually run (do not skip) | `PRAMAN_REQUIRE_OPA=1 uv run pytest tests/test_replay.py` | 14 passed, 0 skipped | ~40 s |
| Webhook p99 < 20 ms over a 200-request burst | `uv run pytest tests/test_ingest.py -k p99 -q` | passes; measured p50 0.71 ms / p95 0.92 ms / **p99 1.46 ms** | ~10 s |

## Tier 4 — measurement, minutes to tens of minutes

| Claim | Command | Expected | Runtime |
|---|---|---|---|
| Estimator bias −3.4%, coverage 95.5% | `uv run praman validate-estimator --worlds 200 --world-n 2000` | `bias -3.4%`, `coverage 95.5%` | ~4 min |
| Naive gross bias +58.7%, coverage 8.5% | same command | `+58.7%`, `8.5%` | — |
| H(C) 2.7498 bits; features add 3.89% | `uv run praman icr-audit --n 20000 --seed 101` | matches the table in `GATE_LOG.md` | ~30 s |
| Gate 1: AUC 0.9566, ICR 0.9608 vs 0.9029 | `uv run praman ablation` | `VERDICT: ship the HEURISTIC` | ~8 min |
| MDE at n=3,000 is ₹41.09 vs a ₹36.20 effect | `uv run praman power --load docs/power_curve.json` | renders the saved measurement instantly | instant |
| The power curve itself | `uv run praman power --save docs/power_curve.json` | re-measures it | **~25 min** |
| Three-tier result (n=5,000 powered) | `uv run praman report --powered-n 5000` | SECONDARY ₹39.03, CI [20.00, 56.71] | ~12 min |

> `praman power` and `praman report` need the OPA sidecar running
> (`.\scripts\dev.ps1`). `praman ablation` does too.

## Tier 5 — live integrations, needs credentials

| Claim | Command | Notes |
|---|---|---|
| 9 real Razorpay failures captured | `uv run python scripts/capture_fixtures.py` | needs `RAZORPAY_KEY_*` in `.env`; refuses non-`rzp_test_` keys |
| Normaliser handles every live payload | `uv run pytest tests/test_fixtures.py` | runs against the committed fixtures, no keys needed |
| Explanations, model-written | `uv run praman explain --seq 11` | falls back to the deterministic template with no `GEMINI_API_KEY` |
| Dashboard | `uv run uvicorn praman.api.app:app` then open `http://127.0.0.1:8000/` | reads the committed ledger |

---

## Regenerating the evidence

| Artifact | Command | Runtime |
|---|---|---|
| Committed demo ledger (~3 MB, two bundles) | `./scripts/regenerate_ledger.sh demo` | ~30 s |
| Full run behind the reported figures (~23 MB) | `./scripts/regenerate_ledger.sh full` | ~2 min |
| Saved estimator validation | `uv run praman validate-estimator --worlds 200 --save docs/estimator_validation.json` | ~4 min |
| Saved power curve | `uv run praman power --save docs/power_curve.json` | ~25 min |
| Pre-warmed explanation cache | `uv run praman prewarm` | ~2 min, needs a Gemini key |

## Which ledger is which

Two distinct artifacts, and confusing them would make the numbers look
inconsistent:

- **`data/ledger.db` (committed, 2,988 entries, 1,200 decisions)** exists so a
  judge can attest without generating anything. It spans both bundles. Its
  per-batch effect estimates are *not* the reported figures: both batches sit
  **below the minimum detectable effect**, so a null there would be arithmetic
  rather than evidence. Underpowered means you *may* miss, not that you must —
  as it happens both intervals exclude zero, which is luck rather than design
  and is exactly why the headline uses the powered run instead.
- **The full run (`--mode full`, 22,912 entries, 9,200 decisions)** is what the
  three-tier result was measured on. Regenerate it with the command above if you
  want to re-derive SECONDARY and ILLUSTRATIVE yourself.

## Known non-determinism

Everything is seeded, so the same command gives the same answer on the same
machine. Two caveats:

- Figures measured across *batches* (the power curve's per-point standard
  errors, the ablation's ICRs) vary by a percent or two between grid seeds. The
  tables above quote the committed seeds.
- `praman ablation` trains LightGBM. Thread scheduling makes the fourth decimal
  place of the AUC non-reproducible; the verdict is not close to the boundary.
