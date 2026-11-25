"""Network-free API checks with fabricated inputs and an injected deterministic engine."""
import asyncio
import json
from copy import deepcopy
import time

from fastapi.testclient import TestClient
import pytest

from audit.api import _result_secrets, create_app
from audit.config import Settings
from audit.db import Store


TOKEN = "fabricated-test-token-0123456789abcdef"
BODY = {"removed_article": {"url": "https://fabricated.invalid/story", "title": "Invented benchmark article", "content": "A completely fabricated article used only for isolated API testing."}, "similar_articles": [], "label": "Fabricated case", "expected_verdict": "INCONCLUSIVE"}
AUTH = {"Authorization": "Bearer " + TOKEN}


class FakeEngine:
    public_config = {"model_enabled": False, "model_name": "fabricated", "search_provider": "disabled"}

    def __init__(self, fail=False, wait=False):
        self.fail = fail
        self.wait = wait
        self.release = asyncio.Event()
        self.started = False
        self.closed = False
        self.active = 0
        self.peak = 0
        self.received_prompts = []
        self.received_models = []

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def run(self, request, on_step=None, prompts=None, models=None):
        self.received_prompts.append(deepcopy(prompts))
        self.received_models.append(deepcopy(models))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            step = {"name": "classification", "status": "completed", "model": None, "input": {"article": request.removed_article.model_dump(mode="json")}, "output": {}, "model_calls": [], "evidence": [], "duration_ms": 1}
            await on_step(step)
            if self.wait:
                await self.release.wait()
            await asyncio.sleep(0.01)
            if self.fail:
                raise RuntimeError("PRIVATE submitted content and secret authorization")
            return {"response": {"verdict": "INCONCLUSIVE", "stages": [], "findings": "Fabricated test output", "flags": [], "short_circuited_at": None, "classification": None}, "steps": [step], "model": {"name": "fabricated"}, "synthetic": True}
        finally:
            self.active -= 1


def settings(tmp_path, **kwargs):
    return Settings(data_dir=tmp_path, **kwargs)


def finished(client, run_id):
    for _ in range(100):
        run = client.get(f"/api/runs/{run_id}/details", headers=AUTH).json()
        if run["status"] in {"succeeded", "failed", "cancelled"}:
            return run
        time.sleep(0.01)
    pytest.fail("background run did not finish")


def test_benchmark_workflow_reaction_export_and_restart(tmp_path, database_url):
    config = settings(tmp_path, database_url=database_url)
    engine = FakeEngine()
    with TestClient(create_app(config, engine)) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "live"}
        workflow = client.get("/api/workflow").json()
        sources = workflow["definition"]["sources"]
        assert {"engine.py", "domain.py", "model.py", "workflow/prompts.py", "workflow/pipeline.py", "search/fetch.py"} <= set(sources)
        assert "class AuditEngine" in sources["engine.py"]
        assert "api_auth_token" not in workflow["definition"]["settings"]
        assert client.get("/api/workflows/" + workflow["id"]).json() == workflow
        response = client.post("/api/benchmarks/runs", json=BODY)
        assert response.status_code == 202
        queued = response.json()["run"]
        assert queued["status"] == "queued"
        assert queued["category"] == "benchmark"
        run = finished(client, queued["id"])
        assert run["status"] == "succeeded"
        assert run["steps"][0]["name"] == "classification"
        assert run["expected_verdict"] == "INCONCLUSIVE"
        assert run["input"]["removed_article"]["title"] == BODY["removed_article"]["title"]
        first = client.post(f'/api/runs/{run["id"]}/reactions', json={"reaction": "needs_review", "reviewer": "Fabricated reviewer", "note": "Invented note"})
        assert first.status_code == 201
        assert first.json()["workflow_id"] == workflow["id"]
        second = client.post(f'/api/runs/{run["id"]}/reactions', json={"reaction": "agree", "reviewer": "Fabricated reviewer"})
        assert second.json()["id"] != first.json()["id"]
        exported = client.get(f'/api/runs/{run["id"]}/export')
        assert exported.headers["content-disposition"].startswith("attachment;")
        assert len(exported.json()["reactions"]) == 2
        assert client.get("/api/runs?category=live").json()["total"] == 0
        assert client.get("/api/runs?category=benchmark&verdict=INCONCLUSIVE&workflow_id=" + workflow["id"]).json()["total"] == 1
        assert client.get("/api/runs?offset=1").json()["items"] == []
    assert engine.closed
    changed_engine = FakeEngine()
    changed_engine.public_config = {**FakeEngine.public_config, "model_name": "alternate-fabricated-model"}
    with TestClient(create_app(config, changed_engine)) as client:
        assert client.get(f'/api/runs/{run["id"]}/details').json() == exported.json()
        assert client.get("/api/workflow").json()["id"] != workflow["id"]
        assert client.get("/api/workflows/" + workflow["id"]).json() == workflow


@pytest.mark.parametrize("route", ["/api/audit", "/api/webhooks/audit"])
def test_live_disabled_precedes_auth_and_validation(tmp_path, route, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())) as client:
        response = client.post(route, content="not-json", headers={"Authorization": "Bearer wrong", "Origin": "https://untrusted.invalid"})
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found"}}
        assert client.post(route + "/", content="not-json").status_code == 404


