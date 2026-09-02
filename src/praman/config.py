"""Central configuration.

Architectural law #11: every threshold lives in policy config, zero magic numbers
in code. This module holds *wiring* — endpoints, secrets, paths — never policy
thresholds. Anything a compliance officer would want to audit belongs in
`policy/config/data.json`, where OPA loads it as `data.config` and it is covered
by the bundle revision.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Razorpay (test mode only) ────────────────────────────────────────────
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # ── Gemini — explanation rendering ONLY. Law #1: never authorises. ───────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # OpenAI-compatible gateway. Gemini behind that wire format means one
    # client shape works for both, so a provider swap is a URL change.
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    explain_cache_path: Path = Path("data/explanations.db")

    # ── OPA sidecar ──────────────────────────────────────────────────────────
    opa_url: str = "http://127.0.0.1:8181"
    opa_timeout_seconds: float = 0.5
    opa_decision_path: str = "praman/retry"

    # ── Storage ──────────────────────────────────────────────────────────────
    ledger_path: Path = Path("data/ledger.db")
    # The delivery log is deliberately a SEPARATE file from the ledger. The
    # ledger is evidence: append-only, hash-chained, every column inside the
    # hash. The delivery log has a mutable `processed` flag, so keeping them in
    # one file would invite the very question the evidence file exists to close.
    ingest_path: Path = Path("data/ingest.db")

    # ── Experiment (law #8) ──────────────────────────────────────────────────
    # Changing experiment_id re-randomises every arm assignment. Immutable once
    # a batch has run, or the ledger's arms no longer re-derive.
    experiment_id: str = "praman-v1"
    holdout_pct: int = Field(default=20, ge=1, le=50)

    # ── Runtime ──────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    env: str = "dev"

    @property
    def opa_decision_url(self) -> str:
        return f"{self.opa_url.rstrip('/')}/v1/data/{self.opa_decision_path.strip('/')}"

    @property
    def ledger_abspath(self) -> Path:
        p = self.ledger_path
        return p if p.is_absolute() else (REPO_ROOT / p)

    @property
    def explain_cache_abspath(self) -> Path:
        p = self.explain_cache_path
        return p if p.is_absolute() else (REPO_ROOT / p)

    @property
    def ingest_abspath(self) -> Path:
        p = self.ingest_path
        return p if p.is_absolute() else (REPO_ROOT / p)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
