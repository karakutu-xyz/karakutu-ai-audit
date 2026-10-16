"""Shared fakes for the article-job tests (not a test module).

* `service_settings(monkeypatch, **over)`: the settings singleton with the article routes on
  (API_AUTH_TOKEN set), an in-memory store and fast timings (patched with monkeypatch.setattr, as
  AGENTS.md rule 5 prescribes);
* `AUTH`: the matching `Authorization` header;
* `FakeArchive`: the archive as the worker sees it (the by-article lookup with status, body fetch,
  discussions, PATCH, POST), recording every call;
* `fake_runtime(archive)`: a Runtime whose model is NOT TestModel and whose catalog is loaded,
  so the publish gates pass unless a test breaks one on purpose.
"""
import json
from pathlib import Path
from types import SimpleNamespace

from src.api.models import AuditResponse
from src.clients.models import ArticleRecord
from src.config.settings import get_settings

TOKEN = "test-api-token-0123456789abcdef-0123456789"   # >= 32 chars (Settings.validate)
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FIXTURES = Path(__file__).parent / "fixtures"
# Named records in fixtures/sample_records.json (synthetic ids):
INCONCLUSIVE_RELATED = 900015   # INCONCLUSIVE + RELATED_REMOVALS, archive fully readable
LIKELY_MOVED = 900003           # LIKELY_MOVED, one partial same-publisher copy
DEGRADED = 900019               # INCONCLUSIVE, the archive was unreachable during the audit
RECORDS = json.loads((FIXTURES / "sample_records.json").read_text())["records"]
BODY = ("Gazeteci, belediyenin ihale sürecindeki usulsüzlük iddialarını belgelerle ortaya koydu. "
        * 20)


def record_by(audit_id=None, *, verdict=None):
    for r in RECORDS:
        if (audit_id is None or r["audit_id"] == audit_id) and (verdict is None or r["verdict"] == verdict):
            return json.loads(json.dumps(r))
    raise LookupError(audit_id or verdict)


def service_settings(monkeypatch, **over):
    s = get_settings()
    values = {
        "API_AUTH_TOKEN": TOKEN,
        "ARCHIVE_WRITES_ENABLED": False,
        "JOB_STORE_URL": "memory://",
        "JOB_QUEUE_MAX": 500,
        "JOB_MAX_ATTEMPTS": 3,
        "JOB_APPLY_MAX_ATTEMPTS": 3,
        "JOB_RETRY_BACKOFF_S": 0,
        "JOB_FAIL_STREAK": 3,
        "JOB_HEALTH_POLL_S": 0,
        "JOB_HEALTH_WAIT_MAX_S": 0,
        "JOB_RESULT_REUSE_S": 0,
        "JOB_CONCURRENCY": 1,
        "JOB_CASE_TIMEOUT_S": 600,
        "JOB_SHUTDOWN_GRACE_S": 1,
        "ARCHIVE_WRITE_PACING_S": 0,
        "WEB_SEARCH_PROVIDER": "searxng",
        "KARAKUTU_FRONTEND_URL": "https://karakutu.xyz",
    }
    values.update(over)
    for k, v in values.items():
        monkeypatch.setattr(s, k, v)
    return s


def art(aid):
    """The article id `open_audit(aid)` embeds by default."""
    return aid + 1_000_000


def open_audit(aid, article_id=None, **kw):
    a = {"id": aid, "status": 0, "decision": None, "subject_tag_notations": [],
         "content_type_notation": None, "geographic_scope": None,
         "created_at": "2026-10-01T10:00:00Z",
         "article": {"id": article_id or art(aid), "link": f"https://news.example.org/a/{aid}",
                     "title": f"Article {aid}", "newspaper": "news-example", "status": 404,
                     "publish_date": "2026-09-30"}}
    a.update(kw)
    return a


class FakeArchive:
    enabled = True

    def __init__(self, audits=None, *, discussions=None, body=BODY, patch_ok=True, post_ok=True,
                 precheck_status=None):
        self.audits = {a["id"]: dict(a) for a in (audits or [])}
        self.discussions = dict(discussions or {})
        self.body, self.patch_ok, self.post_ok = body, patch_ok, post_ok
        self.precheck_status = precheck_status      # force (status, body) for every status read
        self.calls = []
        self.next_discussion_id = 1792

    async def get_audit_by_article(self, article_id):
        self.calls.append(("BY_ARTICLE", article_id))
        if self.precheck_status is not None:
            return self.precheck_status
        for a in self.audits.values():
            if (a.get("article") or {}).get("id") == article_id:
                return 200, json.loads(json.dumps(a))
        return 404, {"detail": "Audit not found"}

    async def get_with_status(self, path, params=None, **kw):
        self.calls.append(("GET_STATUS", path))
        if self.precheck_status is not None:
            return self.precheck_status
        if path.startswith("/audits/"):
            aid = int(path.split("/")[2])
            a = self.audits.get(aid)
            return (200, dict(a)) if a is not None else (404, {"detail": "not found"})
        return 200, {}

    async def get_audit(self, aid):
        self.calls.append(("GET", aid))
        a = self.audits.get(aid)
        return dict(a) if a is not None else None

    async def count_discussions(self, aid):
        self.calls.append(("COUNT", aid))
        return self.discussions.get(aid, 0)

    async def get_article_with_body(self, url):
        self.calls.append(("ARTICLE", url))
        if self.body is None:
            return None
        return ArticleRecord(id=4242, url=url, title="Archived title", news_text=self.body)

    async def update_audit_classification(self, aid, **payload):
        self.calls.append(("PATCH", aid, payload))
        if self.patch_ok:
            self.audits[aid].update(payload)
        return self.patch_ok

    async def post_discussion(self, aid, comment):
        self.calls.append(("POST", aid, comment))
        if not self.post_ok:
            return None
        self.discussions[aid] = self.discussions.get(aid, 0) + 1
        did = self.next_discussion_id
        self.next_discussion_id += 1
        return {"id": did, "audit_id": aid, "comment": comment}

    async def check_health(self):
        return True

    def writes(self):
        return [c for c in self.calls if c[0] in ("PATCH", "POST")]


class FakeProvider:
    def __init__(self, healthy=True):
        self.healthy = healthy
        self.health_calls = 0

    async def check_health(self):
        self.health_calls += 1
        return self.healthy


def fake_runtime(archive, *, model=None, catalog="CATALOG", provider=None):
    return SimpleNamespace(provider=provider or FakeProvider(), model=model or object(),
                           agents="AGENTS", catalog=catalog, search_provider="SEARCH",
                           karakutu_client=archive)


def response_for(record):
    return AuditResponse.model_validate(record["response"])
