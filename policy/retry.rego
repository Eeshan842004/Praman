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

# ── Terminal causes ──────────────────────────────────────────────────────────
deny_reason contains "hard_decline" if input.cause_class == "hard"

deny_reason contains "visa_cat1" if input.network_category == 1

deny_reason contains "mac_03" if input.merchant_advice_code == "03"

deny_reason contains "mac_21" if input.merchant_advice_code == "21"

deny_reason contains "npci_no_retry" if input.npci_retry_remark == "do_not_reinitiate"

# ── Network caps (thresholds from data.config — NEVER hardcoded, law #10) ────
deny_reason contains "visa_network_cap" if input.attempts_30d >= data.config.visa_cap

deny_reason contains "per_payment_cap" if input.attempts_this_payment >= data.config.payment_cap

deny_reason contains "bin_velocity" if input.bin_attempts_1h >= data.config.bin_hourly_cap

# ── Customer-contact fatigue ─────────────────────────────────────────────────
deny_reason contains "nudge_fatigue_7d" if {
	input.tier == "T3"
	input.customer_nudges_7d >= data.config.nudge_cap_7d
}

# ── RBI e-mandate: AFA required above threshold ──────────────────────────────
deny_reason contains "rbi_afa_required" if {
	input.is_emandate
	input.amount_paise > data.config.afa_threshold_paise
	not input.afa_completed
}

# ── RBI: 24h pre-debit notification must have elapsed ────────────────────────
deny_reason contains "rbi_pre_debit_notice_not_elapsed" if {
	input.is_emandate
	input.ms_since_pre_debit_notice < data.config.pre_debit_notice_ms
}

# ── NPCI AutoPay blackout: no mandate execution 10:00–13:00 IST ──────────────
deny_reason contains "npci_autopay_blackout_window" if {
	input.rail == "upi_autopay"
	input.ist_hour >= data.config.npci_blackout_start_hour
	input.ist_hour < data.config.npci_blackout_end_hour
}

# ── Confidence floor: the model must not act on a guess (S5) ─────────────────
# OPA bounds legality, not correctness. This is the one place policy can defend
# against a confidently-wrong cause: below the floor, only T4 (human) is legal.
deny_reason contains "low_confidence" if {
	input.tier in {"T0", "T1", "T2", "T3"}
	to_number(input.max_posterior) < to_number(data.config.confidence_floor)
}

# ── Tier feasibility ─────────────────────────────────────────────────────────
deny_reason contains "no_alternate_instrument" if {
	input.tier == "T2"
	not input.has_alternate_instrument
}
