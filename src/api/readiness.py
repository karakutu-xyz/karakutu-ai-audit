"""`GET /ready` — is this process able to do real work right now? (`/health` stays dependency-free
for the container HEALTHCHECK.)

Each check is one of: ok | fail | synthetic | missing | disabled.

  store    the job store accepts a write (article jobs enabled), else disabled
  wallet   the archive client has a wallet (present/absent only — never its content or path)
  archive  the archive API answers its JSON /health (wallet present), else disabled
  model    the provider answers its health check and the worker is not paused on a fail streak;
           `synthetic` for pydantic-ai's TestModel
  search   SearXNG answers /healthz; `synthetic` for the canned stub; duckduckgo is not probed
  catalog  the IPTC concept catalog is loaded

503 when any check is `fail`, and — with article jobs enabled AND ARCHIVE_WRITES_ENABLED=true —
when anything would block a write (synthetic model or search, no catalog, no wallet): the
service must never look ready to publish while it would only produce synthetic findings.
"""
import asyncio
from typing import Tuple

import httpx
from pydantic_ai.models.test import TestModel

from src.api.models import ReadinessResponse
from src.jobs.gates import write_blockers

PROBE_TIMEOUT_S = 5.0


async def _probe_searxng(base: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            resp = await client.get(f"{base.rstrip('/')}/healthz")
        return resp.is_success
    except Exception:  # noqa: BLE001 — readiness answers "no", it never raises
        return False


async def _bool(coro) -> bool:
    try:
        return bool(await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT_S * 2))
    except Exception:  # noqa: BLE001
        return False


async def readiness(state, settings) -> Tuple[int, ReadinessResponse]:
    rt = getattr(state, "runtime", None)
    store = getattr(state, "job_store", None)
    worker = getattr(state, "job_worker", None)
    client = getattr(rt, "karakutu_client", None)
    checks = {}

    checks["store"] = ("disabled" if not settings.jobs_enabled or store is None
                       else "ok" if await _bool(store.check()) else "fail")
    wallet = bool(client is not None and getattr(client, "enabled", False))
    checks["wallet"] = "ok" if wallet else "missing"
    checks["archive"] = ("disabled" if not wallet
                         else "ok" if await _bool(client.check_health()) else "fail")
    if rt is None or isinstance(getattr(rt, "model", None), TestModel):
        checks["model"] = "synthetic"
    elif worker is not None and (worker.paused or worker.model_unavailable):
        checks["model"] = "fail"
    else:
        checks["model"] = "ok" if await _bool(rt.provider.check_health()) else "fail"
    search = str(settings.WEB_SEARCH_PROVIDER).strip().lower()
    if search == "stub":
        checks["search"] = "synthetic"
    elif search == "searxng":
        checks["search"] = "ok" if await _probe_searxng(settings.SEARXNG_URL) else "fail"
    else:
        checks["search"] = "ok"
    checks["catalog"] = "ok" if getattr(rt, "catalog", None) is not None else "missing"
    if settings.jobs_enabled:
        checks["worker"] = ("ok" if worker is not None and worker.running else "fail")

    blockers = write_blockers(settings, rt)
    writes = bool(settings.jobs_enabled and settings.ARCHIVE_WRITES_ENABLED)
    not_ready = any(v == "fail" for v in checks.values()) or (
        writes and any(b != "writes_disabled" for b in blockers))
    body = ReadinessResponse(status="not_ready" if not_ready else "ready", checks=checks,
                             writes_enabled=writes, write_blockers=blockers)
    return (503 if not_ready else 200), body
