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

## Ideas captured during the build

<!-- Append below. Do not act on anything here before Day 9. -->

- _(none yet)_
