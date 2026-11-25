"""Read-only archive audit service with durable local run history.

Live ingestion is separately gated from benchmarks. No route writes to the archive.
All /api routes are authenticated when a token is configured; live routes always
require one. Middleware applies these gates before parsing any submitted values.
"""
import asyncio
import difflib
import importlib
import importlib.util
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from hmac import compare_digest
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from audit.config import MODEL_STEPS, Settings, canonical_request_host
from audit.db import ArticleStoreError, PromptStoreError, Store
from audit.domain import AuditRequest, Verdict
from audit.engine import AuditEngine
from audit.record import RecordError, build_result
from audit.provenance import build_base, models_from_config, with_prompts
from audit.schemas import (
    ArticleDetail, ArticleList, ArticleImported, ArticleStatus,
    AuditResult, Health, Reaction, ReactionInput, RunDetail, RunInput, RunList,
    SafeErrorResponse, ServiceStatus,
    WorkflowSnapshot, PromptList, PromptDetail, PromptVersion, PromptVersionInput,
    ModelList, ModelStepInput, ModelAssigned, QueueState, QueueCancelled,
    PromptActivateInput, PromptAuthorInput, PromptActivated, PromptDiff,
)

from audit.workflow.prompt_registry import DEFAULTS, REGISTRY, validation_codes

MAX_BODY_BYTES = 2 * 1024 * 1024
LIVE_PATHS = frozenset({"/api/audit", "/api/webhooks/audit"})


def unified_prompt_diff(before: str, after: str, fromfile: str, tofile: str) -> str:
    """Unified diff of two stored texts, marking a missing final newline like `diff -u`."""
    lines = []
    for line in difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                     fromfile=fromfile, tofile=tofile):
        lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(lines)


class RequestGuards:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        api_path = path == "/api" or path.startswith("/api/")
        if api_path:
            original_send = send

            async def uncached_send(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [
                        (key, value) for key, value in message.get("headers", [])
                        if key.lower() not in {b"cache-control", b"pragma"}
                    ] + [(b"cache-control", b"no-store"), (b"pragma", b"no-cache")]}
                await original_send(message)

            send = uncached_send
        headers = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.lower(), []).append(value)

        async def reject(status, code):
            await JSONResponse(status_code=status, content={"error": {"code": code}})(scope, receive, send)

        prompt_write = scope["method"] == "POST" and path.startswith("/api/prompts/")
        model_write = scope["method"] == "POST" and path == "/api/models/steps"
        if model_write and not self.settings.api_auth_token:
            return await reject(403, "model_writes_require_token")
        if prompt_write and not self.settings.api_auth_token:
            return await reject(403, "prompt_writes_require_token")
        live = path in LIVE_PATHS
        if live and (not self.settings.live_ingestion_enabled or not self.settings.api_auth_token):
            return await reject(404, "not_found")
        # A local app without authentication must reject DNS-rebinding hosts,
        # including on GET endpoints that can expose saved articles.
        hosts = headers.get(b"host", [])
        try:
            host = urlsplit("//" + hosts[0].decode("ascii")) if len(hosts) == 1 else None
            trusted_host = host and canonical_request_host(host.hostname) in self.settings.allowed_hosts and not host.username and not host.password and not host.path and not host.query and not host.fragment
            if host:
                _ = host.port
        except (ValueError, UnicodeDecodeError):
            trusted_host = False
        if not trusted_host:
            return await reject(400, "untrusted_host")
        if api_path:
            token = self.settings.api_auth_token
            if token:
                auth = headers.get(b"authorization", [])
                expected = ("Bearer " + token).encode()
                if len(auth) != 1 or not compare_digest(auth[0], expected):
                    return await reject(401, "unauthorized")
            if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                origins = headers.get(b"origin", [])
                if origins and (len(origins) != 1 or origins[0].decode("latin1") not in self.settings.allowed_origins):
                    return await reject(403, "untrusted_origin")

        # Enforce the cap while receiving, including chunked requests. No partial
        # request is forwarded to the parser or persisted on rejection.
        lengths = headers.get(b"content-length", [])
        try:
            if len(lengths) > 1 or (lengths and int(lengths[0]) < 0):
                return await reject(400, "invalid_content_length")
            if lengths and int(lengths[0]) > MAX_BODY_BYTES:
                return await reject(413, "request_too_large")
        except ValueError:
            return await reject(400, "invalid_content_length")
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return await reject(413, "request_too_large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        response_started = False

        async def guarded_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, replay, guarded_send)
        except Exception:
            # Catch inside Starlette's server-error layer so an exception with
            # submitted content is not re-raised into the server's access logs.
            if not response_started:
                await JSONResponse(status_code=500, content={"error": {
                    "code": "internal_error", "correlation_id": uuid4().hex,
                }})(scope, replay, send)