def test_auth_precedes_validation_and_protects_all_api_routes(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, live_ingestion_enabled=True), FakeEngine())) as client:
        for path in ["/api/status", "/api/workflow", "/api/workflows/missing", "/api/runs", "/api/runs/missing", "/api/runs/missing/details", "/api/runs/missing/export"]:
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        for path in ["/api/audit", "/api/webhooks/audit", "/api/benchmarks/runs", "/api/runs/missing/reactions"]:
            assert client.post(path, content="invalid-json").status_code == 401
            assert client.post(path, content="invalid-json", headers=AUTH).status_code == 422
        assert client.get("/health").status_code == 200
        assert client.get("/api/status", headers=AUTH).json()["writes_enabled"] is False
        assert client.get("/api/status", headers=AUTH).json()["mode"] == "live"
        assert client.get("/api/status", headers=AUTH).json()["model_name"] == "fabricated"
        assert client.get("/api/status", headers=AUTH).json()["live_ingestion_enabled"] is True
        assert client.get("/api/status").headers["cache-control"] == "no-store"
        assert client.get("/api/status", headers=AUTH).headers["cache-control"] == "no-store"
        assert client.get("/api/runs", headers=[("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)]).status_code == 401


def test_live_category_and_origin_are_server_assigned(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, live_ingestion_enabled=True), FakeEngine())) as client:
        for route, origin in [("/api/audit", "api"), ("/api/webhooks/audit", "webhook"), ("/api/benchmarks/runs", "benchmark")]:
            response = client.post(route, json=BODY, headers=AUTH)
            assert response.status_code == 202
            assert response.json()["run"]["origin"] == origin
            assert response.json()["run"]["category"] == ("benchmark" if origin == "benchmark" else "live")
            finished(client, response.json()["run"]["id"])
        assert client.get("/api/runs?category=live", headers=AUTH).json()["total"] == 2
        for query, total in [
            ("status=succeeded", 3),
            ("origin=api", 1),
            ("status=succeeded&origin=api", 1),
            ("category=live&status=succeeded&origin=api", 1),
            ("category=benchmark&status=succeeded&origin=api", 0),
            ("status=failed&origin=api", 0),
            ("status=succeeded&origin=webhook", 1),
        ]:
            response = client.get("/api/runs?" + query, headers=AUTH)
            assert response.status_code == 200
            assert response.json()["total"] == total
            assert len(response.json()["items"]) == total
        spoofed = {**BODY, "category": "live", "origin": "webhook", "workflow_id": "invented"}
        assert client.post("/api/benchmarks/runs", json=spoofed, headers=AUTH).status_code == 422


def test_safe_validation_body_limits_and_origin(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        bad = deepcopy(BODY)
        bad["removed_article"]["url"] = "PRIVATE submitted sensitive value"
        response = client.post("/api/benchmarks/runs", json=bad)
        assert response.status_code == 422
        assert "PRIVATE" not in response.text
        assert set(response.json()["error"]["fields"][0]) == {"loc", "type", "code"}
        bad["removed_article"]["url"] = "file:///private/secret"
        assert client.post("/api/benchmarks/runs", json=bad).status_code == 422
        bad = deepcopy(BODY)
        bad["similar_articles"] = [BODY["removed_article"]] * 6
        assert client.post("/api/benchmarks/runs", json=bad).status_code == 422
        for query in ["limit=201", "limit=0", "offset=-1", "category=unknown", "verdict=unknown",
                      "status=unknown", "origin=unknown"]:
            assert client.get("/api/runs?" + query).status_code == 422
        assert client.post("/api/benchmarks/runs", content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
        assert client.post("/api/benchmarks/runs", json=BODY, headers={"Origin": "https://untrusted.invalid"}).status_code == 403
        assert client.post("/api/benchmarks/runs", json=BODY, headers={"Origin": "null"}).status_code == 403
        assert client.post("/api/benchmarks/runs", json=BODY, headers={"Origin": "http://localhost:2201"}).status_code == 202
        assert client.options("/api/benchmarks/runs", headers={"Origin": "https://untrusted.invalid", "Access-Control-Request-Method": "POST"}).headers.get("access-control-allow-origin") is None


def test_failure_retains_completed_steps_and_safe_error(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine(fail=True))) as client:
        run_id = client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
        run = finished(client, run_id)
        assert run["status"] == "failed"
        assert run["output"] is None
        assert len(run["steps"]) == 1
        assert run["error"]["code"] == "audit_failed"
        assert "PRIVATE" not in str(run)
        assert "authorization" not in str(run)


def test_cancellation_marks_interrupted_and_concurrency_is_bounded(tmp_path, database_url):
    engine = FakeEngine(wait=True)
    config = settings(tmp_path, database_url=database_url, max_concurrent_runs=1)
    with TestClient(create_app(config, engine)) as client:
        ids = [client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"] for _ in range(3)]
        assert engine.peak == 1
        assert client.get("/api/runs").json()["total"] == 3
        assert client.get(f"/api/runs/{ids[0]}/details").json()["status"] == "running"
        assert client.get(f"/api/runs/{ids[1]}/details").json()["status"] == "queued"
    with TestClient(create_app(config, FakeEngine())) as client:
        for run_id in ids:
            assert client.get(f"/api/runs/{run_id}/details").json()["error"]["code"] == "interrupted"


def test_missing_run_reaction_and_invalid_reviewer(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        assert client.get("/api/runs/missing/details").status_code == 404
        assert client.get("/api/runs/missing/export").status_code == 404
        assert client.get("/api/workflows/missing").status_code == 404
        assert client.post("/api/runs/missing/reactions", json={"reaction": "agree", "reviewer": "Fabricated"}).status_code == 404
        assert client.post("/api/runs/missing/reactions", json={"reaction": "agree", "reviewer": "   "}).status_code == 422


def test_unexpected_server_exception_has_safe_response_and_no_traceback(tmp_path, caplog, database_url):
    app = create_app(settings(tmp_path, database_url=database_url), FakeEngine())

    @app.get("/api/fabricated-error")
    async def fabricated_error():
        raise RuntimeError("PRIVATE sensitive body in raw exception")

    with TestClient(app) as client:
        response = client.get("/api/fabricated-error")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert response.json()["error"]["correlation_id"]
        assert "PRIVATE" not in response.text
        assert "PRIVATE" not in caplog.text


@pytest.mark.asyncio
async def test_streamed_body_limit_enforced_without_content_length(tmp_path, database_url):
    app = create_app(settings(tmp_path, database_url=database_url), FakeEngine())
    sent = []
    chunks = iter([b"x" * (1024 * 1024), b"y" * (1024 * 1024), b"z"])

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "path": "/api/benchmarks/runs", "method": "POST", "headers": [(b"host", b"testserver")]}, receive, send)
    assert sent[0]["status"] == 413
    assert b"request_too_large" in sent[1]["body"]


@pytest.mark.parametrize("host", ["attacker.invalid", "localhost.attacker.invalid", "user@localhost:2200", "localhost:invalid", "localhost/ignored"])
def test_local_api_rejects_untrusted_hosts(tmp_path, host, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        response = client.get("/api/runs", headers={"Host": host})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "untrusted_host"
        assert response.headers["cache-control"] == "no-store"


def test_openapi_documents_typed_run_workflow_and_reaction_outputs(tmp_path, database_url):
    app = create_app(settings(tmp_path, database_url=database_url), FakeEngine())
    schema = app.openapi()
    expected = [
        ("/health", "get", "200", "Health"),
        ("/api/status", "get", "200", "ServiceStatus"),
        ("/api/workflow", "get", "200", "WorkflowSnapshot"),
        ("/api/workflows/{workflow_id}", "get", "200", "WorkflowSnapshot"),
        ("/api/runs", "get", "200", "RunList"),
        ("/api/runs/{run_id}", "get", "200", "AuditResult"),
        ("/api/runs/{run_id}/details", "get", "200", "RunDetail"),
        ("/api/runs/{run_id}/export", "get", "200", "RunDetail"),
        ("/api/benchmarks/runs", "post", "202", "AuditResult"),
        ("/api/audit", "post", "202", "AuditResult"),
        ("/api/articles/{ref}/audit", "post", "202", "AuditResult"),
        ("/api/runs/{run_id}/cancel", "post", "200", "AuditResult"),
        ("/api/webhooks/audit", "post", "202", "AuditResult"),
        ("/api/runs/{run_id}/reactions", "post", "201", "Reaction"),
    ]
    for path, method, code, name in expected:
        response = schema["paths"][path][method]["responses"][code]
        assert response["content"]["application/json"]["schema"] == {"$ref": f"#/components/schemas/{name}"}
    components = schema["components"]["schemas"]
    assert {"RunStory", "RunDetail", "RunList", "RunStep", "AuditResponse", "WorkflowDefinition", "WorkflowSnapshot", "Reaction", "ServiceStatus", "Health"} <= set(components)
    assert components["RunList"]["properties"]["items"]["items"]["$ref"].endswith("/RunSummary")
    detail = components["RunDetail"]["properties"]
    assert {"input", "output", "steps", "error", "model", "workflow_id", "expected_verdict", "reactions"} <= set(detail)
    assert {"$ref": "#/components/schemas/AuditResponse"} in detail["output"]["anyOf"]
    assert {"name", "status", "input", "output", "model_calls", "evidence", "duration_ms"} <= set(components["RunStep"]["required"])
    assert {"sources", "settings", "steps", "edges", "dependencies", "decision"} <= set(components["WorkflowDefinition"]["required"])
    assert components["ServiceStatus"]["properties"]["writes_enabled"]["const"] is False
    assert "Content-Disposition" in schema["paths"]["/api/runs/{run_id}/export"]["get"]["responses"]["200"]["headers"]


def test_response_validation_failure_is_safe_and_422_contract_matches_handler(tmp_path, caplog, database_url):
    from audit.schemas import Health

    app = create_app(settings(tmp_path, database_url=database_url), FakeEngine())

    @app.get("/api/fabricated-invalid-response", response_model=Health)
    async def invalid_response():
        return {"status": "PRIVATE sensitive model output"}

    with TestClient(app) as client:
        response = client.get("/api/fabricated-invalid-response")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "PRIVATE" not in response.text
        assert "PRIVATE" not in caplog.text
    documented = app.openapi()["paths"]["/api/benchmarks/runs"]["post"]["responses"]["422"]
    assert documented["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/SafeErrorResponse"}


def test_configured_host_and_origin_keep_guards_and_auth(tmp_path, database_url):
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN,
                      allowed_hosts=("Audit.Example.Test",),
                      allowed_origins=("HTTPS://Audit.Example.Test:2201",))
    with TestClient(create_app(config, FakeEngine())) as client:
        headers = {**AUTH, "Host": "audit.example.test:2201"}
        assert client.get("/api/runs", headers=headers).status_code == 200
        assert client.get("/api/runs", headers={"Host": "audit.example.test"}).status_code == 401
        response = client.get("/api/runs", headers={**AUTH, "Host": "unlisted.example.test"})
        assert response.status_code == 400
        assert response.json() == {"error": {"code": "untrusted_host"}}
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/api/benchmarks/runs", json=BODY, headers={
            **headers, "Origin": "https://audit.example.test:2201",
        }).status_code == 202
        response = client.post("/api/benchmarks/runs", json=BODY, headers={
            **headers, "Origin": "https://unlisted.example.test:2201",
        })
        assert response.status_code == 403
        assert response.json() == {"error": {"code": "untrusted_origin"}}
        assert response.headers["cache-control"] == "no-store"


