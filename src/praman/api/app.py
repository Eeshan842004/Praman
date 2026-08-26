"""FastAPI application factory."""

from __future__ import annotations

import logging
from typing import Any

import structlog
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from praman.config import settings


def configure_logging() -> None:
    """Structured JSON logs. Every line is correlatable by payment_id/decision_id."""
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="Praman",
        description=(
            "A provable revenue-recovery kernel. The model proposes, the policy "
            "disposes, the ledger remembers."
        ),
        version="0.1.0",
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "env": settings.env}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # Phase 1 attaches the webhook router here. The factory stays stable so that
    # later phases add routes without touching startup, logging, or metrics.

    return app


app = create_app()
