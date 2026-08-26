"""Prometheus metrics.

`praman_policy_violations_total` must read 0 for the lifetime of the system.
That single gauge IS the compliance story — everything else here exists to
explain how it stayed at zero.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ── Ingest (S2: a slow ack becomes a duplicate becomes a compliance breach) ──
WEBHOOK_ACK = Histogram(
    "praman_webhook_ack_seconds",
    "Time to acknowledge a Razorpay webhook. Alert at p99 > 50ms.",
    buckets=(0.001, 0.005, 0.010, 0.020, 0.050, 0.100, 0.250, 1.0),
)

DUPLICATES = Counter(
    "praman_duplicate_deliveries_total",
    "Webhook deliveries rejected as duplicates. Non-zero is GOOD — it proves "
    "idempotency is working and that no duplicate inflated an attempt counter.",
)

SIGNATURE_FAILURES = Counter(
    "praman_signature_failures_total",
    "Webhook deliveries rejected for an invalid HMAC signature.",
)

# ── Policy kernel ────────────────────────────────────────────────────────────
POLICY_VIOLATIONS = Counter(
    "praman_policy_violations_total",
    "Actuations that occurred without an OPA allow. MUST remain 0.",
)

OPA_FAILURES = Counter(
    "praman_opa_failures_total",
    "OPA evaluation failures. Each one fails CLOSED to T4 (law #9).",
)

OPA_LATENCY = Histogram(
    "praman_opa_latency_seconds",
    "OPA policy evaluation latency.",
    buckets=(0.0005, 0.001, 0.003, 0.005, 0.010, 0.050, 0.500),
)

DECISIONS = Counter(
    "praman_decisions_total",
    "Decisions made, by tier and outcome.",
    labelnames=("tier", "allow"),
)

# ── Ledger (S1: a fork is a constraint violation, not silent corruption) ─────
LEDGER_FORK_ATTEMPTS = Counter(
    "praman_ledger_fork_attempts_total",
    "Concurrent appends that would have forked the chain, caught by "
    "UNIQUE(prev_hash). Non-zero means the lock is being exercised, not that "
    "the chain broke.",
)

LEDGER_ENTRIES = Gauge(
    "praman_ledger_entries",
    "Current ledger height.",
)

# ── Inference ────────────────────────────────────────────────────────────────
ATTRIBUTION_CONFIDENCE = Histogram(
    "praman_attribution_confidence",
    "max(posterior) per decision. A left-shifted distribution means the "
    "confidence floor is doing work.",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# ── Explanation ──────────────────────────────────────────────────────────────
LLM_CACHE_HITS = Counter("praman_llm_cache_hits_total", "Archetype cache hits.")
LLM_CACHE_MISSES = Counter("praman_llm_cache_misses_total", "Archetype cache misses.")
LLM_FALLBACKS = Counter(
    "praman_llm_fallbacks_total",
    "Explanations rendered from the deterministic template because the API "
    "errored or was rate-limited. The demo never breaks on an external API.",
)