def test_empty_allowed_environment_preserves_local_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "")
    monkeypatch.setenv("ALLOWED_ORIGINS", "")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(create_app(Settings.from_env(), FakeEngine())) as client:
        for host in ["localhost:2200", "127.0.0.1:2201", "testserver"]:
            assert client.get("/health", headers={"Host": host}).status_code == 200
        for origin in Settings().allowed_origins:
            assert client.post("/api/benchmarks/runs", json=BODY,
                               headers={"Origin": origin}).status_code == 202
        assert client.get("/health", headers={"Host": "audit.example.test"}).status_code == 400
        assert client.post("/api/benchmarks/runs", json=BODY, headers={
            "Origin": "https://audit.example.test",
        }).status_code == 403


def test_configured_origin_matches_browser_serialization_of_ports(tmp_path, database_url):
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, allowed_hosts=("audit.example.test",),
                      allowed_origins=("https://audit.example.test:443",
                                       "http://audit.example.test:80",
                                       "https://audit.example.test:08443"))
    assert config.allowed_origins >= {"https://audit.example.test", "http://audit.example.test",
                                      "https://audit.example.test:8443"}
    with TestClient(create_app(config, FakeEngine())) as client:
        headers = {**AUTH, "Host": "audit.example.test"}
        for origin in ["https://audit.example.test", "http://audit.example.test",
                       "https://audit.example.test:8443"]:
            assert client.post("/api/benchmarks/runs", json=BODY,
                               headers={**headers, "Origin": origin}).status_code == 202
        assert client.post("/api/benchmarks/runs", json=BODY, headers={
            **headers, "Origin": "https://audit.example.test:08443",
        }).status_code == 403


@pytest.mark.parametrize("host", ["[::1]:2200", "[0:0:0:0:0:0:0:1]:2200", "[0::1]"])
def test_equivalent_ipv6_hosts_are_canonicalized(tmp_path, host, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url, allowed_hosts=("::1",)), FakeEngine())) as client:
        assert client.get("/health", headers={"Host": host}).status_code == 200
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        response = client.get("/health", headers={"Host": host})
        assert response.status_code == 400
        assert response.json() == {"error": {"code": "untrusted_host"}}


@pytest.mark.parametrize("configured", ["http://[::ffff:192.0.2.1]:2201", "http://[::FFFF:C000:201]:2201"])
def test_ipv6_origins_use_browser_serialization(tmp_path, configured, database_url):
    config = settings(tmp_path, database_url=database_url, allowed_origins=(configured, "http://[0:0:0:0:0:0:0:1]:2201",
                                                 "http://[2001:db8:0:0:1:0:0:1]"))
    assert config.allowed_origins >= {"http://[::ffff:c000:201]:2201", "http://[::1]:2201",
                                      "http://[2001:db8::1:0:0:1]"}
    with TestClient(create_app(config, FakeEngine())) as client:
        for origin in ["http://[::ffff:c000:201]:2201", "http://[::1]:2201"]:
            assert client.post("/api/benchmarks/runs", json=BODY,
                               headers={"Origin": origin}).status_code == 202
        assert client.post("/api/benchmarks/runs", json=BODY, headers={
            "Origin": "http://[::ffff:192.0.2.1]:2201",
        }).status_code == 403


def test_test_mode_reports_mode_and_seeds_its_own_database(tmp_path, monkeypatch):
    import audit.api

    seeded = []
    monkeypatch.setattr(audit.api, "seed_demo_data", seeded.append)
    config = settings(tmp_path, mode="test")
    with TestClient(create_app(config, FakeEngine())) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "test"}
        status = client.get("/api/status").json()
        assert status["mode"] == "test"
        assert status["model_enabled"] is False and status["search_provider"] == "disabled"
        assert status["archive_reads_enabled"] is False and status["live_ingestion_enabled"] is False
    assert seeded == [config.database_url]
    assert config.database_url.endswith("/demo/audit-test.sqlite3")
    assert (tmp_path / "demo" / "audit-test.sqlite3").is_file()


def test_live_mode_never_seeds(tmp_path, monkeypatch, database_url):
    import audit.api

    seeded = []
    monkeypatch.setattr(audit.api, "seed_demo_data", seeded.append)
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        assert client.get("/health").json()["mode"] == "live"
    assert seeded == []


def test_seed_demo_data_is_skipped_until_the_dataset_module_exists(monkeypatch):
    import importlib
    import sys
    import types
    import audit.api

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert audit.api.seed_demo_data("fabricated-database-url") is False

    calls = []
    module = types.ModuleType("audit.demo")
    module.seed = calls.append
    monkeypatch.setitem(sys.modules, "audit.demo", module)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert audit.api.seed_demo_data("fabricated-database-url") is True
    assert calls == ["fabricated-database-url"]


def test_prompt_lifecycle_workflow_history_and_restart(tmp_path, database_url):
    from audit.workflow.prompt_registry import DEFAULTS, REGISTRY
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN)
    engine = FakeEngine()
    with TestClient(create_app(config, engine)) as client:
        old = client.get('/api/workflow', headers=AUTH).json()
        listing = client.get('/api/prompts', headers=AUTH).json()['items']
        assert {row['name'] for row in listing} == set(DEFAULTS)
        assert all(row['versions'] == 1 and row['active']['active'] for row in listing)
        for name, text in DEFAULTS.items():
            detail = client.get(f'/api/prompts/{name}', headers=AUTH).json()
            assert detail['active']['text'] == text
            assert detail['required_tokens'] == list(REGISTRY[name].required_tokens)
            assert detail['activations'][0]['kind'] == 'seed'
            assert 'text' not in detail['versions'][0]
        first_run = finished(client, client.post('/api/benchmarks/runs', headers=AUTH, json=BODY).json()["run"]["id"])
        text = DEFAULTS['relevance'] + '\nFabricated API instruction'
        created = client.post('/api/prompts/relevance/versions', headers=AUTH,
                              json={'text': text, 'author': 'Reviewer', 'note': 'Fabricated edit'})
        assert created.status_code == 201
        version = created.json()
        assert version['version'] == 2 and not version['active'] and version['origin'] == 'api'
        assert client.get('/api/workflow', headers=AUTH).json() == old
        active = client.post('/api/prompts/relevance/activate', headers=AUTH, json={'version': 2, 'author': 'Reviewer'})
        assert active.status_code == 200
        new_id = active.json()['workflow_id']
        assert new_id != old['id']
        new = client.get('/api/workflow', headers=AUTH).json()
        assert new['definition']['prompts']['relevance'] == text
        second_run = finished(client, client.post('/api/benchmarks/runs', headers=AUTH, json=BODY).json()["run"]["id"])
        assert first_run['workflow_id'] == old['id']
        assert second_run['workflow_id'] == new_id
        assert engine.received_prompts == [DEFAULTS, {**DEFAULTS, 'relevance': text}]
        for workflow_id in (old['id'], new_id):
            assert client.get('/api/runs', headers=AUTH, params={'workflow_id': workflow_id}).json()['total'] == 1
        diff = client.get('/api/prompts/relevance/diff?from=1&to=2', headers=AUTH).json()
        assert diff['from_version'] == 1 and diff['to_version'] == 2
        assert diff['diff'].startswith('--- relevance/v1\n+++ relevance/v2\n')
        assert '+Fabricated API instruction' in diff['diff']
        assert client.get('/api/prompts/relevance/versions/2', headers=AUTH).json()['text'] == text
        before = client.get('/api/prompts/relevance', headers=AUTH).json()['activations']
        noop = client.post('/api/prompts/relevance/activate', headers=AUTH, json={'version': 2, 'author': 'Reviewer'})
        assert noop.json()['workflow_id'] == new_id
        assert client.get('/api/prompts/relevance', headers=AUTH).json()['activations'] == before
        rollback = client.post('/api/prompts/relevance/rollback', headers=AUTH, json={'author': 'Reviewer'})
        assert rollback.status_code == 200 and rollback.json()['workflow_id'] == old['id']
        detail = client.get('/api/prompts/relevance', headers=AUTH).json()
        assert detail['active']['version'] == 1
        assert [row['version'] for row in detail['versions']] == [2, 1]
        assert detail['activations'][0]['kind'] == 'rollback'
        client.post('/api/prompts/relevance/activate', headers=AUTH, json={'version': 2, 'author': 'Reviewer'})
    with TestClient(create_app(config, FakeEngine())) as client:
        assert client.get('/api/workflow', headers=AUTH).json() == new
        assert client.get('/api/prompts/relevance', headers=AUTH).json()['active']['text'] == text


