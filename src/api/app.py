# src/api/app.py
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.correlation import CORRELATION_ID, CorrelationIdFilter
from src.api.auth import bearer_matches, unauthorized
from src.api.jobs import router as jobs_router
from src.api.models import AuditRequest, AuditResponse, ErrorResponse, HealthResponse, ReadinessResponse
from src.api.readiness import readiness
from src.clients import get_client
from src.clients.concepts import load_catalog
from src.config.settings import get_settings
from src.jobs.store import open_store
from src.jobs.worker import JobWorker
from src.runtime import build_runtime
from src.workflow.pipeline import run_audit

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - [%(correlation_id)s] - %(message)s",
)
logger = logging.getLogger(__name__)

for _handler in logging.getLogger().handlers:
    _handler.addFilter(CorrelationIdFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    # Fail fast on unsafe configuration (above all an API_AUTH_TOKEN under 32 characters).
    cfg.validate()
    logger.info(
        f"Starting {settings.SERVICE_NAME} "
        f"(provider={settings.PROVIDER_TYPE}, search={settings.WEB_SEARCH_PROVIDER})"
    )
    # One composition root (src/runtime.py) for the API, the batch CLIs and the job worker.
    # `load_catalog` / `get_client` are passed from THIS module so tests can patch them here.
    rt = await build_runtime(settings, catalog_loader=load_catalog, client_factory=get_client)
    provider = rt.provider

    app.state.runtime = rt
    app.state.ready_cache = None   # /ready never serves a result from an earlier lifespan
    app.state.model = rt.model
    app.state.search_provider = rt.search_provider
    app.state.agents = rt.agents
    # ONE archive client for the whole process, not one per request.
    #
    # KarakutuClient owns the ArweaveWalletAuth that caches the 15-minute JWT, and the archive
    # rate-limits /authenticate to 3/min per IP. Building the client inside step 2 (the default
    # when `karakutu_client` is None) therefore threw the cache away on every audit and minted a
    # fresh token each time — so above ~3 audits/minute the mint started failing, get_token
    # returned None, and step 2 degraded to "the archive returned no similar articles". That is
    # a SILENT loss of the relocation signal (the strongest one there is) reported to the caller as a
    # legitimate INCONCLUSIVE, which for an evidence tool is worse than an error.
    app.state.karakutu_client = rt.karakutu_client
    # The IPTC vocabulary for step 1, pulled from the archive once per process (disk-cached).
    # None => the service still boots; classification is skipped with an explanation.
    app.state.catalog = rt.catalog
    if app.state.catalog is None:
        logger.warning("No IPTC concept catalog available — classification will be skipped")

    if not await provider.check_health():
        logger.warning(f"Provider '{settings.PROVIDER_TYPE}' reports unhealthy at startup")

    _warn_about_synthetic_configuration()

    # The job store + worker exist only with API_AUTH_TOKEN set (fail closed: no token, no
    # article routes, nothing that can write to the archive).
    app.state.job_store = None
    app.state.job_worker = None
    app.state.intake_lock = asyncio.Lock()
    if cfg.jobs_enabled:
        store = open_store(cfg.job_store_url)
        worker = JobWorker(store, rt, cfg)
        app.state.job_store, app.state.job_worker = store, worker
        await worker.start()
        logger.info("Article jobs enabled: archive writes %s, concurrency %d",
                    "ENABLED" if cfg.ARCHIVE_WRITES_ENABLED else "disabled (shadow / dry-run mode)",
                    cfg.JOB_CONCURRENCY)

    try:
        yield
    finally:
        logger.info("Shutting down")
        worker, store = app.state.job_worker, app.state.job_store
        if worker is not None:
            await worker.stop(cfg.JOB_SHUTDOWN_GRACE_S)
        if store is not None:
            await store.close()


def _warn_about_synthetic_configuration() -> None:
    """Say loudly, at startup, when the service will produce non-real output.

    The defaults (`MODEL_ENABLED=false`, `WEB_SEARCH_PROVIDER=stub`) are deliberate — they
    are what lets the service boot and the suite run with no secrets and no network. The
    hazard is that in that state `/api/audit` still returns a well-formed AuditResponse with
    confident findings prose, built from a synthetic model and three example.com URLs. For a
    tool whose output a human uses as evidence, that is a worse failure than crashing, and it
    is invisible unless someone reads the verdict closely.

    This is a warning, not a hard failure: refusing to boot would break the zero-config
    development and CI paths that the whole test suite depends on.
    """
    synthetic = []
    if not settings.MODEL_ENABLED and settings.PROVIDER_TYPE == "local":
        synthetic.append("the LLM is pydantic-ai's TestModel (synthetic answers, prompts ignored)")
    if settings.WEB_SEARCH_PROVIDER == "stub":
        synthetic.append("web search returns 3 canned example.com URLs")

    if synthetic:
        logger.warning(
            "SYNTHETIC CONFIGURATION — audits produced now are NOT real findings: %s. "
            "Set MODEL_ENABLED=true (+ MODEL_NAME) and WEB_SEARCH_PROVIDER=searxng "
            "for real output.", "; ".join(synthetic),
        )

    # "Wallet deliberately unset" and "wallet configured but the file is missing" are the
    # same silent outcome (client disabled, step 2 dormant) but very different mistakes. In
    # the container especially: the Dockerfile copies only src/, so the repo-relative default
    # resolves to nothing and the archive is off with no clue why.
    wallet = Path(settings.KARAKUTU_WALLET_PATH)
    if str(wallet).strip() and not wallet.is_file() and not getattr(settings, "KARAKUTU_WALLET_JSON", "").strip():
        logger.warning(
            "KARAKUTU_WALLET_PATH=%s does not exist (resolved: %s) — the archive client is "
            "DISABLED: step 2 (find similar link) will report available=false and step 1 "
            "falls back to the cached IPTC vocabulary or is skipped.",
            settings.KARAKUTU_WALLET_PATH, wallet.resolve(),
        )


app = FastAPI(
    title="Karakutu Audit API",
    description="Six-step audit pipeline for removed news articles.",
    version="1.0.0",
    lifespan=lifespan,
)


class PathScopedCORS:
    """CORSMiddleware for every path EXCEPT `/api/v1/*` (server-to-server: a browser origin has
    no business there, so no CORS headers and no preflight answers)."""

    def __init__(self, app, *, excluded_prefix: str = "/api/v1", **cors_kwargs):
        self.app = app
        self.cors = CORSMiddleware(app, **cors_kwargs)
        self.excluded_prefix = excluded_prefix

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if path == self.excluded_prefix or path.startswith(self.excluded_prefix + "/"):
            await self.app(scope, receive, send)
        else:
            await self.cors(scope, receive, send)


def configure_cors(target: FastAPI, origins) -> bool:
    """Install CORS only when origins are configured (CORS_ALLOW_ORIGINS; default empty = none).
    `*` is for local development. Returns whether the middleware was added."""
    origins = [o for o in (origins or []) if o]
    if not origins:
        return False
    target.add_middleware(PathScopedCORS, allow_origins=origins, allow_credentials=False,
                          allow_methods=["GET", "POST"], allow_headers=["*"])
    return True


configure_cors(app, settings.cors_origins)
app.include_router(jobs_router)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Bind a correlation id for the request and echo it back on the response.

    An upstream id is honoured when supplied so a caller can stitch its own trace to ours;
    it is length-capped and stripped of control characters because it lands in log lines.
    """
    supplied = (request.headers.get("X-Correlation-ID") or "").strip()
    cid = "".join(c for c in supplied if c.isprintable())[:64] or uuid.uuid4().hex[:12]
    token = CORRELATION_ID.set(cid)
    try:
        response = await call_next(request)
    finally:
        CORRELATION_ID.reset(token)
    response.headers["X-Correlation-ID"] = cid
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Field locations and messages only — never the submitted values (`str(exc)` echoed them)."""
    detail = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ())) or 'body'}: {e.get('msg', 'invalid')}"
        for e in exc.errors())
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(error="Validation error", detail=detail[:1000], status_code=422).model_dump(),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Log the failure in full; tell the caller only its correlation id.

    `str(exc)` used to be returned in `detail`. On this service that is a disclosure channel:
    unhandled exceptions here carry upstream URLs, internal hostnames, filesystem paths and
    raw SDK error bodies, and `/api/audit` is unauthenticated. The id is enough for an
    operator to find the full traceback in the logs.
    """
    cid = CORRELATION_ID.get()
    logger.error("Unhandled exception: %s: %s", type(exc).__name__, exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error="Internal server error",
            detail=f"The audit could not be completed. Correlation id: {cid}",
            status_code=500,
        ).model_dump(),
        headers={"X-Correlation-ID": cid},
    )


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service=settings.SERVICE_NAME)


# /ready is open to anonymous callers and each evaluation writes to the store and probes up to
# three upstreams, so one result is served for READY_CACHE_S to every caller (bounded work per
# second however often it is polled). Keyed by the objects it inspects, so a new lifespan starts
# fresh. The cache holds the full evaluation; each request is then projected to its view:
# anonymous callers get only {"status"} (same 200/503), and the checks, writes_enabled and
# write_blockers go only to a caller presenting `Authorization: Bearer <API_AUTH_TOKEN>`. With
# API_AUTH_TOKEN unset the detailed view is never served.
READY_CACHE_S = 5.0


@app.get("/ready", response_model=ReadinessResponse, response_model_exclude_none=True,
         responses={503: {"model": ReadinessResponse}})
async def ready(request: Request, response: Response) -> ReadinessResponse:
    state = request.app.state
    key = tuple(id(getattr(state, k, None)) for k in ("runtime", "job_store", "job_worker"))
    lock = getattr(state, "ready_lock", None)
    if lock is None:
        lock = state.ready_lock = asyncio.Lock()
    async with lock:
        cached = getattr(state, "ready_cache", None)
        now = time.monotonic()
        if cached is not None and cached[0] == key and now < cached[1]:
            code, body = cached[2], cached[3]
        else:
            code, body = await readiness(state, get_settings())
            state.ready_cache = (key, now + READY_CACHE_S, code, body)
    response.status_code = code
    if _ready_detail_authorized(request):
        return body
    return ReadinessResponse(status=body.status)


def _ready_detail_authorized(request: Request) -> bool:
    """The detailed /ready view: only with API_AUTH_TOKEN set AND presented (fail closed)."""
    return bearer_matches(request, get_settings().API_AUTH_TOKEN)


# --- /api/audit guards -------------------------------------------------------------------------
#
# Optional bearer token (API_AUTH_TOKEN): enforced when set, compared in constant time. And an
# absurd-size guard: any Article text field of 2,000,000 characters or more is refused (413).
# Everything below that is accepted exactly as before — the pipeline still truncates content to
# CONTENT_LIMIT=120_000 where it always did, so no previously accepted request changes result.
MAX_ARTICLE_FIELD_CHARS = 2_000_000


class _Unauthorized(Exception):
    pass


@app.exception_handler(_Unauthorized)
async def _unauthorized_handler(request: Request, exc: _Unauthorized):
    return unauthorized()


async def require_audit_token(request: Request) -> None:
    """Dependency: runs BEFORE body validation, so an unauthenticated caller learns nothing
    about the request schema."""
    if not _audit_authorized(request):
        raise _Unauthorized()


def _audit_authorized(request: Request) -> bool:
    token = get_settings().API_AUTH_TOKEN
    if not token:
        return True
    return bearer_matches(request, token)


def _oversized_field(body: AuditRequest):
    for i, art in enumerate([body.removed_article, *body.similar_articles]):
        where = "removed_article" if i == 0 else f"similar_articles[{i - 1}]"
        for name in ("url", "title", "content"):
            if len(getattr(art, name) or "") >= MAX_ARTICLE_FIELD_CHARS:
                return f"{where}.{name}"
    return None


@app.post(
    "/api/audit",
    response_model=AuditResponse,
    response_model_exclude_none=False,  # keep short_circuited_at: null in the JSON body
    dependencies=[Depends(require_audit_token)],
)
async def audit(request: Request, body: AuditRequest) -> AuditResponse:
    field = _oversized_field(body)
    if field:
        return JSONResponse(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            content=ErrorResponse(error="field_too_large",
                                                  detail=f"{field} is {MAX_ARTICLE_FIELD_CHARS:,} characters or more",
                                                  status_code=413).model_dump())
    state = request.app.state
    return await run_audit(body, agents=state.agents, search_provider=state.search_provider,
                           catalog=state.catalog, karakutu_client=state.karakutu_client)
