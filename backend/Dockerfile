FROM ghcr.io/astral-sh/uv:0.12.2 AS uv
# Debian trixie, not bookworm: bookworm's OpenSSL 3.0 TLS handshake draws a Cloudflare bot
# challenge (HTTP 403, cf-mitigated: challenge) from publishers that serve the same request
# from a newer OpenSSL, so whole outlets read as unreadable inside the container only.
FROM python:3.11-slim-trixie

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/backend \
    DATA_DIR=/data \
    LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/audit ./backend/audit
COPY searxng/settings.yml ./searxng/settings.yml
COPY compose.yaml compose.search.yaml compose.test.yaml \
    compose.postgres.yaml compose.wallet.yaml ./
RUN groupadd --gid 10001 audit \
    && useradd --uid 10001 --gid audit --no-create-home audit \
    && mkdir /data && chown audit:audit /data
USER audit
EXPOSE 2200
CMD ["uvicorn", "audit.api:app", "--host", "0.0.0.0", "--port", "2200", "--no-access-log"]