@pytest.mark.parametrize('kind', ['activate', 'rollback'])
def test_prompt_snapshot_failure_is_atomic_and_retry_succeeds(tmp_path, monkeypatch, kind, database_url):
    import audit.api as api
    from audit.workflow.prompt_registry import DEFAULTS
    app = create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())
    with TestClient(app) as client:
        text = DEFAULTS['relevance'] + '\nFabricated atomic revision'
        assert client.post('/api/prompts/relevance/versions', headers=AUTH,
                           json={'text': text, 'author': 'Reviewer'}).status_code == 201
        if kind == 'rollback':
            assert client.post('/api/prompts/relevance/activate', headers=AUTH,
                               json={'version': 2, 'author': 'Reviewer'}).status_code == 200
        before = client.get('/api/workflow', headers=AUTH).json()
        active_before = app.state.store.active_prompts()
        history_before = app.state.store.prompt_activations('relevance')
        body = {'author': 'Reviewer', **({'version': 2} if kind == 'activate' else {})}

        def fail_snapshot(_base, _texts, _models=None):
            raise RuntimeError('Fabricated snapshot failure')

        with monkeypatch.context() as patch:
            patch.setattr(api, 'with_prompts', fail_snapshot)
            response = client.post(f'/api/prompts/relevance/{kind}', headers=AUTH, json=body)
        assert response.status_code == 500
        assert app.state.store.active_prompts() == active_before
        assert app.state.store.prompt_activations('relevance') == history_before
        assert client.get('/api/workflow', headers=AUTH).json() == before

        retry = client.post(f'/api/prompts/relevance/{kind}', headers=AUTH, json=body)
        assert retry.status_code == 200
        current = client.get('/api/workflow', headers=AUTH).json()
        assert current['id'] == retry.json()['workflow_id'] != before['id']
        assert current['definition']['prompts'] == {
            name: row['text'] for name, row in app.state.store.active_prompts().items()}
        assert current['definition']['prompts']['relevance'] == (text if kind == 'activate' else DEFAULTS['relevance'])
        assert len(app.state.store.prompt_activations('relevance')) == len(history_before) + 1
        assert app.state.store.workflow(current['id']) == current


def test_already_active_prompt_repairs_stale_current_workflow(tmp_path, database_url):
    from audit.workflow.prompt_registry import DEFAULTS
    app = create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())
    with TestClient(app) as client:
        startup = client.get('/api/workflow', headers=AUTH).json()
        client.post('/api/prompts/relevance/versions', headers=AUTH,
                    json={'text': DEFAULTS['relevance'] + '\nFabricated repair revision', 'author': 'Reviewer'})
        body = {'version': 2, 'author': 'Reviewer'}
        assert client.post('/api/prompts/relevance/activate', headers=AUTH, json=body).status_code == 200
        expected = client.get('/api/workflow', headers=AUTH).json()
        history = app.state.store.prompt_activations('relevance')
        app.state.workflow = startup
        retry = client.post('/api/prompts/relevance/activate', headers=AUTH, json=body)
        assert retry.status_code == 200 and retry.json()['workflow_id'] == expected['id']
        assert client.get('/api/workflow', headers=AUTH).json() == expected
        assert app.state.store.prompt_activations('relevance') == history


def test_prompt_changes_reuse_startup_provenance(tmp_path, monkeypatch, database_url):
    import audit.api as api
    from audit.provenance import build_base
    from audit.workflow.prompt_registry import DEFAULTS
    source = tmp_path / 'backend' / 'audit'
    source.mkdir(parents=True)
    source_file = source / 'engine.py'
    source_file.write_text('RULE = "fabricated startup"\n')
    lock_file = tmp_path / 'uv.lock'
    lock_file.write_text('version = 1\n')
    monkeypatch.setattr(api, 'build_base', lambda config: build_base(config, source_root=source))
    engine = FakeEngine()
    engine.public_config = dict(engine.public_config)
    app = create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), engine)
    with TestClient(app) as client:
        startup = client.get('/api/workflow', headers=AUTH).json()
        base = deepcopy(app.state.workflow_base)
        source_file.write_text('RULE = "fabricated edited"\n')
        lock_file.write_text('version = 2\n')
        engine.public_config['model_name'] = 'fabricated changed model'

        def fail_base(_config):
            pytest.fail('Activation must not read source/configuration again')

        monkeypatch.setattr(api, 'build_base', fail_base)
        text = DEFAULTS['relevance'] + '\nFabricated startup-base revision'
        client.post('/api/prompts/relevance/versions', headers=AUTH,
                    json={'text': text, 'author': 'Reviewer'})
        assert client.post('/api/prompts/relevance/activate', headers=AUTH,
                           json={'version': 2, 'author': 'Reviewer'}).status_code == 200
        activated = client.get('/api/workflow', headers=AUTH).json()
        assert activated['id'] != startup['id']
        assert activated['definition']['prompts'] == {**DEFAULTS, 'relevance': text}
        assert {key: value for key, value in activated['definition'].items() if key != 'prompts'} == base
        assert app.state.workflow_base == base
        assert client.post('/api/prompts/relevance/rollback', headers=AUTH,
                           json={'author': 'Reviewer'}).status_code == 200
        assert client.get('/api/workflow', headers=AUTH).json() == startup


def test_running_and_queued_runs_keep_captured_prompts(tmp_path, database_url):
    from audit.workflow.prompt_registry import DEFAULTS
    engine = FakeEngine(wait=True)
    app = create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, max_concurrent_runs=1), engine)
    with TestClient(app) as client:
        old_id = client.get('/api/workflow', headers=AUTH).json()['id']
        first = client.post('/api/benchmarks/runs', headers=AUTH, json=BODY).json()["run"]
        for _ in range(100):
            if engine.received_prompts:
                break
            time.sleep(.01)
        assert engine.received_prompts == [DEFAULTS]
        queued = client.post('/api/benchmarks/runs', headers=AUTH, json=BODY).json()["run"]
        text = DEFAULTS['relevance'] + '\nFabricated new instruction'
        client.post('/api/prompts/relevance/versions', headers=AUTH, json={'text': text, 'author': 'Reviewer'})
        new_id = client.post('/api/prompts/relevance/activate', headers=AUTH,
                             json={'version': 2, 'author': 'Reviewer'}).json()['workflow_id']
        latest = client.post('/api/benchmarks/runs', headers=AUTH, json=BODY).json()["run"]
        assert engine.received_prompts == [DEFAULTS]
        # Release the running fake, allowing both pre- and post-activation queued runs.
        engine.wait = False
        client.portal.call(engine.release.set)
        assert finished(client, first['id'])['workflow_id'] == old_id
        assert finished(client, queued['id'])['workflow_id'] == old_id
        assert finished(client, latest['id'])['workflow_id'] == new_id
        assert engine.received_prompts == [DEFAULTS, DEFAULTS, {**DEFAULTS, 'relevance': text}]


def test_prompt_writes_guarded_before_body_and_reads_remain_open(tmp_path, database_url):
    routes = ['/api/prompts/relevance/versions', '/api/prompts/relevance/activate', '/api/prompts/relevance/rollback']
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        assert client.get('/api/prompts').status_code == 200
        assert client.get('/api/prompts/relevance').status_code == 200
        assert client.get('/api/status').json()['prompts_editable'] is False
        for route in routes:
            response = client.post(route, content=b'{malformed')
            assert response.status_code == 403
            assert response.json() == {'error': {'code': 'prompt_writes_require_token'}}
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())) as client:
        assert client.get('/api/prompts').status_code == 401
        assert client.get('/api/prompts', headers=AUTH).status_code == 200
        assert client.get('/api/status', headers=AUTH).json()['prompts_editable'] is True
        for route in routes:
            assert client.post(route, content=b'{malformed', headers={'Authorization': 'Bearer incorrect'}).status_code == 401
            assert client.post(route, json={}, headers={**AUTH, 'Origin': 'https://untrusted.invalid'}).status_code == 403


