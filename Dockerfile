# Praman — application image.
# uv-based, layer-cached on the lockfile so a source edit does not reinstall
# LightGBM. Pinned to 3.12 per blueprint §2.2 (not 3.13 — ML wheel availability).

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer — invalidated only by a lockfile change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Application layer.
COPY src/ ./src/
COPY policy/ ./policy/
RUN uv sync --frozen --no-dev

RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "praman.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
