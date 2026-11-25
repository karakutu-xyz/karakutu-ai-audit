"""Articles, summaries and archive reads use fabricated data and offline engines only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
import socket
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url

from audit.api import create_app
from audit.clients.karakutu import KarakutuClient
from audit.clients.models import ArticleRecord
from audit.db import ArticleRow, ArticleStoreError, MIGRATIONS, Run, Store, article_values, normalized_url
from audit.demo import DemoSeedRefused, FICTION, OUTLETS, seed
from audit.demo_engine import DemoEngine
from audit.domain import Verdict
from audit.engine import AuditEngine
from audit.provenance import STEPS
from audit.run_summary import VERDICT_TEXT, summarize_run
from audit.schemas import ArticleDetail, RunStory
from test_api import AUTH, BODY, TOKEN, FakeEngine, finished, settings
from test_db import old_database, workflow


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Article test attempted network access")

    # The opt-in database fixture may resolve only its explicitly configured server.
    admin = os.environ.get("TEST_POSTGRES_URL")
    target = make_url(admin) if admin else None
    resolve = socket.getaddrinfo

    def database_dns(host, port, *args, **kwargs):
        if target and host == target.host and int(port) == (target.port or 5432):
            return resolve(host, port, *args, **kwargs)
        return fail()

    monkeypatch.setattr(socket, "getaddrinfo", database_dns)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)


def add_article(store, *, url="https://paper.example/fictional", category="benchmark", **extra):
    values = article_values({"url": url, "title": "Fictional permit question", "content": FICTION}, category)
    with Session(store.engine) as session, session.begin():
        row = ArticleRow(**{**values, **extra})
        session.add(row)
        return row.ref


def test_url_normalization_and_ref():
    url = "  HTTPS://PAPER.Example/Case?Mixed=VALUE#fragment  "
    normalized = "https://paper.example/Case?Mixed=VALUE"
    assert normalized_url(url) == normalized
    assert article_values({"url": url, "title": "Fiction", "content": FICTION}, "benchmark")["ref"] == "u-" + hashlib.sha256(normalized.encode()).hexdigest()[:16]
    assert normalized_url("https://paper.example/Case?Mixed=other") != normalized


def version_two(path):
    old_database(path)
    store = Store(f"sqlite:///{path}", migrations=MIGRATIONS[:2])
    store.close()
    # Metadata also creates new tables; remove them to reproduce a genuine retained v2 DB.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE articles")
        connection.execute("DROP TABLE model_assignments")
        connection.execute("INSERT INTO runs SELECT 'run_second', workflow_id,category,origin,status,'later',finished_at,label,article_title,verdict,synthetic,input,output,steps,model,error,expected_verdict FROM runs WHERE id='run_old'")
        connection.execute("UPDATE runs SET created_at='2026-11-01' WHERE id='run_old'")
        connection.execute("UPDATE runs SET created_at='2026-11-02', input=json_set(input, '$.removed_article.url', 'HTTPS://FABRICATED.invalid/story#later', '$.removed_article.title', 'Later fictional title', '$.removed_article.content', 'Later fictional content') WHERE id='run_second'")


def test_migration_backfill_and_repeat_startup(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    version_two(path)
    for _ in range(2):
        store = Store(f"sqlite:///{path}")
        try:
            articles = store.articles()
            assert articles["total"] == 1
            article = articles["items"][0]
            first, second = store.run("run_old"), store.run("run_second")
            assert first["article_ref"] == second["article_ref"] == article["ref"]
            assert article["run_count"] == 2
            assert store.article(article["ref"])["content"] == first["input"]["removed_article"]["content"]
            assert article["title"] == first["input"]["removed_article"]["title"]
            assert second["input"]["removed_article"]["title"] == "Later fictional title"
            assert first["reactions"][0]["id"] == "rx_old"
        finally:
            store.close()


def test_articles_migration_failure_is_transactional(tmp_path):
    path = tmp_path / "rollback.sqlite3"
    version_two(path)

    def fail(connection):
        MIGRATIONS[3][1](connection)
        raise RuntimeError("fictional migration failure")

    with pytest.raises(RuntimeError, match="fictional migration failure"):
        Store(f"sqlite:///{path}", migrations=[*MIGRATIONS[:3], (4, fail)])
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "article_ref" not in {r[1] for r in connection.execute("PRAGMA table_info(runs)")}
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='articles'").fetchall()
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 2
    store = Store(f"sqlite:///{path}")
    assert store.articles()["items"][0]["run_count"] == 2
    store.close()


@pytest.mark.parametrize("verdict", list(Verdict))
def test_summary_verdict_table_and_flags(verdict):
    story = summarize_run({"status": "failed" if verdict == Verdict.AUDIT_FAILED else "succeeded",
        "verdict": verdict.value, "steps": [], "output": {"findings": "Must never be pasted.",
        "flags": ["SOURCE_DEAD", "RELATED_REMOVALS"]}})
    parsed = RunStory.model_validate(story)
    assert (parsed.verdict_label, parsed.headline) == VERDICT_TEXT[verdict.value]
    assert len(parsed.sentences) == 2
    assert "Must never" not in str(story)
    assert (parsed.human_review_note is None) == (verdict == Verdict.AUDIT_FAILED)
    assert [s.name.value for s in parsed.steps] == [s["name"] for s in STEPS]


def test_summary_step_lines_failure_skips_and_running():
    record = {"status": "failed", "verdict": None, "steps": [
        {"name": "classification", "status": "completed", "output": {"explanation": "  " + "F" * 600, "outcome": "pass"}},
        {"name": "similarity", "status": "failed", "output": {"explanation": "Fictional failure", "outcome": "inconclusive"}},
        {"name": "relevance", "status": "skipped", "output": {"explanation": "earlier step failed", "outcome": "inconclusive"}}]}
    story = summarize_run(record)
    assert story["verdict"] == "AUDIT_FAILED"
    assert story["human_review_note"] is None
    assert len(story["steps"][0]["line"]) == 500 and story["steps"][0]["line"].endswith("…")
    assert story["failed_steps"] == [STEPS[1]["title"]]
    assert story["skipped_steps"] == [STEPS[2]["title"]]
    assert story["steps"][2]["line"] == "Skipped: earlier step failed"
    record.update(status="running", steps=record["steps"][:1])
    story = summarize_run(record)
    assert story["verdict_label"] == "Audit in progress"
    assert [s["status"] for s in story["steps"]] == ["completed", "running", "pending", "pending", "pending", "pending"]
    assert story["steps"][1]["line"] is None
    record.update(status="queued", steps=[])
    assert all(s["status"] == "pending" for s in summarize_run(record)["steps"])


def test_demo_list_filters_detail_and_sql_projection(tmp_path):
    config = settings(tmp_path, mode="test", api_auth_token=TOKEN)
    with TestClient(create_app(config, DemoEngine(step_delay=0))) as client:
        client.headers.update(AUTH)
        listing = client.get("/api/articles").json()
        assert 3 <= listing["total"] < 27
        assert listing["limit"] == 50 and listing["offset"] == 0
        unaudited = client.get("/api/articles?status=not_audited").json()
        assert unaudited["total"] >= 3
        assert all(a["run_count"] == 0 and a["latest_run_id"] is None for a in unaudited["items"])
        assert unaudited["total"] == 3
        assert all(a["source"] and a["removed_at"] for a in listing["items"])
        for status in ("audited", "failed", "in_progress"):
            response = client.get("/api/articles", params={"status": status}).json()
            # Test-mode startup seeds after interrupting previous work.
            assert response["total"] > 0
            assert all(a["status"] == status for a in response["items"])
        assert client.get("/api/articles?category=live").json()["total"] == 2
        match = client.get("/api/articles?q=HARBOR&limit=1&offset=1").json()
        assert len(match["items"]) == 1 and match["total"] > 1
        assert client.get("/api/articles?q=%25").json()["total"] == 0
        for verdict in Verdict:
            matches = client.get("/api/articles", params={"verdict": verdict.value}).json()
            assert all(a["latest_verdict"] == verdict.value for a in matches["items"])
        a = next(a for a in listing["items"] if a["run_count"] == 3)
        detail = client.get(f"/api/articles/{a['ref']}").json()
        ArticleDetail.model_validate(detail)
        assert detail["content"] and detail["source"] and detail["removed_at"]
        assert len(detail["runs"]) == 3
        assert [r["created_at"] for r in detail["runs"]] == sorted((r["created_at"] for r in detail["runs"]), reverse=True)
        assert detail["runs"][0]["summary"] == client.get(f"/api/runs/{detail['runs'][0]['id']}/details").json()["summary"]
        assert client.get("/api/articles/missing").status_code == 404
        assert client.post("/api/articles/missing/audit").status_code == 404
        for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"status": "bad"}, {"q": "x" * 201}):
            assert client.get("/api/articles", params=params).status_code == 422
        statements = []
        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)
        event.listen(client.app.state.store.engine, "before_cursor_execute", capture)
        client.get("/api/articles")
        event.remove(client.app.state.store.engine, "before_cursor_execute", capture)
        assert len(statements) == 2
        assert not any("runs.input" in s or "runs.output" in s or "runs.steps" in s for s in statements)
        assert any("row_number() OVER" in s for s in statements)


def test_first_audit_rerun_retains_history_and_labels(tmp_path):
    config = settings(tmp_path, mode="test", api_auth_token=TOKEN)
    with TestClient(create_app(config, DemoEngine(step_delay=0))) as client:
        client.headers.update(AUTH)
        ref = client.get("/api/articles?status=not_audited").json()["items"][0]["ref"]
        first = client.post(f"/api/articles/{ref}/audit")
        assert first.status_code == 202
        first = finished(client, first.json()["run"]["id"])
        second = client.post(f"/api/articles/{ref}/audit")
        assert second.status_code == 202
        second = finished(client, second.json()["run"]["id"])
        assert first["id"] != second["id"]
        assert first["article_ref"] == second["article_ref"] == ref
        assert second["origin"] == "article" and second["category"] == "benchmark"
        assert second["input"]["similar_articles"] == []
        assert len(client.get(f"/api/articles/{ref}").json()["runs"]) == 2
        assert client.get(f"/api/runs/{first['id']}/details").json() == first
        benchmark = client.post("/api/benchmarks/runs", json=BODY).json()
        benchmark = finished(client, benchmark["run"]["id"])
        rerun = client.post(f"/api/articles/{benchmark['article_ref']}/audit")
        assert rerun.status_code == 202
        rerun = client.get(f"/api/runs/{rerun.json()['run']['id']}/details")
        assert rerun.json()["label"] == BODY["label"]
        assert rerun.json()["expected_verdict"] == BODY["expected_verdict"]
        assert rerun.json()["workflow_id"] == client.get("/api/workflow").json()["id"]


def test_first_audit_uses_disabled_model_engine(tmp_path, database_url):
    config = settings(tmp_path, database_url=database_url)
    with TestClient(create_app(config, AuditEngine(config.engine_config()))) as client:
        ref = add_article(client.app.state.store)
        response = client.post(f"/api/articles/{ref}/audit")
        assert response.status_code == 202
        run = finished(client, response.json()["run"]["id"])
        assert run["article_ref"] == ref and run["verdict"] == "AUDIT_FAILED"
        assert client.get("/api/articles?status=failed").json()["total"] == 1


def test_every_demo_article_audits_and_reruns_offline(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Test mode constructed the live engine")

    monkeypatch.setattr(AuditEngine, "__init__", fail)
    config = settings(tmp_path, mode="test", api_auth_token=TOKEN)
    with TestClient(create_app(config)) as client:
        client.headers.update(AUTH)
        engine = client.app.state.audit_engine
        assert isinstance(engine, DemoEngine)
        engine.step_delay = 0
        assert not config.live_ingestion_enabled
        listing = client.get("/api/articles").json()["items"]
        assert len(listing) == 19
        assert {a["category"] for a in listing} == {"live", "benchmark"}
        for article in listing:
            if article["status"] == "in_progress":
                conflict = client.post(f"/api/articles/{article['ref']}/audit")
                assert conflict.status_code == 409
                assert conflict.json()["error"] == {
                    "code": "run_in_progress", "run_id": article["latest_run_id"],
                }
        # End the two scripted pending examples before exercising their re-runs.
        client.app.state.store.interrupt_incomplete()
        for article in listing:
            ref = article["ref"]
            before = client.get(f"/api/articles/{ref}").json()
            assert FICTION in before["content"]
            assert before["source"] and before["removed_at"]
            history = {r["id"]: client.get(f"/api/runs/{r['id']}/details").json() for r in before["runs"]}
            completed = []
            for _ in range(2):
                response = client.post(f"/api/articles/{ref}/audit")
                assert response.status_code == 202
                run = finished(client, response.json()["run"]["id"])
                assert run["status"] == "succeeded"
                assert run["article_ref"] == ref and run["origin"] == "article"
                assert run["category"] == article["category"]
                assert run["workflow_id"] == client.get("/api/workflow").json()["id"]
                assert run["synthetic"] is True and run["model"]["enabled"] is False
                assert "Demo output from the offline test mode" in run["output"]["findings"]
                completed.append(run)
            assert completed[0]["id"] != completed[1]["id"]
            assert completed[0]["output"] == completed[1]["output"]
            assert client.get(f"/api/runs/{completed[0]['id']}/details").json() == completed[0]
            after = client.get(f"/api/articles/{ref}").json()
            assert after["run_count"] == before["run_count"] + 2
            assert after["status"] == "audited"
            for run_id, saved in history.items():
                assert client.get(f"/api/runs/{run_id}/details").json() == saved
        assert client.get("/api/articles?status=not_audited").json()["total"] == 0


@pytest.mark.parametrize("configured", [False, True])
def test_test_mode_article_audits_follow_the_write_token_rule(tmp_path, configured):
    """Test mode needs no token by default; a configured token is enforced like every write."""
    config = settings(tmp_path, mode="test", api_auth_token=TOKEN if configured else "")
    with TestClient(create_app(config, DemoEngine(step_delay=0))) as client:
        headers = AUTH if configured else {}
        for category in ("benchmark", "live"):
            ref = client.get(f"/api/articles?category={category}&status=audited", headers=headers).json()["items"][0]["ref"]
            path = f"/api/articles/{ref}/audit"
            if configured:
                for wrong in ({}, {"Authorization": "Bearer wrong"}):
                    response = client.post(path, headers=wrong)
                    assert response.status_code == 401
                    assert response.json()["error"]["code"] == "unauthorized"
                assert client.post(path, headers={**AUTH, "Origin": "https://untrusted.invalid"}).status_code == 403
            assert client.post(path, headers=headers).status_code == 202


def test_live_mode_articles_never_seed_demo_data(tmp_path):
    config = settings(tmp_path)
    with TestClient(create_app(config, FakeEngine())) as client:
        assert config.mode == "live"
        assert client.get("/api/articles").json()["total"] == 0
        ref = add_article(client.app.state.store)
        before = client.get("/api/articles").json()
        with pytest.raises(DemoSeedRefused, match="Not a test database"):
            seed(config.database_url)
        assert client.get("/api/articles").json() == before
        assert [a["ref"] for a in before["items"]] == [ref]
        with Session(client.app.state.store.engine) as session:
            assert session.scalar(select(ArticleRow).where(ArticleRow.origin == "demo")) is None


@pytest.mark.parametrize("configured", [False, True])
def test_live_mode_article_audit_still_requires_live_ingestion(tmp_path, configured, database_url):
    config = settings(tmp_path, database_url=database_url, api_auth_token=TOKEN if configured else "")
    with TestClient(create_app(config, FakeEngine())) as client:
        ref = add_article(client.app.state.store, category="live")
        assert client.post(f"/api/articles/{ref}/audit", headers=AUTH).status_code == 404
        assert client.app.state.store.article(ref)["run_count"] == 0


def test_audit_conflict_is_atomic_and_no_content(tmp_path, database_url):
    engine = FakeEngine(wait=True)
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        ref = add_article(client.app.state.store)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(f"/api/articles/{ref}/audit"), range(2)))
        assert sorted(r.status_code for r in responses) == [202, 409]
        run_id = next(r.json()["run"]["id"] for r in responses if r.status_code == 202)
        conflict = next(r for r in responses if r.status_code == 409)
        assert conflict.json() == {"error": {"code": "run_in_progress", "run_id": run_id}}
        assert client.get(f"/api/articles/{ref}").json()["run_count"] == 1
        for status in ("queued", "running"):
            client.app.state.store._update(run_id, status=status)
            assert client.post(f"/api/articles/{ref}/audit").status_code == 409
        empty_ref = add_article(client.app.state.store, url="https://paper.example/empty", content=" ")
        assert client.post(f"/api/articles/{empty_ref}/audit").json() == {"error": {"code": "article_has_no_content"}}
        assert client.get("/api/articles?status=in_progress").json()["total"] == 1


def test_store_checks_in_progress_without_api_lock(tmp_path, database_url):
    store = Store(database_url)
    workflow(store)
    ref = add_article(store)
    def create(_):
        try:
            return store.create_run(workflow_id="wf_test", category="benchmark", origin="article",
                                    input={}, article_ref=ref)["id"]
        except ArticleStoreError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert results.count("run_in_progress") == 1
    assert store.article(ref)["run_count"] == 1
    store.close()


@pytest.mark.parametrize("path", ["/api/articles/import", "/api/articles/missing/audit"])
def test_article_writes_auth_and_origin_guards(tmp_path, path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN), FakeEngine())) as client:
        assert client.post(path).status_code == 401
        assert client.post(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post(path, headers={**AUTH, "Origin": "https://untrusted.invalid"}).status_code == 403
        assert client.post(path, headers=AUTH).status_code in {404, 409}
        assert client.get("/api/articles").status_code == 401
    with TestClient(create_app(settings(tmp_path, database_url=database_url), FakeEngine())) as client:
        assert client.post(path).status_code in {404, 409}


class FakeArchive:
    enabled = True
    base_url = "https://archive.example"

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def audits_created_between(self, start, end):
        self.calls.append(("list", start, end))
        item = {"article_id": 42, "url": "https://paper.example/archived", "title": "Fictional archive story",
                "newspaper": {"display_name": "Fictional Wire"}, "publish_date": "2026-11-03T00:00:00Z"}
        return [item, deepcopy(item), {"article_id": None}]

    async def get_article(self, article_id):
        self.calls.append(("detail", article_id))
        if self.fail:
            raise RuntimeError("PRIVATE upstream article content")
        return ArticleRecord(id=42, url="https://paper.example/archived", language_code="en",
            versions=[{"title": "Fictional old title", "news_text": "Earlier fiction", "check_date": 1, "content_hash": "old"},
                      {"title": "Fictional latest title", "news_text": FICTION, "check_date": 2, "content_hash": "new"}],
            checks={"entries": [{"status": 404, "check_date": 1}, {"status": 200, "check_date": 2},
                                 {"status": 410, "check_date": 3}, {"status": 404, "check_date": 4}]})


def test_test_mode_disables_import_and_lazy_archive_fill(tmp_path):
    engine = DemoEngine(step_delay=0)
    engine.archive = FakeArchive()
    config = settings(tmp_path, mode="test", api_auth_token=TOKEN)
    with TestClient(create_app(config, engine)) as client:
        client.headers.update(AUTH)
        # An injected archive client must not bypass the run-mode boundary.
        response = client.post("/api/articles/import")
        assert response.status_code == 409
        assert response.json() == {"error": {"code": "archive_unavailable", "reason": "not_configured"}}
        client.app.state.store.import_articles([
            {"article_id": 42, "url": "https://paper.example/archived", "title": "Fictional archive story"},
        ])
        for _ in range(2):
            response = client.get("/api/articles/ka-42")
            assert response.status_code == 200
            assert response.json()["content"] == ""
        assert engine.archive.calls == []
        assert client.app.state.article_fill_attempts == set()


@pytest.mark.parametrize("fail", [False, True])
def test_archive_import_and_lazy_fill_once(tmp_path, fail, database_url):
    engine = FakeEngine()
    engine.archive = FakeArchive(fail)
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        imported = client.post("/api/articles/import?days=90")
        assert imported.status_code == 200
        assert imported.json() == {"imported": 1, "updated": 0, "skipped": 2}
        kind, start, end = engine.archive.calls[0]
        assert kind == "list"
        # The archive's audit listing takes Unix seconds, as related removals sends them.
        assert type(start) is int and type(end) is int
        assert end - start == 90 * 86400
        assert client.post("/api/articles/import").json() == {"imported": 0, "updated": 0, "skipped": 3}
        assert client.post("/api/articles/import?days=91").status_code == 422
        stored = client.app.state.store.article("ka-42")
        assert stored["content"] == "" and stored["source"] == "Fictional Wire"
        assert stored["status"] == "not_audited" and stored["category"] == "live"
        for _ in range(2):
            response = client.get("/api/articles/ka-42")
            assert response.status_code == 200
            detail = response.json()
            assert "PRIVATE" not in response.text
        assert [c[0] for c in engine.archive.calls].count("detail") == 1
        assert detail["content"] == ("" if fail else FICTION)
        if not fail:
            # Archive check dates are Unix seconds; stored dates are ISO like published_at.
            assert detail["language"] == "en"
            assert detail["removed_at"] == "1970-01-01T00:00:03+00:00"
            assert len(detail["snapshot"]["versions"]) == 2
            assert "news_text" not in str(detail["snapshot"])
            assert detail["links"]["archive_url"] is None
        # Importing local metadata must not enable live execution.
        assert client.post("/api/articles/ka-42/audit").status_code == 404


@pytest.mark.parametrize("entries", [
    [{"status": 404, "check_date": "unknown"}],
    [{"status": 404, "check_date": 10 ** 30}],
    [{"status": 410, "check_date": "later"}, {"status": 200, "check_date": 2}],
])
def test_lazy_fill_keeps_text_when_removal_date_is_unusable(tmp_path, entries, database_url):
    class OddDates(FakeArchive):
        async def get_article(self, article_id):
            record = await super().get_article(article_id)
            return record.model_copy(update={"checks": {"entries": entries}})

    engine = FakeEngine()
    engine.archive = OddDates()
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        assert client.post("/api/articles/import").status_code == 200
        detail = client.get("/api/articles/ka-42").json()
        assert detail["content"] == FICTION and detail["language"] == "en"
        assert detail["removed_at"] is None


def test_archive_unavailable_and_live_article_audit(tmp_path, database_url):
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        assert client.post("/api/articles/import").json() == {"error": {"code": "archive_unavailable", "reason": "not_configured"}}
        assert client.post("/api/articles/import").status_code == 409
    engine.archive = FakeArchive()
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, live_ingestion_enabled=True), engine)) as client:
        assert client.post("/api/articles/import", headers=AUTH).status_code == 200
        assert client.post("/api/articles/ka-42/audit", headers=AUTH).status_code == 422
        assert client.get("/api/articles/ka-42", headers=AUTH).status_code == 200
        run = client.post("/api/articles/ka-42/audit", headers=AUTH)
        assert run.status_code == 202
        assert run.json()["run"]["category"] == "live" and run.json()["run"]["origin"] == "article"
        assert run.json()["article"]["ref"] == "ka-42"


async def test_archive_client_get_only():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 42, "url": "https://paper.example/fictional", "checks": {"entries": []}})
    archive = KarakutuClient("https://archive.example")
    await archive.http.aclose()
    archive.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert (await archive.get_article(42)).id == 42
        assert requests[0].url.path == "/articles/42"
        assert requests[0].url.params.get_list("expand") == ["checks", "versions"]
        assert all(r.method == "GET" for r in requests)
    finally:
        await archive.close()


def test_all_submission_routes_link_same_article(tmp_path, database_url):
    with TestClient(create_app(settings(tmp_path, database_url=database_url, api_auth_token=TOKEN, live_ingestion_enabled=True), FakeEngine())) as client:
        runs = [client.post(path, headers=AUTH, json=BODY).json() for path in
                ("/api/benchmarks/runs", "/api/audit", "/api/webhooks/audit")]
        assert len({r["article"]["ref"] for r in runs}) == 1
        assert client.get("/api/articles", headers=AUTH).json()["items"][0]["run_count"] == 3


def test_article_and_run_insert_rollback_together(tmp_path, database_url):
    store = Store(database_url)
    with pytest.raises(Exception):
        store.create_run(workflow_id="missing", category="benchmark", origin="benchmark", input=BODY)
    with Session(store.engine) as session:
        assert session.scalar(select(ArticleRow)) is None
        assert session.scalar(select(Run)) is None
    store.close()


def test_latest_run_drives_status_verdict_activity_and_search(tmp_path, database_url):
    store = Store(database_url)
    workflow(store)
    ref = add_article(store, source="Fictional Wire", created_at="2026-11-01")
    other = add_article(store, url="https://second.example/story", created_at="2026-11-03")
    first = store.create_run(workflow_id="wf_test", category="benchmark", origin="article", input={}, article_ref=ref)
    store._update(first["id"], created_at="2026-11-02", status="succeeded", verdict="MOVED")
    assert store.articles()["items"][0]["ref"] == other
    second = store.create_run(workflow_id="wf_test", category="benchmark", origin="article", input={}, article_ref=ref)
    store._update(second["id"], created_at="2026-11-04", status="failed", verdict="AUDIT_FAILED")
    assert store.articles()["items"][0]["ref"] == ref
    assert store.articles(verdict="MOVED")["total"] == 0
    assert store.articles(status="failed")["items"][0]["latest_run_id"] == second["id"]
    for query in ("WIRE", "PAPER.EXAMPLE", "/FICTIONAL", "PERMIT"):
        assert store.articles(q=query)["items"][0]["ref"] == ref
    assert store.articles(offset=2)["items"] == []
    store.close()


def test_demo_articles_idempotent_and_non_demo_unchanged(tmp_path):
    url = f"sqlite:///{tmp_path / 'demo-test.sqlite3'}"
    store = Store(url)
    ref = add_article(store, source="Fictional independent outlet", origin="run")
    before = store.article(ref)
    first = seed(url)
    articles = store.articles(limit=200)
    assert first["tables"]["articles"] == 19
    assert seed(url) == first
    assert store.articles(limit=200) == articles
    assert store.article(ref) == before
    with Session(store.engine) as session:
        assert all(session.get(ArticleRow, r.article_ref).origin == "demo"
                   for r in session.scalars(select(Run)).all())
    store.close()


def test_import_updates_metadata_and_keeps_existing_run_identity(tmp_path, database_url):
    store = Store(database_url)
    workflow(store)
    run = store.create_run(workflow_id="wf_test", category="benchmark", origin="benchmark", input=BODY)
    original = store.article(run["article_ref"])
    items = [{"article_id": 52, "url": BODY["removed_article"]["url"], "title": "Fictional archive title", "newspaper": "Fictional Wire"}]
    assert store.import_articles(items) == {"imported": 0, "updated": 1, "skipped": 0}
    article = store.article(run["article_ref"])
    assert article["ref"] == original["ref"] and article["category"] == "benchmark"
    assert article["content"] == original["content"] and article["title"] == original["title"]
    assert article["archive_id"] == "52" and article["source"] == "Fictional Wire"
    items = [{"article_id": 53, "url": "https://import.example/fictional", "title": "Fictional initial title"}]
    assert store.import_articles(items)["imported"] == 1
    items[0]["title"] = "Fictional revised title"
    assert store.import_articles(items)["updated"] == 1
    assert store.article("ka-53")["title"] == items[0]["title"]
    items[0]["url"] = "https://changed.example/fictional"
    assert store.import_articles(items)["updated"] == 1
    assert store.article("ka-53")["url"] == items[0]["url"]
    assert store.article("ka-53")["host"] == "changed.example"
    assert store.import_articles([{"article_id": 54, "url": "https://[broken"}])["skipped"] == 1
    store.close()


def test_archive_connection_credentials_never_in_article_response(tmp_path, database_url):
    engine = FakeEngine()
    engine.archive = FakeArchive()
    engine.archive.base_url = "https://fictional-user:fictional-pass@archive.example/private-api"
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        assert client.post("/api/articles/import").status_code == 200
        response = client.get("/api/articles/ka-42")
        assert response.status_code == 200
        assert response.json()["links"] == {
            "original_url": "https://paper.example/archived", "archive_url": None}
        for private in (engine.archive.base_url, "fictional-user", "fictional-pass", "archive.example"):
            assert private not in response.text


def test_migration_batches_metadata_and_links_for_two_thousand_runs(tmp_path):
    path = tmp_path / "batch-upgrade.sqlite3"
    version_two(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM reactions")
        connection.execute("DELETE FROM runs")
        payloads = []
        # Reverse insertion order exercises creation-time ordering and keyset ties.
        for i in reversed(range(2000)):
            payload = {"removed_article": {"url": f"https://batch.example/story-{i % 4}",
                "title": f"Fictional École {i}", "content": f"Fictional content from run {i}.",
                "published_at": "2026-11-01"}}
            payloads.append((f"run_batch_{i}", f"2026-11-01T00:{i // 120:02d}:00Z", json.dumps(payload)))
        connection.executemany("INSERT INTO runs (id, workflow_id, category, origin, status, "
            "created_at, article_title, synthetic, input, steps, model) "
            "VALUES (?, 'wf_old', 'benchmark', 'benchmark', 'succeeded', ?, 'Fictional title', 0, ?, '[]', '{}')",
            payloads)
    statements = []
    def capture(_conn, _cursor, statement, params, _context, many):
        statements.append((statement, params, many))
    def migrate(connection):
        event.listen(connection, "before_cursor_execute", capture)
        try:
            MIGRATIONS[3][1](connection)
        finally:
            event.remove(connection, "before_cursor_execute", capture)
    store = Store(f"sqlite:///{path}", migrations=[*MIGRATIONS[:3], (4, migrate)])
    try:
        assert len(statements) < 100  # Batch statements, well below two per run.
        content_reads = [params for sql, params, _many in statements
                         if sql.startswith("SELECT runs.id, JSON_EXTRACT")]
        assert len(content_reads) == 1
        assert len(content_reads[0]) == 6  # JSON path plus one first-run ID per URL.
        links = [(params, many) for sql, params, many in statements if sql.startswith("UPDATE runs")]
        assert len(links) == 4 and all(many and len(params) == 500 for params, many in links)
        assert store.articles()["total"] == 4
        assert store.articles(q="ÉCOLE")["total"] == 4
        # Tied timestamps are resolved by insertion order: 119, 118, 117, 116 come first.
        for item in store.articles()["items"]:
            i = 116 + int(item["url"].rsplit("-", 1)[1])
            detail = store.article(item["ref"])
            assert detail["run_count"] == 500
            assert detail["content"] == f"Fictional content from run {i}."
            assert detail["title"] == f"Fictional École {i}"
        before = store.articles()
        with store.engine.begin() as connection:
            MIGRATIONS[3][1](connection)  # A repeated backfill is also a no-op.
        assert store.articles() == before
    finally:
        store.close()
    reopened = Store(f"sqlite:///{path}")
    assert reopened.articles() == before
    reopened.close()


def test_archive_identity_merges_url_article_and_refills_after_url_change(tmp_path, database_url):
    engine = FakeEngine()
    engine.archive = FakeArchive()
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        store = client.app.state.store
        workflow(store)
        assert client.post("/api/articles/import").json()["imported"] == 1
        # Fill the old URL, then reproduce an independently submitted new URL.
        assert client.get("/api/articles/ka-42").json()["content"] == FICTION
        new_url = "https://paper.example/new-fictional-address"
        payload = {"removed_article": {"url": new_url, "title": "Fictional new address", "content": FICTION}}
        run = store.create_run(workflow_id="wf_test", category="benchmark", origin="benchmark", input=payload)
        archived_run = store.create_run(workflow_id="wf_test", category="live", origin="article",
                                        input={}, article_ref="ka-42")
        old_input = store.run(archived_run["id"])["input"]
        assert store.import_articles([{"article_id": 42, "url": new_url,
            "title": "Fictional refreshed title", "newspaper": "Fictional Wire"}]) == {
                "imported": 0, "updated": 1, "skipped": 0}
        article = store.article("ka-42")
        assert article["url"] == new_url and article["run_count"] == 2
        assert article["content"] == "" and article["snapshot"] is None
        assert article["language"] is None and article["removed_at"] is None
        assert store.article(run["article_ref"]) is None
        assert store.run(run["id"])["article_ref"] == "ka-42"
        assert store.run(archived_run["id"])["input"] == old_input
        assert store.articles()["total"] == 1
        old_get = engine.archive.get_article
        async def refreshed_record(article_id):
            record = await old_get(article_id)
            record.url = new_url
            record.versions[-1].news_text = FICTION + " Refreshed fictional text."
            return record
        engine.archive.get_article = refreshed_record
        refreshed = client.get("/api/articles/ka-42").json()["content"]
        assert refreshed == FICTION + " Refreshed fictional text."
        assert [call[0] for call in engine.archive.calls].count("detail") == 2
        assert store.import_articles([{"article_id": 42, "url": new_url,
            "title": "Fictional refreshed title", "newspaper": "Fictional Wire"}])["skipped"] == 1
        assert store.article("ka-42")["content"] == refreshed


@pytest.mark.parametrize("upgrade", [False, True])
def test_archive_id_unique_for_fresh_and_migrated_databases(tmp_path, upgrade, database_url):
    path = tmp_path / "unique.sqlite3"
    if upgrade:
        version_two(path)
    store = Store(f"sqlite:///{path}" if upgrade else database_url)
    try:
        store.import_articles([{"article_id": 42, "url": "https://paper.example/first"}])
        with pytest.raises(IntegrityError), Session(store.engine) as session, session.begin():
            session.add(ArticleRow(**article_values({"url": "https://paper.example/second",
                "title": "Fictional duplicate", "content": FICTION}, "live"), archive_id="42"))
    finally:
        store.close()


def test_demo_upgrade_repairs_owned_metadata_and_preserves_unrelated_article(tmp_path):
    path = tmp_path / "demo-upgrade-test.sqlite3"
    url = f"sqlite:///{path}"
    seed(url)
    store = Store(url)
    workflow(store)
    unrelated = store.create_run(workflow_id="wf_test", category="benchmark", origin="benchmark", input=BODY)
    store.close()
    # Reproduce a pre-Articles (v3) seeded database, retaining all run input and IDs.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ix_runs_article_ref")
        connection.execute("DROP INDEX ix_runs_article_activity")
        connection.execute("ALTER TABLE runs DROP COLUMN article_ref")
        connection.execute("DROP TABLE articles")
        connection.execute("PRAGMA user_version=3")
    store = Store(url)
    try:
        before = store.article(unrelated["article_ref"])
        assert seed(url)["tables"]["articles"] == 19
        with Session(store.engine) as session:
            articles = session.scalars(select(ArticleRow).where(ArticleRow.origin == "demo")).all()
            assert len(articles) == 19
            assert all(a.source in {name for name, _host in OUTLETS} and a.published_at and a.removed_at
                       for a in articles)
            for run in session.scalars(select(Run).where(Run.id.startswith("run_demo_"))).all():
                assert session.get(ArticleRow, run.article_ref).origin == "demo"
        assert store.article(unrelated["article_ref"]) == before
        assert seed(url)["tables"]["articles"] == 19
    finally:
        store.close()


def test_demo_metadata_repair_requires_literal_demo_run_ownership(tmp_path):
    url = f"sqlite:///{tmp_path / 'mixed-test.sqlite3'}"
    seed(url)
    store = Store(url)
    workflow(store)
    try:
        with Session(store.engine) as session, session.begin():
            demo = session.get(Run, "run_demo_reservoir_v1")
            payload, ref = deepcopy(demo.input), demo.article_ref
            article = session.get(ArticleRow, ref)
            article.origin, article.source, article.removed_at = "run", "Fictional private source", None
        mixed = store.create_run(workflow_id="wf_test", category="benchmark", origin="benchmark", input=payload)
        # LIKE 'run_demo_%' would mistake this non-demo ID for a demo run.
        with store.engine.begin() as connection:
            connection.execute(Run.__table__.update().where(Run.id == mixed["id"]).values(id="runXdemoYprivate"))
        before = store.article(ref)
        seed(url)
        assert store.article(ref) == before
        with Session(store.engine) as session:
            assert session.get(ArticleRow, ref).origin == "run"
    finally:
        store.close()


def test_unicode_search_normalization_updates_and_literal_wildcards(tmp_path, database_url):
    store = Store(database_url)
    workflow(store)
    try:
        payload = {"removed_article": {"url": "https://unicode.example/fictional",
            "title": "Fictional École 100%_complete\\draft", "content": FICTION}}
        run = store.create_run(workflow_id="wf_test", category="benchmark", origin="benchmark", input=payload)
        for query in ("ÉCOLE", "école", "e\u0301cole", "ÉＣＯＬＥ", "%_", "\\draft"):
            assert store.articles(q=query)["items"][0]["ref"] == run["article_ref"]
        assert store.articles(q="100%_missing")["total"] == 0
        assert store.import_articles([{"article_id": 42, "url": payload["removed_article"]["url"],
            "newspaper": "Fictional Straẞe Wire"}])["updated"] == 1
        assert store.articles(q="STRASSE")["total"] == 1
        store.import_articles([{"article_id": 53, "url": "https://other.example/old", "title": "Fictional old"}])
        store.import_articles([{"article_id": 53, "url": "https://other.example/new", "title": "Fictional École"}])
        assert store.articles(q="ÉCOLE")["total"] == 2
        assert store.articles(q="/OLD")["total"] == 0
        with Session(store.engine) as session, session.begin():
            row = session.get(ArticleRow, "ka-53")
            row.title, row.source, row.host, row.url = "Fictional revised", "École Wire", "école.example", "https://école.example/École"
        for query in ("ÉCOLE WIRE", "ÉCOLE.EXAMPLE", "/ÉCOLE"):
            assert store.articles(q=query)["items"][0]["ref"] == "ka-53"
    finally:
        store.close()


def test_archive_merge_rolls_back_links_and_deleted_row_on_failure(tmp_path, database_url):
    store = Store(database_url)
    workflow(store)
    try:
        store.import_articles([{"article_id": 42, "url": "https://paper.example/old"}])
        payload = {"removed_article": {"url": "https://paper.example/new", "title": "Fictional new", "content": FICTION}}
        run = store.create_run(workflow_id="wf_test", category="benchmark", origin="benchmark", input=payload)
        before = store.article(run["article_ref"])
        def fail(_mapper, _connection, row):
            if row.ref == "ka-42":
                raise RuntimeError("Fictional merge failure")
        event.listen(ArticleRow, "before_update", fail)
        try:
            with pytest.raises(RuntimeError, match="Fictional merge failure"):
                store.import_articles([{"article_id": 42, "url": payload["removed_article"]["url"]}])
        finally:
            event.remove(ArticleRow, "before_update", fail)
        assert store.article(run["article_ref"]) == before
        assert store.run(run["id"])["article_ref"] == run["article_ref"]
        assert store.article("ka-42")["url"] == "https://paper.example/old"
    finally:
        store.close()


def test_import_preserves_run_owned_archive_identity_on_conflicts(tmp_path, database_url):
    store = Store(database_url)
    workflow(store)
    try:
        run = store.create_run(workflow_id="wf_test", category="benchmark", origin="benchmark", input=BODY)
        store.import_articles([{"article_id": 42, "url": BODY["removed_article"]["url"]}])
        before = store.article(run["article_ref"])
        assert store.import_articles([{"article_id": 42, "url": "https://paper.example/new"}])["skipped"] == 1
        assert store.articles()["total"] == 1
        assert store.article(run["article_ref"]) == before
        # A different archive identity cannot take the URL or steal its run links.
        assert store.import_articles([{"article_id": 43, "url": BODY["removed_article"]["url"]}])["skipped"] == 1
        assert store.run(run["id"])["article_ref"] == run["article_ref"]
    finally:
        store.close()


def test_stale_archive_fill_after_concurrent_url_change_is_dropped(tmp_path, database_url):
    """A detail fetch started for one URL must not fill the article after an import moved it."""
    engine = FakeEngine()
    engine.archive = FakeArchive()
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        store = client.app.state.store
        assert client.post("/api/articles/import").json()["imported"] == 1
        new_url = "https://paper.example/moved-fictional-address"
        old_get = engine.archive.get_article

        async def moved_during_fetch(article_id):
            record = await old_get(article_id)
            # The concurrent import lands while the archive response is in flight.
            store.import_articles([{"article_id": 42, "url": new_url, "title": "Fictional moved title",
                                    "newspaper": "Fictional Wire"}])
            return record

        engine.archive.get_article = moved_during_fetch
        detail = client.get("/api/articles/ka-42").json()
        assert detail["url"] == new_url
        assert detail["content"] == "" and detail["snapshot"] is None
        assert store.article("ka-42")["content"] == ""

        # The next read fetches again for the new identity and fills it.
        engine.archive.get_article = old_get
        assert client.get("/api/articles/ka-42").json()["content"] == FICTION


def test_fill_article_rejects_a_different_archive_record(tmp_path, database_url):
    store = Store(database_url)
    try:
        store.import_articles([{"article_id": 42, "url": "https://paper.example/archived"}])
        article = store.article("ka-42")
        other = ArticleRecord(id=43, url="https://paper.example/archived", versions=[
            {"title": "Fictional other", "news_text": FICTION, "check_date": 1, "content_hash": "x"}])
        assert store.fill_article("ka-42", other, expected_url=article["url"],
                                  expected_archive_id=article["archive_id"]) is False
        assert store.article("ka-42")["content"] == ""
    finally:
        store.close()


def test_archive_nul_characters_are_dropped_before_any_write(tmp_path, database_url):
    # PostgreSQL rejects NUL in text and JSON; one bad archive item must not roll back the
    # whole import or leave an article without text. Both backends store the same values.
    class NulArchive(FakeArchive):
        async def audits_created_between(self, start, end):
            item = {"article_id": 42, "url": "https://paper.example/archived",
                    "title": "Fictional\x00 archive story", "newspaper": {"display_name": "Fictional\x00 Wire"},
                    "publish_date": "2026-11-03T00:00:00Z"}
            return [item, {"article_id": 43, "url": "https://paper.example/second", "title": "Second fiction"}]

        async def get_article(self, article_id):
            record = await super().get_article(article_id)
            versions = [version.model_copy(update={"title": version.title + "\x00",
                                                   "news_text": version.news_text + "\x00"})
                        for version in record.versions]
            return record.model_copy(update={"versions": versions, "language_code": "e\x00n"})

    engine = FakeEngine()
    engine.archive = NulArchive()
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        assert client.post("/api/articles/import").json() == {"imported": 2, "updated": 0, "skipped": 0}
        detail = client.get("/api/articles/ka-42").json()
        assert detail["title"] == "Fictional archive story" and detail["source"] == "Fictional Wire"
        assert detail["content"] == FICTION and detail["language"] == "en"
        assert all("\x00" not in version["title"] for version in detail["snapshot"]["versions"])
        assert client.get("/api/articles/ka-43").json()["title"] == "Second fiction"


@pytest.mark.parametrize("stored, shown", [
    ("1700000000", "2023-11-14T22:13:20+00:00"),
    ("1700000000.0", "2023-11-14T22:13:20+00:00"),
    (" 1700000000 ", "2023-11-14T22:13:20+00:00"),
    ("9" * 40, None),
    ("1e+30", None),
    ("nan", "nan"),
    ("2026-11-02T11:00:00+00:00", "2026-11-02T11:00:00+00:00"),
    (None, None),
])
def test_removal_dates_stored_by_older_versions_read_as_iso(tmp_path, stored, shown, database_url):
    # Rows filled before ISO storage hold Unix seconds; upgrading must not show raw numbers.
    store = Store(database_url)
    try:
        values = article_values({"url": "https://older.example/report", "title": "Older fictional row",
                                 "content": FICTION}, "live", "archive")
        with Session(store.engine) as session, session.begin():
            session.add(ArticleRow(**values, removed_at=stored))
        assert store.article(values["ref"])["removed_at"] == shown
        assert store.articles()["items"][0]["removed_at"] == shown
    finally:
        store.close()


@pytest.mark.parametrize("fail", [False, True])
def test_removal_checked_tells_not_loaded_from_no_removal_found(tmp_path, fail, database_url):
    engine = FakeEngine()
    engine.archive = FakeArchive(fail)
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        assert client.post("/api/articles/import").status_code == 200
        listed = client.get("/api/articles").json()["items"]
        assert [(item["ref"], item["removal_checked"]) for item in listed] == [("ka-42", False)]
        detail = client.get("/api/articles/ka-42").json()
        # Opening loads the archive details; a failed load stays unchecked so it can retry.
        assert detail["removal_checked"] is (not fail)
        assert client.get("/api/articles").json()["items"][0]["removal_checked"] is (not fail)
        store = client.app.state.store
        values = article_values({"url": "https://run.example/case", "title": "Fictional run case",
                                 "content": FICTION}, "benchmark")
        with Session(store.engine) as session, session.begin():
            session.add(ArticleRow(**values))
        flags = {item["ref"]: item["removal_checked"] for item in client.get("/api/articles").json()["items"]}
        assert flags[values["ref"]] is True


def test_run_owned_article_matched_by_import_is_not_left_unchecked(tmp_path, database_url):
    # An import attaches an archive id to an existing run-owned article with the same URL but
    # keeps its u- reference; opening it never loads archive details, so it must not wait.
    engine = FakeEngine()
    engine.archive = FakeArchive()
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        store = client.app.state.store
        values = article_values({"url": "https://paper.example/archived", "title": "Fictional run case",
                                 "content": FICTION}, "live")
        with Session(store.engine) as session, session.begin():
            session.add(ArticleRow(**values))
        assert client.post("/api/articles/import").status_code == 200
        item = next(i for i in client.get("/api/articles").json()["items"] if i["ref"] == values["ref"])
        assert client.app.state.store.article(values["ref"])["archive_id"] == "42"
        assert item["removal_checked"] is True


@pytest.mark.parametrize("dead", [False, True])
def test_removal_checked_after_a_load_with_empty_text(tmp_path, dead, database_url):
    # An archive response may carry no text; the check still happened.
    class EmptyText(FakeArchive):
        async def get_article(self, article_id):
            record = await super().get_article(article_id)
            versions = [version.model_copy(update={"news_text": ""}) for version in record.versions]
            checks = record.checks if dead else {"entries": [{"status": 200, "check_date": 2}]}
            return record.model_copy(update={"versions": versions, "news_text": "", "checks": checks})

    engine = FakeEngine()
    engine.archive = EmptyText()
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        assert client.post("/api/articles/import").status_code == 200
        detail = client.get("/api/articles/ka-42").json()
        assert detail["content"] == "" and detail["removal_checked"] is True
        assert (detail["removed_at"] is not None) is dead
        assert client.get("/api/articles").json()["items"][0]["removal_checked"] is True
        # Moving the article to another URL clears its details (JSON null): unchecked again.
        store = client.app.state.store
        with Session(store.engine) as session, session.begin():
            row = session.get(ArticleRow, "ka-42")
            row.snapshot = None
        assert client.get("/api/articles").json()["items"][0]["removal_checked"] is False


def test_only_one_archive_import_runs_at_a_time(tmp_path, database_url):
    import asyncio
    import threading

    started, release = threading.Event(), threading.Event()

    class SlowArchive(FakeArchive):
        async def audits_created_between(self, start, end):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await super().audits_created_between(start, end)

    engine = FakeEngine()
    engine.archive = SlowArchive()
    with TestClient(create_app(settings(tmp_path, database_url=database_url), engine)) as client:
        with ThreadPoolExecutor(2) as pool:
            try:
                first = pool.submit(client.post, "/api/articles/import")
                assert started.wait(5)
                # Bounded, so a missing guard fails here instead of waiting on the stalled archive.
                second = pool.submit(client.post, "/api/articles/import").result(5)
                assert second.status_code == 409
                assert second.json() == {"error": {"code": "import_in_progress"}}
            finally:
                release.set()
            assert first.result(5).json() == {"imported": 1, "updated": 0, "skipped": 2}
        assert client.post("/api/articles/import").status_code == 200


@pytest.mark.parametrize("run_status", ["cancelled", "failed"])
def test_story_never_blames_the_model_for_a_stopped_step(run_status):
    # Runs saved before the stop wording existed carry the model-failure text with a
    # CancelledError; a person stop (cancelled) or a service stop (failed, interrupted) both
    # read as stopped, without an outcome.
    stopped = {"name": "classification", "status": "failed", "output": {
        "explanation": "Stage did not complete: the analysis model failed.", "outcome": "inconclusive",
        "details": {"error_type": "CancelledError"}}}
    story = summarize_run({"status": run_status, "verdict": None, "steps": [stopped]})
    step = story["steps"][0]
    assert step["line"] == "Stage did not complete: the audit was stopped."
    assert step["outcome"] is None
    assert step["status"] == ("stopped" if run_status == "cancelled" else "failed")
    assert "model" not in step["line"]


def test_model_failures_keep_their_wording():
    from audit.domain import StageName
    from audit.workflow import pipeline

    assert pipeline._stage_failed(StageName.RELEVANCE).explanation == \
        "Stage did not complete: the analysis model failed."
    failed = {"name": "relevance", "status": "failed", "output": {
        "explanation": "Stage did not complete: the analysis model failed.", "outcome": "inconclusive",
        "details": {}}}
    step = summarize_run({"status": "failed", "verdict": None, "steps": [failed]})["steps"][2]
    assert step["line"] == "Stage did not complete: the analysis model failed."
    assert step["outcome"] == "inconclusive"