def test_prompt_validation_missing_tokens_duplicates_and_not_found(tmp_path, database_url):
    from audit.workflow.prompt_registry import DEFAULTS, REGISTRY, validation_codes
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())) as client:
        for name, text in DEFAULTS.items():
            assert validation_codes(name, text) == []
            for token in REGISTRY[name].required_tokens:
                private = text.replace(token, '') + '\nPRIVATE fabricated marker'
                response = client.post(f'/api/prompts/{name}/versions', headers=AUTH,
                                       json={'text': private, 'author': 'Reviewer'})
                assert response.status_code == 422
                assert 'missing_token:' + token in response.json()['error']['codes']
                assert 'PRIVATE fabricated marker' not in response.text
                assert private not in response.text
        for text in ('', '  ', '\0', 'x' * 20001, 123, None, [], '\ud800'):
            response = client.post('/api/prompts/relevance/versions', headers=AUTH,
                                   content=json.dumps({'text': text, 'author': 'Reviewer'}),
                                   )
            assert response.status_code == 422
            assert '"input"' not in response.text
        for fields in ({'author': ''}, {'author': ' '}, {'author': 'x' * 121}, {'author': 'Reviewer', 'note': 'x' * 2001}):
            response = client.post('/api/prompts/relevance/versions', headers=AUTH, json={'text': DEFAULTS['relevance'], **fields})
            assert response.status_code == 422
        duplicate = client.post('/api/prompts/relevance/versions', headers=AUTH,
                                json={'text': DEFAULTS['relevance'], 'author': 'Reviewer'})
        assert duplicate.status_code == 409 and duplicate.json()['error']['code'] == 'duplicate_version'
        detail = client.get('/api/prompts/relevance', headers=AUTH).json()
        assert len(detail['versions']) == len(detail['activations']) == 1
        response = client.post('/api/prompts/relevance/rollback', headers=AUTH, json={'author': 'Reviewer'})
        assert response.status_code == 409 and response.json()['error']['code'] == 'no_previous_version'
        for route in ('/api/prompts/missing', '/api/prompts/relevance/versions/999',
                      '/api/prompts/missing/versions/1', '/api/prompts/relevance/diff?from=1&to=999'):
            assert client.get(route, headers=AUTH).status_code == 404
        for route, body in (('/api/prompts/missing/versions', {'text': 'fabricated', 'author': 'Reviewer'}),
                            ('/api/prompts/missing/activate', {'version': 1, 'author': 'Reviewer'}),
                            ('/api/prompts/missing/rollback', {'author': 'Reviewer'}),
                            ('/api/prompts/relevance/activate', {'version': 999, 'author': 'Reviewer'})):
            assert client.post(route, headers=AUTH, json=body).status_code == 404


@pytest.mark.parametrize("before, after", [
    ("Fabricated line one\nFabricated old ending", "Fabricated line one\nFabricated new ending"),
    ("Fabricated old ending\n", "Fabricated new ending"),
    ("Fabricated old ending", "Fabricated new ending\n"),
])
def test_prompt_diff_marks_missing_final_newline(before, after):
    from audit.api import unified_prompt_diff
    diff = unified_prompt_diff(before, after, "relevance/v1", "relevance/v2")
    lines = diff.split("\n")
    old_line = next(line for line in lines if line.startswith("-Fabricated") and "ending" in line)
    new_line = next(line for line in lines if line.startswith("+Fabricated") and "ending" in line)
    assert "+" not in old_line[1:] and old_line.endswith("old ending")
    assert new_line.endswith("new ending")
    marker = "\\ No newline at end of file"
    assert diff.count(marker) == (not before.endswith("\n")) + (not after.endswith("\n"))
    assert diff.endswith("\n")


class ModelEngine(FakeEngine):
    models_error = None

    async def available_models(self):
        return ([] if self.models_error else ['fabricated', 'fabricated-screen']), self.models_error


def test_model_writes_require_auth_before_body_validation(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        assert client.get('/api/models').status_code == 200
        response = client.post('/api/models/steps', content=b'{malformed')
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'model_writes_require_token'
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())) as client:
        for headers in [{}, {'Authorization': 'Bearer wrong'}]:
            assert client.post('/api/models/steps', headers=headers, content=b'{malformed').status_code == 401
        assert client.post('/api/models/steps', headers={**AUTH, 'Origin': 'https://untrusted.invalid'}, json={}).status_code == 403


def test_model_catalog_assignments_validation_and_restart_precedence(tmp_path, database_url):
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, model_enabled=True, model_name_relevance='fabricated-screen')
    engine = ModelEngine()
    app = create_app(config, engine)
    with TestClient(app) as client:
        initial = client.get('/api/workflow', headers=AUTH).json()
        catalog = client.get('/api/models', headers=AUTH).json()
        assert catalog['available'] == ['fabricated', 'fabricated-screen']
        assert catalog['default'] == 'fabricated' and catalog['error'] is None
        assert catalog['steps']['relevance'] == {'override': 'fabricated-screen', 'effective': 'fabricated-screen', 'source': 'config'}
        assert 'similarity' not in catalog['steps']
        status = client.get('/api/status', headers=AUTH).json()
        assert status['model_provider'] == 'lmstudio' and status['response_format'] == 'json_schema'
        for step, model, code in [('similarity', 'fabricated', 'unknown_step'), ('invented', None, 'unknown_step'), ('source', 'missing', 'unknown_model')]:
            response = client.post('/api/models/steps', headers=AUTH, json={'step': step, 'model': model, 'author': 'Reviewer'})
            assert response.status_code == 422 and response.json()['error']['code'] == code
        for body in [{'step': 'source', 'model': '', 'author': 'Reviewer'}, {'step': 'source', 'model': 'fabricated', 'author': ' '}]:
            assert client.post('/api/models/steps', headers=AUTH, json=body).status_code == 422
        assert app.state.store.model_assignments() == {}
        clear = client.post('/api/models/steps', headers=AUTH, json={'step': 'relevance', 'model': None, 'author': 'Reviewer', 'note': 'Invented clear'})
        assert clear.status_code == 200
        assert clear.json()['effective'] == 'fabricated'
        assert clear.json()['workflow_id'] != initial['id']
        assert client.get('/api/models', headers=AUTH).json()['steps']['relevance'] == {'override': None, 'effective': 'fabricated', 'source': 'api'}
        assert app.state.store.model_assignments() == {'relevance': None}
        # An API null keeps selecting the default after restart, overriding the env value.
    with TestClient(create_app(config, ModelEngine())) as client:
        assert client.get('/api/workflow', headers=AUTH).json()['id'] == clear.json()['workflow_id']
        assert client.get('/api/models', headers=AUTH).json()['steps']['relevance']['source'] == 'api'
        restore = client.post('/api/models/steps', headers=AUTH, json={'step': 'relevance', 'model': 'fabricated-screen', 'author': 'Reviewer'})
        assert restore.json()['workflow_id'] == initial['id']


def test_model_discovery_error_and_synthetic_allow_configured_names(tmp_path, database_url):
    engine = ModelEngine()
    engine.models_error = 'models_unavailable'
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, model_enabled=True)
    with TestClient(create_app(config, engine)) as client:
        assert client.get('/api/models', headers=AUTH).json()['error'] == 'models_unavailable'
        response = client.post('/api/models/steps', headers=AUTH, json={'step': 'source', 'model': 'not-listed', 'author': 'Reviewer'})
        assert response.status_code == 200
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())) as client:
        catalog = client.get('/api/models', headers=AUTH).json()
        assert catalog['available'] == [] and catalog['error'] is None
        assert catalog['steps']['source']['effective'] == 'not-listed'
        assert client.post('/api/models/steps', headers=AUTH, json={'step': 'source', 'model': 'another-model', 'author': 'Reviewer'}).status_code == 200


def test_model_assignment_and_workflow_failure_roll_back_together(tmp_path, monkeypatch, database_url):
    import audit.api as api
    app = create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())
    with TestClient(app) as client:
        before = client.get('/api/workflow', headers=AUTH).json()
        def fail(*args):
            raise RuntimeError('PRIVATE snapshot failure')
        with monkeypatch.context() as patch:
            patch.setattr(api, 'with_prompts', fail)
            response = client.post('/api/models/steps', headers=AUTH, json={'step': 'relevance', 'model': 'fabricated-screen', 'author': 'Reviewer'})
        assert response.status_code == 500 and 'PRIVATE' not in response.text
        assert app.state.store.model_assignments() == app.state.model_assignments == {}
        assert client.get('/api/workflow', headers=AUTH).json() == before
        assert client.post('/api/models/steps', headers=AUTH, json={'step': 'relevance', 'model': 'fabricated-screen', 'author': 'Reviewer'}).status_code == 200


