# check=skip=JSONArgsRecommended
# Two stages so the runtime image carries no pip cache and no build tooling.
# All wheels here are manylinux binaries (cryptography included), so no compiler is needed.
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime deps ONLY — requirements-dev.txt (pytest, ruff) deliberately does not ship.
COPY requirements.txt /app/
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim AS runtime

# curl is here for the HEALTHCHECK below and nothing else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Run unprivileged, with a fixed uid/gid so a host bind mount (the wallet, a /data dir) can be
# chowned/permissioned predictably.
RUN groupadd --gid 10001 audit \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin audit

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Only the service package ships. The whole archive flow (intake, runner, rules, comment
# templates, applier) lives in src/archive_flow, so nothing at runtime needs
# scripts/ (operator tooling) — .dockerignore keeps it, tests/ and local notes out of the context.
COPY --chown=audit:audit src/ /app/src/
COPY LICENSE NOTICE /app/

# Writable paths, all owned by the app user:
#   /app/.cache — the IPTC concept cache (KARAKUTU_CONCEPTS_CACHE default .cache/…).
#   /data       — the persistent volume for the SQLite job store (DATA_DIR=/data below; the
#                 store defaults to <DATA_DIR>/karakutu-audit.sqlite3 unless JOB_STORE_URL
#                 says otherwise). /app/data points at it too, so a relative
#                 sqlite:///data/… URL also lands on the volume.
# The wallet is NEVER baked in: mount it read-only and point KARAKUTU_WALLET_PATH at it.
RUN mkdir -p /app/.cache /data \
    && ln -s /data /app/data \
    && chown -R audit:audit /app/.cache /data

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PROVIDER_TYPE=local \
    WEB_SEARCH_PROVIDER=stub \
    PORT=2200 \
    LOG_LEVEL=INFO \
    DATA_DIR=/data \
    UVICORN_GRACEFUL_TIMEOUT=45 \
    FORWARDED_ALLOW_IPS=127.0.0.1

VOLUME ["/data"]

USER audit

EXPOSE 2200

# The liveness probe the compose/K8s deployment should reuse. /health is dependency-free:
# it does not touch the model, the search backend or the archive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-2200}/health" || exit 1

# Shell form so ${PORT} is expanded; `exec` keeps uvicorn as PID 1 so SIGTERM is delivered
# to it directly. --timeout-graceful-shutdown bounds how long in-flight requests get after
# SIGTERM; only after it does the lifespan give the job worker's current case
# JOB_SHUTDOWN_GRACE_S. The orchestrator's stop grace (compose stop_grace_period) must cover
# both: UVICORN_GRACEFUL_TIMEOUT + JOB_SHUTDOWN_GRACE_S + a margin. --proxy-headers trusts
# X-Forwarded-For/-Proto only from FORWARDED_ALLOW_IPS (uvicorn reads that env var); set it to
# the address the container sees the reverse proxy connect from.
CMD exec uvicorn src.api.app:app --host 0.0.0.0 --port "${PORT:-2200}" \
    --proxy-headers --timeout-graceful-shutdown "${UVICORN_GRACEFUL_TIMEOUT:-45}"
