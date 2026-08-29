# Backlog — scope discipline made physical

> Every idea that arrives after Day 0 lands here, not in the sprint.
> Nothing is promoted out of this file before Day 9. No exceptions.

## Deferred by explicit decision (v2.0 change log)

| Item | Why deferred | Promote if |
|---|---|---|
| Next.js 15 + shadcn/ui dashboard | Second toolchain, second deploy target, ~5 extra hours landing on freeze day. Jinja2 + Tailwind CDN ships the same information. | Day 8 has genuine slack after freeze criteria are met |
| Razorpay MCP server as actuation surface | Strong ecosystem signal but not on the golden path | Day 8 stretch only |
| Natural-language ledger query via Gemini | Explanation caching matters more | Day 8 stretch only |
| Merkle root anchoring of the ledger | Hash chain already satisfies the audit claim | Never (production path, documented in ARCHITECTURE.md) |
| `lifelines` survival library | Discrete-time hazard *is* pooled logistic regression; one fewer dependency | Never |
| Multi-processor live integrations | Portability is proven by the taxonomy, not by N integrations | Never (out of scope, stated in README) |
| Auth / multi-tenancy / RBAC | Not in the judging bar | Never |
| Real-time streaming | The bar asks for a batch | Never |
| Hyperparameter tuning | Sane defaults; calibration matters more than sharpness | Never |
| Sequential testing (mSPRT) with always-valid CIs | Batch bootstrap is sufficient and simpler to defend | Never (production path) |
| **SHAP / Shapley explanations** | **CUT.** Explainability is served by the model's own top-k feature attributions and by `tier_evaluations`, which shows every deny reason for every tier. A judge asking "why this action?" is answered by the policy trace, not by a Shapley value — the policy trace is the part that is *binding*. Also drops a heavy dependency from the actuation path. | Never |
| **Hazard timing model (Phase 6)** | **CUT.** Retry timing uses a fixed per-cause delay. A discrete-time hazard model changes *when* a retry fires, not whether it is legal or whether the effect is measured correctly — so it cannot strengthen either of the two claims the submission rests on, and it adds a second thing that can be miscalibrated. `slice_runner.RETRY_DELAY_MS` is the fixed delay. | Never |

## Ideas captured during the build

<!-- Append below. Do not act on anything here before Day 9. -->

- **Batched-transaction ledger append.** Considered when the powered batch size
  grew, then *not* built: measured append cost is 0.80 ms/entry, so even a
  ~35,000-entry batch spends ~30 s in the ledger while OPA evaluation dominates
  at 4.6 ms/decline. Batching would also weaken law #4 — a DECISION would no
  longer be durable before its ACTUATION. Not worth trading an architectural law
  for an optimisation the measurements say is unnecessary. Revisit only if a
  batch exceeds ~100k entries.
- **Replay via `opa exec` instead of an ephemeral server.** Rejected: it needs one
  input file per decision on disk, and it would make the replay path differ from
  the live path. A replay that does not use the production client proves nothing
  about the production client.