def test_running_and_queued_runs_keep_captured_models_and_prompt_activation_preserves_them(tmp_path, database_url):
    from audit.workflow.prompt_registry import DEFAULTS
    engine = ModelEngine(wait=True)
    app = create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, max_concurrent_runs=1), engine)
    with TestClient(app) as client:
        old = client.get('/api/workflow', headers=AUTH).json()
        first = client.post('/api/benchmarks/runs', headers=AUTH, json=BODY).json()["run"]
        for _ in range(100):
            if engine.received_models:
                break
            time.sleep(.01)
        queued = client.post('/api/benchmarks/runs', headers=AUTH, json=BODY).json()["run"]
        response = client.post('/api/models/steps', headers=AUTH, json={'step': 'relevance', 'model': 'fabricated-screen', 'author': 'Reviewer'})
        assert response.status_code == 200
        updated = client.get('/api/workflow', headers=AUTH).json()
        latest = client.post('/api/benchmarks/runs', headers=AUTH, json=BODY).json()["run"]
        assert engine.received_models == [old['definition']['models']]
        client.post('/api/prompts/relevance/versions', headers=AUTH, json={'text': DEFAULTS['relevance'] + '\nInvented revision', 'author': 'Reviewer'})
        assert client.post('/api/prompts/relevance/activate', headers=AUTH, json={'version': 2, 'author': 'Reviewer'}).status_code == 200
        assert client.get('/api/workflow', headers=AUTH).json()['definition']['models'] == updated['definition']['models']
        engine.wait = False
        client.portal.call(engine.release.set)
        assert finished(client, first['id'])['workflow_id'] == old['id']
        assert finished(client, queued['id'])['workflow_id'] == old['id']
        assert finished(client, latest['id'])['workflow_id'] == updated['id']
        assert engine.received_models == [old['definition']['models'], old['definition']['models'], updated['definition']['models']]


def test_test_mode_startup_seeds_the_real_demo_dataset_and_survives_a_restart(tmp_path):
    from audit.demo_engine import DemoEngine

    config = settings(tmp_path, mode="test")
    for _ in range(2):
        with TestClient(create_app(config, DemoEngine(step_delay=0))) as client:
            runs = client.get("/api/runs", params={"limit": 200}).json()["items"]
            statuses = {run["status"] for run in runs}
            assert {"queued", "running", "succeeded", "failed"} <= statuses
            assert all(run["id"].startswith("run_demo_") for run in runs)
    assert sorted(path.name for path in (tmp_path / "demo").iterdir())[0] == "audit-test.sqlite3"


def assert_stopped(run, step_count=0):
    assert run["status"] == "cancelled"
    assert run["status"] != "succeeded"
    assert run["verdict"] is None
    assert run["output"] is None
    assert run["finished_at"]
    assert run["error"] == {
        "code": "cancelled",
        "message": ("A person stopped this audit before it finished. "
                    "Submit a new run to retry."),
    }
    assert len(run["steps"]) == step_count
    story = run["summary"]
    assert story["verdict"] is None
    assert story["verdict_label"] == "Stopped by a person"
    assert "not a result" in story["headline"]
    assert story["human_review_note"] is None
    assert all(step["status"] == "pending" for step in story["steps"][step_count:])


def test_operator_stop_queued_and_running_preserves_steps(tmp_path, database_url):
    engine = FakeEngine(wait=True)
    app = create_app(settings(tmp_path, database_url=database_url, max_concurrent_runs=1), engine)
    with TestClient(app) as client:
        first, second = [client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
                         for _ in range(2)]
        assert client.get("/api/queue").json() == {
            "paused": False, "max_concurrent_runs": 1,
            "running": [first], "queued": [second],
        }
        response = client.post(f"/api/runs/{second}/cancel")
        assert response.status_code == 200
        assert_stopped(client.get(f"/api/runs/{second}/details").json())
        assert engine.active == 1
        assert client.get(f"/api/runs/{first}/details").json()["status"] == "running"
        response = client.post(f"/api/runs/{first}/cancel")
        assert response.status_code == 200
        assert_stopped(client.get(f"/api/runs/{first}/details").json(), 1)
        assert engine.active == 0
        assert not app.state.run_tasks
        assert not app.state.operator_cancels
        assert client.get("/api/runs?status=cancelled").json()["total"] == 2
        articles = client.get("/api/articles?status=cancelled").json()
        assert articles["total"] == 1
        assert articles["items"][0]["latest_run_status"] == "cancelled"
        assert articles["items"][0]["latest_verdict"] is None
        history = client.get("/api/articles/" + response.json()["article"]["ref"]).json()
        assert all(run["status"] == "cancelled" for run in history["runs"])
        assert client.post(f"/api/runs/{first}/cancel").json() == {
            "error": {"code": "run_not_active"}}


def test_stop_finished_and_unknown_runs(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        run_id = client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
        before = finished(client, run_id)
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 409
        assert response.json() == {"error": {"code": "run_not_active"}}
        assert client.get(f"/api/runs/{run_id}/details").json() == before
        assert client.post("/api/runs/missing/cancel").status_code == 404


def test_pause_resume_and_restart_queue(tmp_path, database_url):
    config = settings(tmp_path, database_url=database_url)
    engine = FakeEngine(wait=True)
    app = create_app(config, engine)
    with TestClient(app) as client:
        first = client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
        second = client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
        assert client.post("/api/queue/pause").json()["paused"] is True
        third = client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
        # Release the active engine on its own loop, after pause. The second
        # task already passed the event and is waiting for the semaphore.
        client.portal.call(engine.release.set)
        assert finished(client, first)["status"] == "succeeded"
        assert client.get("/api/queue").json() == {
            "paused": True, "max_concurrent_runs": 1,
            "running": [], "queued": [second, third],
        }
        assert client.get(f"/api/runs/{second}/details").json()["steps"] == []
        assert engine.peak == 1
        assert client.post("/api/queue/resume").json()["paused"] is False
        for run_id in [second, third]:
            assert finished(client, run_id)["status"] == "succeeded"
        assert client.post("/api/queue/pause").json()["queued"] == []
    with TestClient(create_app(config, FakeEngine())) as client:
        assert client.get("/api/queue").json()["paused"] is False


@pytest.mark.parametrize("include_running", [False, True])
def test_bulk_stop_queue(tmp_path, include_running, database_url):
    engine = FakeEngine(wait=True)
    app = create_app(settings(tmp_path, database_url=database_url), engine)
    with TestClient(app) as client:
        ids = [client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
               for _ in range(3)]
        query = "?include_running=true" if include_running else ""
        response = client.post("/api/queue/cancel" + query)
        expected = ids if include_running else ids[1:]
        assert response.status_code == 200
        assert set(response.json()["cancelled"]) == set(expected)
        for run_id in expected:
            assert_stopped(client.get(f"/api/runs/{run_id}/details").json(),
                           1 if run_id == ids[0] else 0)
        assert client.get("/api/queue").json()["queued"] == []
        if not include_running:
            assert engine.active == 1
            assert client.get(f"/api/runs/{ids[0]}/details").json()["status"] == "running"
        assert client.post("/api/queue/cancel").json() == {"cancelled": []}


def test_stop_untracked_active_rows_and_prestart_task(tmp_path, database_url):
    app = create_app(settings(tmp_path, database_url=database_url), FakeEngine())
    with TestClient(app) as client:
        for running in [False, True]:
            run = app.state.store.create_run(
                workflow_id=app.state.workflow["id"], category="benchmark",
                origin="benchmark", input={key: BODY[key] for key in
                                           ("removed_article", "similar_articles")})
            if running:
                app.state.store.start_run(run["id"])
            stopped = client.post(f'/api/runs/{run["id"]}/cancel').json()
            assert stopped["record_id"] is None
            assert_stopped(client.get(f'/api/runs/{run["id"]}/details').json())

        async def stop_before_task_runs():
            # Calling the route functions in the same turn deterministically
            # exercises cancellation before execute_run's try block is entered.
            routes = {route.path: route.endpoint for route in app.routes}
            from audit.schemas import RunInput
            run = await routes["/api/benchmarks/runs"](RunInput.model_validate(BODY))
            return await routes["/api/runs/{run_id}/cancel"](run["run"]["id"])

        stopped = client.portal.call(stop_before_task_runs)
        assert stopped["record_id"] is None
        assert_stopped(client.get(f"/api/runs/{stopped['run']['id']}/details").json())


def test_completion_wins_operator_stop_race(tmp_path, database_url):
    class CompletingEngine(FakeEngine):
        async def run(self, request, **kwargs):
            # Simulate completion winning just as cancellation is delivered.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().run(request, **kwargs)

    app = create_app(settings(tmp_path, database_url=database_url), CompletingEngine())
    with TestClient(app) as client:
        run_id = client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 409
        assert response.json() == {"error": {"code": "run_not_active"}}
        assert client.get(f"/api/runs/{run_id}/details").json()["status"] == "succeeded"


@pytest.mark.parametrize("path", ["/api/runs/missing/cancel", "/api/queue/pause",
                                  "/api/queue/resume", "/api/queue/cancel"])
def test_run_controls_use_existing_guards(tmp_path, path, database_url):
    app = create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())
    with TestClient(app) as client:
        assert client.get("/api/queue").status_code == 401
        assert client.post(path).status_code == 401
        assert client.post(path, headers={**AUTH, "Origin": "https://untrusted.invalid"}).status_code == 403


