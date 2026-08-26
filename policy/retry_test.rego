package praman.retry_test

import rego.v1

import data.praman.retry

# The full threshold set, mirroring policy/config/data.json. Declared once and
# reused so a threshold change shows up as exactly one diff in this file.
cfg := {
	"visa_cap": 15,
	"payment_cap": 3,
	"bin_hourly_cap": 10,
	"nudge_cap_7d": 2,
	"afa_threshold_paise": 1500000,
	"pre_debit_notice_ms": 86400000,
	"npci_blackout_start_hour": 10,
	"npci_blackout_end_hour": 13,
	"confidence_floor": "0.400000",
}

base := {
	"cause_class": "soft",
	"network_category": 2,
	"merchant_advice_code": "01",
	"npci_retry_remark": "reinitiate_same_crn",
	"attempts_30d": 3,
	"attempts_this_payment": 1,
	"bin_attempts_1h": 2,
	"tier": "T1",
	"customer_nudges_7d": 0,
	"is_emandate": false,
	"amount_paise": 50000,
	"afa_completed": false,
	"ms_since_pre_debit_notice": 999999999,
	"rail": "card",
	"ist_hour": 15,
	"max_posterior": "0.810000",
	"has_alternate_instrument": true,
}

# ── Happy path ───────────────────────────────────────────────────────────────
test_allows_clean_soft_decline if {
	retry.allow with input as base with data.config as cfg
}

# ── Network caps ─────────────────────────────────────────────────────────────
test_denies_at_visa_cap if {
	not retry.allow with input as object.union(base, {"attempts_30d": 15}) with data.config as cfg
}

test_allows_just_below_visa_cap if {
	retry.allow with input as object.union(base, {"attempts_30d": 14}) with data.config as cfg
}

test_denies_at_per_payment_cap if {
	not retry.allow with input as object.union(base, {"attempts_this_payment": 3}) with data.config as cfg
}

test_denies_on_bin_velocity if {
	not retry.allow with input as object.union(base, {"bin_attempts_1h": 10}) with data.config as cfg
}

# ── Absolute terminal signals ────────────────────────────────────────────────
test_mac_03_is_absolute if {
	not retry.allow with input as object.union(base, {"merchant_advice_code": "03"}) with data.config as cfg
}

test_mac_21_is_absolute if {
	not retry.allow with input as object.union(base, {"merchant_advice_code": "21"}) with data.config as cfg
}

test_visa_cat1_never_retries if {
	not retry.allow with input as object.union(base, {"network_category": 1}) with data.config as cfg
}

test_hard_decline_never_retries if {
	not retry.allow with input as object.union(base, {"cause_class": "hard"}) with data.config as cfg
}

test_npci_do_not_reinitiate_is_absolute if {
	not retry.allow with input as object.union(base, {"npci_retry_remark": "do_not_reinitiate"}) with data.config as cfg
}

# ── Confidence floor (S5) ────────────────────────────────────────────────────
test_low_confidence_blocks_automated_tiers if {
	not retry.allow with input as object.union(base, {"max_posterior": "0.310000"}) with data.config as cfg
}

test_low_confidence_still_permits_t4 if {
	retry.allow with input as object.union(base, {"max_posterior": "0.310000", "tier": "T4"}) with data.config as cfg
}

# ── RBI ──────────────────────────────────────────────────────────────────────
test_rbi_afa_blocks_high_value_emandate if {
	not retry.allow with input as object.union(base, {
		"is_emandate": true,
		"amount_paise": 2500000,
	})
		with data.config as cfg
}

test_rbi_afa_satisfied_allows if {
	retry.allow with input as object.union(base, {
		"is_emandate": true,
		"amount_paise": 2500000,
		"afa_completed": true,
	})
		with data.config as cfg
}

test_rbi_pre_debit_notice_must_elapse if {
	not retry.allow with input as object.union(base, {
		"is_emandate": true,
		"ms_since_pre_debit_notice": 1000,
	})
		with data.config as cfg
}

# ── NPCI AutoPay blackout ────────────────────────────────────────────────────
test_npci_blackout_blocks_autopay if {
	not retry.allow with input as object.union(base, {
		"rail": "upi_autopay",
		"ist_hour": 11,
	})
		with data.config as cfg
}

test_npci_outside_blackout_allows_autopay if {
	retry.allow with input as object.union(base, {
		"rail": "upi_autopay",
		"ist_hour": 14,
	})
		with data.config as cfg
}

# ── Contact fatigue ──────────────────────────────────────────────────────────
test_nudge_fatigue_blocks_t3 if {
	not retry.allow with input as object.union(base, {
		"tier": "T3",
		"customer_nudges_7d": 2,
	})
		with data.config as cfg
}

test_nudge_fatigue_does_not_block_t1 if {
	retry.allow with input as object.union(base, {
		"tier": "T1",
		"customer_nudges_7d": 5,
	})
		with data.config as cfg
}

# ── Tier feasibility ─────────────────────────────────────────────────────────
test_t2_requires_alternate_instrument if {
	not retry.allow with input as object.union(base, {
		"tier": "T2",
		"has_alternate_instrument": false,
	})
		with data.config as cfg
}

# ─────────────────────────────────────────────────────────────────────────────
# ★ S3 — THE REGULATORY DEADLOCK
# One payment. Four independent regulators forbid an action simultaneously.
# This test is the reason the ladder must evaluate every tier and never
# short-circuit: the value is in the COMPLETE deny-set, not the first hit.
# ─────────────────────────────────────────────────────────────────────────────
test_regulatory_deadlock_records_all_conflicts if {
	reasons := retry.deny_reason with input as object.union(base, {
		"is_emandate": true,
		"amount_paise": 2200000, # ₹22,000 > ₹15,000 AFA threshold
		"rail": "upi_autopay",
		"ist_hour": 11, # inside NPCI 10:00–13:00 blackout
		"tier": "T3",
		"customer_nudges_7d": 2, # at fatigue cap
		"ms_since_pre_debit_notice": 1000, # 24h notice has NOT elapsed
	})
		with data.config as cfg

	count(reasons) == 4
	"rbi_afa_required" in reasons
	"rbi_pre_debit_notice_not_elapsed" in reasons
	"npci_autopay_blackout_window" in reasons
	"nudge_fatigue_7d" in reasons
}

test_regulatory_deadlock_denies if {
	not retry.allow with input as object.union(base, {
		"is_emandate": true,
		"amount_paise": 2200000,
		"rail": "upi_autopay",
		"ist_hour": 11,
		"tier": "T3",
		"customer_nudges_7d": 2,
		"ms_since_pre_debit_notice": 1000,
	})
		with data.config as cfg
}

# ── Bundle revision is echoed from data, not computed ────────────────────────
test_bundle_revision_is_echoed if {
	retry.bundle_revision == "test-rev-abc" with data.revision as {"revision": "test-rev-abc"}
}
