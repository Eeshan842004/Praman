package praman.retry

import rego.v1

# ─────────────────────────────────────────────────────────────────────────────
# Architectural law #3: deny by default. Never write `allow if <positive>`.
# A single loosely-written positive rule leaks a compliance violation through.
# ─────────────────────────────────────────────────────────────────────────────
default allow := false

allow if count(deny_reason) == 0

# Echo the loaded bundle revision so every decision records what OPA ACTUALLY ran.
# Architectural law #6: never trust a locally-computed hash of a policy file.
bundle_revision := data.revision.revision

# ─────────────────────────────────────────────────────────────────────────────
# T4 — human escalation — is unconditionally legal
# ─────────────────────────────────────────────────────────────────────────────
# Every rule in this file governs a money movement or a customer contact. T4
# routes the decline to the MERCHANT'S OWN ops queue. That is neither a debit
# nor a customer contact, so RBI's pre-debit-notice rule, NPCI's blackout
# window, the network caps and our own fatigue and confidence limits have no
# jurisdiction over it — they were never written about it.
#
# This is not a convenience. If every tier including T4 can deny, the ladder has
# no legal terminal state and the orchestrator is undefined at exactly the
# moment the design exists to handle: four regulators simultaneously forbidding
# every action. "Nothing is legal, not even telling a human" is not a decision a
# payments system is allowed to reach.
#
# The exemption lives in ONE place rather than as a guard repeated on fourteen
# rules, so a rule added later cannot forget it. `rule_fired` stays the complete
# set of what triggered — the audit trail keeps seeing everything — while
# `deny_reason` is what BINDS.
#
# Law #3 is untouched: still exactly one `allow` rule, still
# `count(deny_reason) == 0`, still deny by default, still no positive rule.
# Nothing was made permissive. One tier was placed outside the jurisdiction of
# rules that never claimed it.
deny_reason contains reason if {
	input.tier != "T4"
	some reason in rule_fired
}

# ── Terminal causes ──────────────────────────────────────────────────────────
rule_fired contains "hard_decline" if input.cause_class == "hard"

rule_fired contains "visa_cat1" if input.network_category == 1

rule_fired contains "mac_03" if input.merchant_advice_code == "03"

rule_fired contains "mac_21" if input.merchant_advice_code == "21"

rule_fired contains "npci_no_retry" if input.npci_retry_remark == "do_not_reinitiate"

# ── Network caps (thresholds from data.config — NEVER hardcoded, law #11) ────
rule_fired contains "visa_network_cap" if input.attempts_30d >= data.config.visa_cap

rule_fired contains "per_payment_cap" if input.attempts_this_payment >= data.config.payment_cap

rule_fired contains "bin_velocity" if input.bin_attempts_1h >= data.config.bin_hourly_cap

# ── Customer-contact fatigue ─────────────────────────────────────────────────
rule_fired contains "nudge_fatigue_7d" if {
	input.tier == "T3"
	input.customer_nudges_7d >= data.config.nudge_cap_7d
}

# ── RBI e-mandate: AFA required above threshold ──────────────────────────────
rule_fired contains "rbi_afa_required" if {
	input.is_emandate
	input.amount_paise > data.config.afa_threshold_paise
	not input.afa_completed
}

# ── RBI: 24h pre-debit notification must have elapsed ────────────────────────
rule_fired contains "rbi_pre_debit_notice_not_elapsed" if {
	input.is_emandate
	input.ms_since_pre_debit_notice < data.config.pre_debit_notice_ms
}

# ── NPCI AutoPay blackout: no mandate execution 10:00–13:00 IST ──────────────
rule_fired contains "npci_autopay_blackout_window" if {
	input.rail == "upi_autopay"
	input.ist_hour >= data.config.npci_blackout_start_hour
	input.ist_hour < data.config.npci_blackout_end_hour
}

# ── Confidence floor: the model must not act on a guess (S5) ─────────────────
# OPA bounds legality, not correctness. This is the one place policy can defend
# against a confidently-wrong cause: below the floor, only T4 (human) is legal.
# The tier guard is now redundant with the T4 exemption above and is kept
# deliberately — it is the rule that must survive if the exemption is ever
# removed, because escalating a guess to a human is the correct response to low
# confidence, not a loophole in it.
rule_fired contains "low_confidence" if {
	input.tier in {"T0", "T1", "T2", "T3"}
	to_number(input.max_posterior) < to_number(data.config.confidence_floor)
}

# ── Tier feasibility ─────────────────────────────────────────────────────────
rule_fired contains "no_alternate_instrument" if {
	input.tier == "T2"
	not input.has_alternate_instrument
}