def test_cancelled_story_marks_interrupted_step_stopped_without_rewriting_it(tmp_path, database_url):
    class InterruptedEngine(FakeEngine):
        async def run(self, request, on_step=None, prompts=None, models=None):
            try:
                return await super().run(request, on_step, prompts, models)
            except asyncio.CancelledError:
                await on_step({
                    "name": "similarity", "status": "failed", "model": None,
                    "input": {}, "output": {
                        "explanation": "Stage did not complete.",
                        "details": {"error_type": "CancelledError"},
                    },
                    "model_calls": [], "evidence": [], "duration_ms": 1,
                })
                raise

    app = create_app(settings(tmp_path, database_url=database_url), InterruptedEngine(wait=True))
    with TestClient(app) as client:
        run_id = client.post("/api/benchmarks/runs", json=BODY).json()["run"]["id"]
        for _ in range(100):
            if client.get(f"/api/runs/{run_id}/details").json()["steps"]:
                break
            time.sleep(0.01)
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 200
        assert response.json()["record_id"] is None
        stopped = client.get(f"/api/runs/{run_id}/details").json()
        assert_stopped(stopped, 2)
        step = stopped["steps"][1]
        assert step["status"] == "failed"
        assert step["output"]["details"]["error_type"] == "CancelledError"
        story = stopped["summary"]
        assert story["steps"][0]["status"] == "completed"
        assert story["steps"][1]["status"] == "stopped"
        assert story["failed_steps"] == []
        assert story["sentences"] == []
        assert story["headline"] == (
            "A person stopped this audit before it finished. This is not a result."
        )
        assert client.get(f"/api/runs/{run_id}/details").json() == stopped


def test_cancelled_summary_suppresses_stale_verdict_and_flags():
    from audit.run_summary import summarize_run

    story = summarize_run({
        "status": "cancelled", "verdict": "POSSIBLE_CENSORSHIP",
        "steps": [], "output": {
            "flags": ["SOURCE_DEAD", "RELATED_REMOVALS"],
            "stages": [{"stage": "classification", "outcome": "fail"}],
        },
    })
    assert story["verdict"] is None
    assert story["human_review_note"] is None
    assert story["sentences"] == []
    assert all(step["status"] == "pending" for step in story["steps"])


@pytest.mark.parametrize("field", ["url", "title", "content", "label", "reviewer", "note", "author", "text", "model"])
def test_nul_inputs_are_rejected_with_codes_only(tmp_path, database_url, field):
    private = "fictional-private-text\x00tail"
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())) as client:
        if field in {"url", "title", "content", "label"}:
            body = deepcopy(BODY)
            if field == "label":
                body[field] = private
            else:
                body["removed_article"][field] = private
            path = "/api/benchmarks/runs"
        elif field in {"reviewer", "note"}:
            body = {"reviewer": "Reviewer", "reaction": "agree", field: private}
            path = "/api/runs/missing/reactions"
        elif field == "model":
            body = {"step": "relevance", "model": private, "author": "Reviewer"}
            path = "/api/models/steps"
        else:
            body = {"author": "Reviewer", "text": "Invented prompt", field: private}
            path = "/api/prompts/relevance/versions"
        response = client.post(path, json=body, headers=AUTH)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
        assert all(set(item) == {"loc", "type", "code"} for item in response.json()["error"]["fields"])
        assert "fictional-private-text" not in response.text


def test_live_startup_refuses_a_database_holding_demo_data(tmp_path, database_url):
    """Live mode never serves demo data, whether the file check or the store check sees it."""
    store = Store(database_url)
    store.save_workflow({"id": "wf_demo_v1", "created_at": "test",
                         "definition": {"steps": [], "settings": {}}})
    store.close()
    with pytest.raises((ValueError, RuntimeError), match="demo data"):
        with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())):
            pass


def assert_lean_result(body):
    from audit.record import AuditResult
    assert AuditResult.model_validate(body).model_dump(mode="json", by_alias=True) == body
    assert body["schema_version"] == "2.0"
    assert not {"steps", "input", "output", "model", "provenance", "request"} & body.keys()


@pytest.mark.parametrize("path", ["/api/audit", "/api/webhooks/audit", "/api/benchmarks/runs"])
def test_submissions_and_single_result_are_lean(tmp_path, database_url, path):
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN,
                      live_ingestion_enabled=True)
    with TestClient(create_app(config, FakeEngine())) as client:
        client.headers.update(AUTH)
        submitted = client.post(path, json=BODY)
        assert submitted.status_code == 202
        body = submitted.json()
        assert_lean_result(body)
        assert body["run"]["status"] == "queued"
        assert body["record_id"] is body["lineage"] is body["verdict"] is None
        assert body["stages"] == [] and body["evidence"] is None
        run_id = body["run"]["id"]
        details = finished(client, run_id)
        result = client.get(f"/api/runs/{run_id}").json()
        assert_lean_result(result)
        assert result["verdict"] == details["output"]["verdict"]
        assert result["run"]["workflow_id"] == details["workflow_id"]
        if path == "/api/benchmarks/runs":
            assert result["record_id"] is result["lineage"] is None
        else:
            assert result["record_id"] == run_id + ".r0"
            assert result["lineage"] == {"kind": "initial"}
        assert client.get(f"/api/runs/{run_id}/details").json() == details
        assert details["input"]["removed_article"]["content"] == BODY["removed_article"]["content"]
        assert details["steps"]
        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/runs/missing/details").status_code == 404


def test_article_submission_and_reaudit_lineage(tmp_path, database_url):
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN,
                      live_ingestion_enabled=True)
    with TestClient(create_app(config, FakeEngine())) as client:
        client.headers.update(AUTH)
        first = client.post("/api/audit", json=BODY).json()
        first_id = first["run"]["id"]
        finished(client, first_id)
        assert client.get(f"/api/runs/{first_id}").json()["lineage"] == {"kind": "initial"}
        ref = first["article"]["ref"]
        second = client.post(f"/api/articles/{ref}/audit")
        assert second.status_code == 202
        assert_lean_result(second.json())
        assert second.json()["record_id"] is None
        second_id = second.json()["run"]["id"]
        finished(client, second_id)
        result = client.get(f"/api/runs/{second_id}").json()
        assert result["record_id"] == second_id + ".r0"
        assert result["lineage"] == {"kind": "re_audit", "previous_record_id": first_id + ".r0"}
        # Re-reading an old result never points forward to a later run.
        assert client.get(f"/api/runs/{first_id}").json()["lineage"] == {"kind": "initial"}


def test_cancel_response_and_cancelled_result_have_no_record(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine(wait=True))) as client:
        accepted = client.post("/api/benchmarks/runs", json=BODY).json()
        run_id = accepted["run"]["id"]
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 200
        assert_lean_result(response.json())
        assert response.json()["run"]["status"] == "cancelled"
        assert response.json()["record_id"] is response.json()["lineage"] is None
        assert response.json()["verdict"] is None and response.json()["stages"] == []
        assert client.get(f"/api/runs/{run_id}").json() == response.json()