def seed_demo_data(database_url: str) -> bool:
    """Load the synthetic demo dataset into a test-mode database, when it is installed."""
    if importlib.util.find_spec("audit.demo") is None:
        return False
    importlib.import_module("audit.demo").seed(database_url)
    return True


def _result_secrets(settings: Settings) -> tuple[str, ...]:
    """Private refusal inventory; never expose settings or wallet parsing errors."""
    values = [settings.api_auth_token, settings.model_api_key, settings.archive_wallet_path]
    # Same source resolution as ArweaveWalletAuth: stripped inline JSON wins, else the file.
    wallet = (settings.archive_wallet_json or "").strip()
    if not wallet and settings.archive_wallet_path:
        try:
            wallet = Path(settings.archive_wallet_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            wallet = ""
    if wallet:
        values.append(wallet)
        try:
            jwk = json.loads(wallet)
        except ValueError:
            jwk = None
        if isinstance(jwk, dict):
            values.extend(jwk[key] for key in ("d", "p", "q", "dp", "dq", "qi")
                          if isinstance(jwk.get(key), str))
    # SearXNG's server secret is not read by this service.
    return tuple(value for value in values if value)


def create_app(settings: Settings | None = None, engine=None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.api_auth_token and len(settings.api_auth_token) < 32:
        raise ValueError("API_AUTH_TOKEN must contain at least 32 characters")
    if settings.max_concurrent_runs < 1:
        raise ValueError("MAX_CONCURRENT_RUNS must be positive")
    result_secrets = _result_secrets(settings)
    test_mode = settings.mode == "test"
    if engine is None and test_mode:
        from audit.demo_engine import DemoEngine
        engine = DemoEngine()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(settings.database_url)
        if not test_mode and store.has_demo_data():
            # Fail closed: a copied or restored demo database never receives real runs.
            store.close()
            raise RuntimeError("Live mode must not use a database holding demo data")
        app.state.store = store
        app.state.tasks = set()
        app.state.run_tasks = {}
        app.state.operator_cancels = set()
        app.state.cancel_lock = asyncio.Lock()
        app.state.queue_paused = False
        app.state.queue_open = asyncio.Event()
        app.state.queue_open.set()
        app.state.semaphore = asyncio.Semaphore(settings.max_concurrent_runs)
        audit_engine = engine or AuditEngine(settings.engine_config())
        app.state.audit_engine = audit_engine
        try:
            store.sync_prompt_defaults(DEFAULTS)
            app.state.prompt_lock = asyncio.Lock()
            app.state.article_lock = asyncio.Lock()
            app.state.article_fill_lock = asyncio.Lock()
            app.state.article_import_lock = asyncio.Lock()
            app.state.article_fill_attempts = set()
            store.interrupt_incomplete()
            if test_mode:
                # After interrupt_incomplete, so demo runs keep their queued/running state.
                seed_demo_data(settings.database_url)
            await audit_engine.start()
            app.state.model_assignments = store.model_assignments()
            app.state.model_defaults = models_from_config({**audit_engine.public_config,
                "model_provider": settings.model_provider, "response_format": settings.response_format})
            app.state.workflow_base = build_base(audit_engine.public_config)
            app.state.workflow = store.save_workflow(with_prompts(app.state.workflow_base, active_texts(), current_models()))
            yield
        finally:
            tasks = list(app.state.tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await audit_engine.close()
            store.close()

    app = FastAPI(title="Karakutu AI Audit", version="0.1.0", lifespan=lifespan,
                  responses={code: {"model": SafeErrorResponse} for code in
                             (400, 401, 403, 404, 409, 413, 422, 500)})
    app.state.settings = settings
    app.add_middleware(RequestGuards, settings=settings)

    def active_texts():
        return {name: value["text"] for name, value in app.state.store.active_prompts().items()}

    @app.exception_handler(PromptStoreError)
    async def prompt_store_error(_request: Request, exc: PromptStoreError):
        code = exc.args[0]
        return JSONResponse(status_code=404 if code == "not_found" else 409,
                            content={"error": {"code": code}})

    @app.exception_handler(ArticleStoreError)
    async def article_store_error(_request: Request, exc: ArticleStoreError):
        status = {"not_found": 404, "article_has_no_content": 422}.get(exc.code, 409)
        error = {"code": exc.code}
        if exc.run_id:
            error["run_id"] = exc.run_id
        return JSONResponse(status_code=status, content={"error": error})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Pydantic's default errors include submitted values and exception context.
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "fields": [
            {"loc": list(error["loc"]), "type": error["type"], "code": "invalid_value"}
            for error in exc.errors()
        ]}})

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        codes = {404: "not_found", 401: "unauthorized", 403: "forbidden"}
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": codes.get(exc.status_code, "request_failed")}})

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _exc: Exception):
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "correlation_id": uuid4().hex}})

    @app.get("/health", response_model=Health)
    async def health():
        return {"status": "ok", "mode": settings.mode}

    @app.get("/api/status", response_model=ServiceStatus)
    async def status():
        return {"service": "karakutu-ai-audit", "mode": settings.mode, "model_enabled": settings.model_enabled,
                "model_name": app.state.audit_engine.public_config.get("model_name", settings.model_name),
                "model_provider": settings.model_provider, "response_format": settings.response_format,
                "search_provider": settings.search_provider,
                "search": await search_state(),
                "archive_reads_enabled": bool(settings.archive_base_url),
                "archive": archive_state(),
                "live_ingestion_enabled": bool(settings.live_ingestion_enabled and settings.api_auth_token),
                "writes_enabled": False, "prompts_editable": bool(settings.api_auth_token)}

    def current_overrides(assignments=None):
        return {**settings.model_overrides(), **(app.state.model_assignments if assignments is None else assignments)}

    def current_models(assignments=None):
        defaults = app.state.model_defaults
        return {**defaults, "steps": {step: model or defaults["default"]
                for step, model in current_overrides(assignments).items()}}

    async def available_models():
        if not settings.model_enabled:
            return [], None
        return await app.state.audit_engine.available_models()

    @app.get("/api/models", response_model=ModelList)
    async def models_list():
        available, error = await available_models()
        models = current_models()
        overrides = current_overrides()
        return {"available": available, "error": error, "default": models["default"],
                "steps": {step: {"override": overrides[step], "effective": models["steps"][step],
                    "source": "api" if step in app.state.model_assignments else "config"}
                    for step in MODEL_STEPS}}

    @app.post("/api/models/steps", response_model=ModelAssigned)
    async def assign_model(body: ModelStepInput):
        if body.step not in MODEL_STEPS:
            return JSONResponse(status_code=422, content={"error": {"code": "unknown_step"}})
        available, error = await available_models()
        if (body.model is not None and settings.model_enabled and error is None
                and body.model not in available):
            return JSONResponse(status_code=422, content={"error": {"code": "unknown_model"}})
        async with app.state.prompt_lock:
            assignments = {**app.state.model_assignments, body.step: body.model}
            models = current_models(assignments)
            # Capture prompts before entering the SQLite write transaction.
            texts = active_texts()
            snapshot = app.state.store.assign_model(body.step, body.model, body.author, body.note,
                lambda: with_prompts(app.state.workflow_base, texts, models))
            app.state.model_assignments = assignments
            app.state.workflow = snapshot
            return {"step": body.step, "override": body.model, "effective": models["steps"][body.step],
                    "source": "api", "workflow_id": snapshot["id"]}

    @app.get("/api/workflow", response_model=WorkflowSnapshot)
    async def workflow():
        return app.state.workflow

    @app.get("/api/workflows/{workflow_id}", response_model=WorkflowSnapshot)
    async def historic_workflow(workflow_id: str):
        result = app.state.store.workflow(workflow_id)
        if result is None:
            raise HTTPException(404)
        return result

    def prompt_spec(name: str):
        if name not in REGISTRY:
            raise HTTPException(404)
        return REGISTRY[name]

    def prompt_version_or_404(name: str, version: int):
        prompt_spec(name)
        result = app.state.store.prompt_version(name, version)
        if result is None:
            raise HTTPException(404)
        return result

    @app.get("/api/prompts", response_model=PromptList)
    async def prompts_list():
        items = []
        for name, spec in REGISTRY.items():
            versions = app.state.store.prompt_versions(name)
            items.append({"name": name, "step": spec.step, "versions": len(versions),
                          "active": next(row for row in versions if row["active"])})
        return {"items": items}

    @app.get("/api/prompts/{name}", response_model=PromptDetail)
    async def prompt_detail(name: str):
        spec = prompt_spec(name)
        versions = app.state.store.prompt_versions(name)
        active = next(row for row in versions if row["active"])
        return {"name": name, "step": spec.step, "required_tokens": spec.required_tokens,
                "active": app.state.store.prompt_version(name, active["version"]),
                "versions": versions, "activations": app.state.store.prompt_activations(name)}

    @app.get("/api/prompts/{name}/versions/{version}", response_model=PromptVersion)
    async def prompt_version(name: str, version: int):
        return prompt_version_or_404(name, version)

    @app.post("/api/prompts/{name}/versions", status_code=201, response_model=PromptVersion)
    async def create_prompt_version(name: str, body: PromptVersionInput):
        prompt_spec(name)
        codes = validation_codes(name, body.text)
        if codes:
            return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "codes": codes}})
        return app.state.store.create_prompt_version(name, **body.model_dump())

    async def activate_prompt(name: str, body: PromptAuthorInput, version: int | None, kind: str):
        prompt_spec(name)
        async with app.state.prompt_lock:
            active, snapshot = app.state.store.activate_prompt(
                name, version, kind, body.author, body.note,
                lambda texts: with_prompts(app.state.workflow_base, texts, current_models()))
            app.state.workflow = snapshot
            return {"name": name, "active": active, "workflow_id": app.state.workflow["id"]}

    @app.post("/api/prompts/{name}/activate", response_model=PromptActivated)
    async def prompt_activate(name: str, body: PromptActivateInput):
        return await activate_prompt(name, body, body.version, "activate")

    @app.post("/api/prompts/{name}/rollback", response_model=PromptActivated)
    async def prompt_rollback(name: str, body: PromptAuthorInput):
        return await activate_prompt(name, body, None, "rollback")

    @app.get("/api/prompts/{name}/diff", response_model=PromptDiff)
    async def prompt_diff(name: str, from_version: int = Query(alias="from"), to_version: int = Query(alias="to")):
        before = prompt_version_or_404(name, from_version)
        after = prompt_version_or_404(name, to_version)
        diff = unified_prompt_diff(before["text"], after["text"],
                                   f"{name}/v{from_version}", f"{name}/v{to_version}")
        return {"name": name, "from_version": from_version, "to_version": to_version, "diff": diff}

    @app.get("/api/runs", response_model=RunList)
    async def runs(category: Literal["benchmark", "live"] | None = None,
                   limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                   verdict: Verdict | None = None, workflow_id: str | None = None,
                   status: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = None,
                   origin: Literal["benchmark", "api", "webhook", "article"] | None = None):
        return app.state.store.runs(category=category, limit=limit, offset=offset,
                                    verdict=verdict.value if verdict else None, workflow_id=workflow_id,
                                    status=status, origin=origin)

    def run_or_404(run_id: str) -> dict:
        result = app.state.store.run(run_id)
        if result is None:
            raise HTTPException(404)
        return result

    def lean_result(run):
        previous = app.state.store.previous_record_run_id(run)
        lineage = {"kind": "re_audit", "previous_record_id": previous + ".r0"} if previous else None
        try:
            return build_result(run, lineage=lineage, secret_values=result_secrets)
        except RecordError as exc:
            return JSONResponse(status_code=409, content={"error": {
                "code": "result_withheld", "reason": exc.code, "run_id": run["id"],
            }})

    result_responses = {409: {
        "model": SafeErrorResponse,
        "description": "Result withheld; the saved run and its details remain available. "
                       "Submissions remain queued and cancellations remain effective.",
    }}

    @app.get("/api/runs/{run_id}", response_model=AuditResult, responses=result_responses)
    async def run_result(run_id: str):
        return lean_result(run_or_404(run_id))

    @app.get("/api/runs/{run_id}/details", response_model=RunDetail)
    async def run_detail(run_id: str):
        return run_or_404(run_id)

    @app.get("/api/runs/{run_id}/export", response_model=RunDetail,
             responses={200: {"description": "Complete run JSON attachment, including evidence and reviews.",
                              "headers": {"Content-Disposition": {"schema": {"type": "string"}}}}})
    async def export_run(run_id: str):
        result = run_or_404(run_id)
        return Response(content=json.dumps(RunDetail.model_validate(result).model_dump(mode="json"), ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{result["id"]}.json"'})

    async def execute_run(run_id: str, request: AuditRequest, definition: dict):
        store = app.state.store
        try:
            while True:
                await app.state.queue_open.wait()
                async with app.state.semaphore:
                    # A pause can arrive while this task waits for a slot.
                    if app.state.queue_paused:
                        continue
                    if not store.start_run(run_id):
                        return

                    async def on_step(step):
                        store.append_step(run_id, step)

                    result = await app.state.audit_engine.run(
                        request, on_step=on_step, prompts=definition["prompts"],
                        models=definition["models"])
                    store.complete_run(run_id, result)
                    return
        except asyncio.CancelledError:
            if run_id in app.state.operator_cancels:
                store.cancel_run(run_id)
            else:
                store.fail_run(run_id, "interrupted")
            raise
        except Exception:
            # Never store or log raw exception text; model/server exceptions can
            # contain article content, prompts or authorization information.
            store.fail_run(run_id)

    def submit(body: RunInput | None, category: str, origin: str, article_ref: str | None = None):
        workflow = app.state.workflow
        run = app.state.store.create_run(
            workflow_id=workflow["id"], category=category, origin=origin,
            input=body.model_dump(mode="json", include={"removed_article", "similar_articles"}) if body else {},
            label=body.label if body else None,
            expected_verdict=body.expected_verdict.value if body and body.expected_verdict else None,
            synthetic=not settings.model_enabled, article_ref=article_ref)
        request = AuditRequest.model_validate(run["input"])
        task = asyncio.create_task(execute_run(run["id"], request, workflow["definition"]))
        app.state.tasks.add(task)
        app.state.run_tasks[run["id"]] = task

        def task_finished(completed):
            app.state.tasks.discard(completed)
            app.state.run_tasks.pop(run["id"], None)
            app.state.operator_cancels.discard(run["id"])
            if not completed.cancelled():
                # Retrieve even a persistence failure so asyncio cannot print a
                # raw SQL exception containing article JSON to the server log.
                completed.exception()

        task.add_done_callback(task_finished)
        return lean_result(run)

    async def cancel_active_run(run_id: str) -> dict | None:
        # Serialize overlapping individual/bulk stops so only one request wins.
        async with app.state.cancel_lock:
            run = run_or_404(run_id)
            if run["status"] not in {"queued", "running"}:
                return None
            task = app.state.run_tasks.get(run_id)
            if task is not None:
                app.state.operator_cancels.add(run_id)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            # Also handles seed rows and tasks cancelled before their first turn.
            app.state.store.cancel_run(run_id)
            app.state.operator_cancels.discard(run_id)
            run = run_or_404(run_id)
            return run if run["status"] == "cancelled" else None

    @app.post("/api/runs/{run_id}/cancel", response_model=AuditResult,
             responses=result_responses)
    async def cancel_run(run_id: str):
        run = await cancel_active_run(run_id)
        if run is None:
            return JSONResponse(status_code=409, content={"error": {"code": "run_not_active"}})
        return lean_result(run)

    def queue_state() -> dict:
        return {"paused": app.state.queue_paused,
                "max_concurrent_runs": settings.max_concurrent_runs,
                **app.state.store.active_runs()}

    @app.get("/api/queue", response_model=QueueState)
    async def queue():
        return queue_state()

    @app.post("/api/queue/pause", response_model=QueueState)
    async def pause_queue():
        app.state.queue_paused = True
        app.state.queue_open.clear()
        return queue_state()

    @app.post("/api/queue/resume", response_model=QueueState)
    async def resume_queue():
        app.state.queue_paused = False
        app.state.queue_open.set()
        return queue_state()

    @app.post("/api/queue/cancel", response_model=QueueCancelled)
    async def cancel_queue(include_running: bool = False):
        active = app.state.store.active_runs()
        targets = active["queued"] + (active["running"] if include_running else [])
        cancelled = []
        for run_id in targets:
            if await cancel_active_run(run_id) is not None:
                cancelled.append(run_id)
        return {"cancelled": cancelled}

    async def search_state():
        # Stay offline even if an injected test engine exposes a live client.
        if test_mode or settings.search_provider == "disabled":
            return "disabled"
        client = getattr(app.state.audit_engine, "search", None)
        return await client.probe() if client else "unchecked"

    def archive_client():
        # Test mode must stay offline even if an injected engine exposes a client.
        if test_mode:
            return None
        return getattr(app.state.audit_engine, "archive", None)

    def archive_state():
        client = archive_client()
        return getattr(client, "state", "unchecked") if client and client.enabled else "disabled"

    def archive_unavailable():
        state = archive_state()
        reason = "not_configured" if state == "disabled" else state
        return JSONResponse(status_code=409, content={
            "error": {"code": "archive_unavailable", "reason": reason}})

    def article_or_404(ref):
        result = app.state.store.article(ref)
        if result is None:
            raise HTTPException(404)
        return result

    @app.get("/api/articles", response_model=ArticleList)
    async def articles(status: ArticleStatus | None = None, verdict: Verdict | None = None,
                       category: Literal["live", "benchmark"] | None = None,
                       q: str | None = Query(default=None, max_length=200),
                       limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
        return app.state.store.articles(status=status, verdict=verdict.value if verdict else None,
                                         category=category, q=q, limit=limit, offset=offset)

    @app.post("/api/articles/import", response_model=ArticleImported,
              responses={409: {"model": SafeErrorResponse}})
    async def import_articles(days: int = Query(default=30, ge=1, le=90)):
        client = archive_client()
        if client is None or not client.enabled:
            return archive_unavailable()
        # One import at a time, whatever client starts it: a second request would only fetch
        # the same archive listing again while the first is still writing.
        lock = app.state.article_import_lock
        if lock.locked():
            return JSONResponse(status_code=409, content={"error": {"code": "import_in_progress"}})
        async with lock:
            # The archive's audit listing filters on Unix seconds, never ISO dates.
            end = int(datetime.now(timezone.utc).timestamp())
            try:
                items = await client.audits_created_between(end - days * 86400, end)
            except Exception:
                items = None
            if items is None:
                return archive_unavailable()
            return app.state.store.import_articles(items)

    @app.get("/api/articles/{ref}", response_model=ArticleDetail)
    async def article_detail(ref: str):
        result = article_or_404(ref)
        client = archive_client()
        if ref.startswith("ka-") and not result["content"] and client is not None and client.enabled:
            async with app.state.article_fill_lock:
                identity = (ref, result["url"])
                if identity not in app.state.article_fill_attempts:
                    app.state.article_fill_attempts.add(identity)
                    try:
                        record = await client.get_article(result["archive_id"] or ref[3:])
                        if record is not None:
                            app.state.store.fill_article(ref, record, expected_url=result["url"],
                                                         expected_archive_id=result["archive_id"])
                    except Exception:
                        # Return the saved metadata without exposing upstream content/errors.
                        pass
            result = article_or_404(ref)
        result["links"] = {"original_url": result["url"], "archive_url": None}
        return result

    @app.post("/api/articles/{ref}/audit", status_code=202, response_model=AuditResult,
             responses=result_responses)
    async def article_audit(ref: str):
        async with app.state.article_lock:
            article = article_or_404(ref)
            if article["category"] == "live" and not test_mode and not (settings.live_ingestion_enabled and settings.api_auth_token):
                raise HTTPException(404)
            # Store re-reads the article and checks for active work under the database writer lock.
            return submit(None, article["category"], "article", article_ref=ref)

    @app.post("/api/benchmarks/runs", status_code=202, response_model=AuditResult,
             responses=result_responses)
    async def benchmark(body: RunInput):
        return submit(body, "benchmark", "benchmark")

    @app.post("/api/audit", status_code=202, response_model=AuditResult,
             responses=result_responses)
    async def audit(body: RunInput):
        return submit(body, "live", "api")

    @app.post("/api/webhooks/audit", status_code=202, response_model=AuditResult,
             responses=result_responses)
    async def webhook(body: RunInput):
        return submit(body, "live", "webhook")

    @app.post("/api/runs/{run_id}/reactions", status_code=201, response_model=Reaction)
    async def reaction(run_id: str, body: ReactionInput):
        result = app.state.store.add_reaction(run_id, body.model_dump(mode="json"))
        if result is None:
            raise HTTPException(404)
        return result

    return app


app = create_app()