def test_seeded_demo_runs_have_lean_display_results_without_portable_identity(tmp_path):
    app = create_app(settings(tmp_path, mode="test"))
    with TestClient(app) as client:
        for summary in client.get("/api/runs").json()["items"]:
            result = client.get(f"/api/runs/{summary['id']}")
            assert result.status_code == 200
            assert_lean_result(result.json())
            assert result.json()["record_id"] is result.json()["lineage"] is None


def stored_result_run(client, *, payload=None, findings="Fictional output"):
    store = client.app.state.store
    run = store.create_run(workflow_id=client.app.state.workflow["id"], category="live",
                           origin="api", input=deepcopy(payload or BODY))
    store.complete_run(run["id"], {"response": {
        "verdict": "INCONCLUSIVE", "stages": [], "findings": findings, "flags": [],
        "short_circuited_at": None, "classification": None}})
    return store.run(run["id"])


@pytest.mark.parametrize("kind", ["credential", "api", "model", "wallet_json", "wallet_file",
                                  "wallet_file_blank_json"])
def test_unsafe_stored_result_is_withheld_but_details_remain(tmp_path, database_url, kind):
    secret = "fictional-sensitive-material"
    options = {"api_auth_token": TOKEN}
    if kind == "model":
        options["model_api_key"] = secret
    if kind.startswith("wallet"):
        raw = json.dumps({"d": secret})
        if kind == "wallet_json":
            options["archive_wallet_json"] = raw
        else:
            # Blank inline JSON falls back to the file, exactly as archive sign-in does.
            if kind == "wallet_file_blank_json":
                options["archive_wallet_json"] = "  \n"
            wallet = tmp_path / "fictional-wallet.json"
            wallet.write_text(raw)
            options["archive_wallet_path"] = str(wallet)
    findings = ("access_token=fictional-token" if kind == "credential"
                else TOKEN if kind == "api" else secret)
    config = settings(tmp_path, database_url=database_url, **options)
    with TestClient(create_app(config, FakeEngine())) as client:
        client.headers.update(AUTH)
        run = stored_result_run(client, findings=findings)
        response = client.get(f'/api/runs/{run["id"]}')
        assert response.status_code == 409
        assert response.json() == {"error": {
            "code": "result_withheld", "run_id": run["id"],
            "reason": "credential_detected" if kind == "credential" else "secret_detected"}}
        details = client.get(f'/api/runs/{run["id"]}/details')
        assert details.status_code == 200
        assert details.json()["output"]["findings"] == findings
        assert client.app.state.store.run(run["id"]) == run


def test_refusal_inventory_covers_the_database_password(tmp_path):
    config = settings(tmp_path, database_url="postgresql+psycopg://audit:fictional%40db-pass@db.example/audit")
    assert "fictional@db-pass" in _result_secrets(config)()


def test_wallet_provisioned_after_start_is_refused(tmp_path, database_url):
    wallet = tmp_path / "fictional-wallet.json"  # missing at start, as before provisioning
    secret = "fictional-private-component"
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN,
                      archive_wallet_path=str(wallet))
    with TestClient(create_app(config, FakeEngine())) as client:
        client.headers.update(AUTH)
        run = stored_result_run(client, findings=secret)
        assert client.get(f'/api/runs/{run["id"]}').status_code == 200
        wallet.write_text(json.dumps({"d": secret}))
        response = client.get(f'/api/runs/{run["id"]}')
        assert response.status_code == 409
        assert response.json()["error"]["reason"] == "secret_detected"


def test_unicode_url_byte_limit_at_submission(tmp_path, database_url):
    payload = deepcopy(BODY)
    payload["removed_article"]["url"] = "https://news.example/" + "🧵" * 4000
    with TestClient(create_app(settings(tmp_path, database_url=database_url),
                               FakeEngine())) as client:
        response = client.post("/api/benchmarks/runs", json=payload)
        assert response.status_code == 422
        assert response.json() == {"error": {"code": "validation_error", "fields": [
            {"loc": ["body", "removed_article", "url"], "type": "value_error",
             "code": "invalid_value"}]}}
        assert client.app.state.store.runs()["total"] == 0


def test_old_oversized_url_is_withheld_on_read_reaudit_and_cancel(tmp_path, database_url):
    payload = deepcopy(BODY)
    payload["removed_article"]["url"] = "https://news.example/" + "🧵" * 4000
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN,
                      live_ingestion_enabled=True)
    with TestClient(create_app(config, FakeEngine(wait=True))) as client:
        client.headers.update(AUTH)
        client.post("/api/queue/pause")
        run = stored_result_run(client, payload=payload)
        response = client.get(f'/api/runs/{run["id"]}')
        assert response.status_code == 409
        assert response.json() == {"error": {
            "code": "result_withheld", "reason": "too_large", "run_id": run["id"]}}
        assert client.get(f'/api/runs/{run["id"]}/details').status_code == 200
        response = client.post(f'/api/articles/{run["article_ref"]}/audit')
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "result_withheld" and error["reason"] == "too_large"
        run_id = error["run_id"]
        assert client.get(f"/api/runs/{run_id}/details").json()["status"] == "queued"
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 409
        assert response.json() == {"error": {
            "code": "result_withheld", "reason": "too_large", "run_id": run_id}}
        assert client.get(f"/api/runs/{run_id}/details").json()["status"] == "cancelled"


@pytest.mark.parametrize("path", ["/api/audit", "/api/webhooks/audit", "/api/benchmarks/runs",
                                  "/api/articles/{ref}/audit", "/api/runs/{run_id}/cancel"])
def test_mutations_map_builder_refusals_without_undoing_work(
        tmp_path, database_url, monkeypatch, path):
    from audit.record import RecordError

    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN,
                      live_ingestion_enabled=True)
    with TestClient(create_app(config, FakeEngine(wait=True))) as client:
        client.headers.update(AUTH)
        client.post("/api/queue/pause")
        original = stored_result_run(client)
        ref = original["article_ref"]
        if path.endswith("/cancel"):
            queued = client.post(f"/api/articles/{ref}/audit").json()
            path = path.format(run_id=queued["run"]["id"])
        else:
            path = path.format(ref=ref)

        def refuse(*args, **kwargs):
            raise RecordError("too_large")

        monkeypatch.setattr("audit.api.build_result", refuse)
        response = client.post(path, json=BODY)
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "result_withheld" and error["reason"] == "too_large"
        details = client.get(f'/api/runs/{error["run_id"]}/details')
        assert details.status_code == 200
        assert details.json()["status"] == ("cancelled" if path.endswith("/cancel") else "queued")
        assert error["run_id"] != original["id"]


def test_imported_long_archive_identity_survives_result_routes(tmp_path, database_url):
    from types import SimpleNamespace

    archive_id = "7" * 78
    ref = "ka-" + archive_id
    url = "https://news.example/fictional-import"
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN,
                      live_ingestion_enabled=True)
    with TestClient(create_app(config, FakeEngine())) as client:
        client.headers.update(AUTH)
        store = client.app.state.store
        assert store.import_articles([{"article_id": archive_id, "url": url,
                                      "title": "Fictional archive item"}])["imported"] == 1
        assert store.fill_article(ref, SimpleNamespace(
            id=archive_id, versions=[], news_text="Fabricated archive text.",
            language_code="en", checks=[]), expected_url=url, expected_archive_id=archive_id)
        client.post("/api/queue/pause")
        submitted = client.post(f"/api/articles/{ref}/audit")
        assert submitted.status_code == 202
        run_id = submitted.json()["run"]["id"]
        responses = [submitted, client.get(f"/api/runs/{run_id}"),
                     client.post(f"/api/runs/{run_id}/cancel")]
        for response in responses:
            assert response.status_code in {200, 202}
            assert response.json()["article"]["ref"] == ref
            assert response.json()["article"]["archive_id"] == archive_id
        assert client.get(f"/api/runs/{run_id}/details").status_code == 200


def test_url_owned_article_result_includes_attached_archive_identity(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url),
                               FakeEngine())) as client:
        run = stored_result_run(client)
        ref = run["article_ref"]
        assert ref.startswith("u-")
        assert client.app.state.store.import_articles([{
            "article_id": "42", "url": BODY["removed_article"]["url"],
            "title": "Fictional archive title"}])["updated"] == 1
        response = client.get(f'/api/runs/{run["id"]}')
        assert response.status_code == 200
        assert response.json()["article"]["ref"] == ref
        assert response.json()["article"]["archive_id"] == "42"
