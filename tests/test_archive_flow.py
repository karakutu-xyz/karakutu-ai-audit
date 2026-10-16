"""src/archive_flow — the batch scripts' logic as a package.

What a reader of the archive sees must not change by accident: the comment text, the PATCH
payload, the record shape, the ledger entries. These tests pin that:

  * GOLDEN comments — SHA-256 of every template × every IPTC-wording state over the synthetic
    sample records (`tests/fixtures/sample_records.json`, one per verdict, flag and tag state the
    templates render), pinned in `tests/fixtures/golden_comments.json`. The pin guards against
    unintended changes to rendered comments; a deliberate change regenerates the fixture;
  * run_case produces the run_open_cases.py record (same keys, same order, same values);
  * apply_record sends the same PATCH / POST and writes the same ledger entry as apply_results.py;
  * build_case keeps fetch_listed_cases.py's retry and field precedence;
  * the two applier additions (write-ahead tag mark, read-after-write) only ever turn a wrong
    "PROPOSED ONLY" into the true "set" — the normal path is untouched.
"""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.api.models import AuditResponse
from src.archive_flow import applier, comments, intake, rules, runner
from src.clients import karakutu as karakutu_module
from src.clients.karakutu import KarakutuClient
from src.clients.models import ArticleRecord

FIXTURES = Path(__file__).parent / "fixtures"
RECORDS = json.loads((FIXTURES / "sample_records.json").read_text())["records"]
GOLDEN = json.loads((FIXTURES / "golden_comments.json").read_text())
STATES = (rules.TAGS_SET, rules.TAGS_PROPOSED, rules.TAGS_PROPOSED_EXISTING)


def _record(verdict=None, *, flag=None):
    for r in RECORDS:
        if (verdict is None or r["verdict"] == verdict) and (flag is None or flag in (r.get("flags") or [])):
            return json.loads(json.dumps(r))
    raise LookupError(verdict)


# --- golden comments ---------------------------------------------------------------------

def test_golden_fixture_covers_every_verdict_and_state():
    verdicts = {r["verdict"] for r in RECORDS}
    assert {"MOVED", "LIKELY_MOVED", "NOT_REMOVED", "INCONCLUSIVE", "LIKELY_LOW_PUBLIC_INTEREST",
            "POSSIBLE_CENSORSHIP", "FOUND_ONLINE", "ERROR"} <= verdicts
    assert any("RELATED_REMOVALS" in (r.get("flags") or []) for r in RECORDS)
    assert any("SOURCE_DEAD" in (r.get("flags") or []) for r in RECORDS)
    assert len(GOLDEN["hashes"]) == len(RECORDS) * 4 * 3
    assert all("logs" not in r and "content" not in r for r in RECORDS)


def test_comments_match_the_golden_hashes(monkeypatch):
    monkeypatch.setattr(comments, "_stamp", lambda: GOLDEN["frozen_date"])
    got, mismatched = {}, []
    for r in RECORDS:
        for k in sorted(comments.TEMPLATES):
            for s in STATES:
                key = f"{r['audit_id']}/{k}/{s}"
                got[key] = hashlib.sha256(comments.render(k, r, tags_state=s).encode()).hexdigest()
                if got[key] != GOLDEN["hashes"][key]:
                    mismatched.append(key)
    assert not mismatched, mismatched[:10]
    assert got.keys() == GOLDEN["hashes"].keys()


# --- runner: record shape ------------------------------------------------------------------

RECORD_KEYS = ["audit_id", "article_id", "url", "title", "newspaper", "publish_date", "http_status",
               "created_at", "content_chars", "list", "probe", "elapsed_s", "stage_times_s", "error",
               "logs", "verdict", "flags", "classification", "response",
               "model"]   # additive: requested + served model ids; nothing renders it


def _case(src):
    return {"audit_id": src["audit_id"], "article_id": src["article_id"], "url": src["url"],
            "title": src["title"], "newspaper": src["newspaper"], "publish_date": src["publish_date"],
            "http_status": src["http_status"], "created_at": src["created_at"], "list": src["list"],
            "probe": src["probe"], "content": "x" * 130_000}


RT = SimpleNamespace(agents="AGENTS", search_provider="SEARCH", catalog="CATALOG")


async def test_run_case_produces_the_run_open_cases_record(monkeypatch):
    src = _record("INCONCLUSIVE", flag="RELATED_REMOVALS")
    seen = {}

    async def fake_run_audit(request, *, agents, search_provider, catalog, **kw):
        seen.update(content_len=len(request.removed_article.content), agents=agents,
                    search=search_provider, catalog=catalog, kw=kw)
        logging.getLogger("src.workflow.pipeline").info("hello from the pipeline")
        return AuditResponse.model_validate(src["response"])

    monkeypatch.setattr(runner, "run_audit", fake_run_audit)
    rec = await runner.run_case(_case(src), RT, instrument=False)

    assert list(rec) == RECORD_KEYS
    assert seen == {"content_len": runner.CONTENT_LIMIT, "agents": "AGENTS", "search": "SEARCH",
                    "catalog": "CATALOG", "kw": {}}, "no archive client passed (the script never did)"
    assert rec["content_chars"] == 130_000
    assert rec["verdict"] == src["verdict"] and rec["flags"] == src["flags"]
    assert rec["response"] == src["response"]
    assert rec["classification"] == src["classification"]
    assert rec["error"] is None
    for k in ("audit_id", "article_id", "url", "title", "newspaper", "publish_date", "http_status",
              "created_at", "list", "probe"):
        assert rec[k] == src[k], k


async def test_run_case_records_a_timeout_and_an_exception_as_error(monkeypatch):
    async def slow(*a, **k):
        await asyncio.sleep(5)

    async def boom(*a, **k):
        raise RuntimeError("model went away")

    case = _case(_record("MOVED"))
    monkeypatch.setattr(runner, "run_audit", slow)
    rec = await runner.run_case(case, RT, timeout=0.01, instrument=False)
    assert (rec["verdict"], rec["error"], rec["response"], rec["flags"], rec["classification"]) == \
        ("ERROR", "case timeout: exceeded 0.01s", None, [], None)
    monkeypatch.setattr(runner, "run_audit", boom)
    rec = await runner.run_case(case, RT, instrument=False)
    assert rec["verdict"] == "ERROR" and rec["error"] == "RuntimeError: model went away"
    assert not rules.result_usable(rec).write


async def test_run_case_falls_back_to_the_title_when_there_is_no_content(monkeypatch):
    seen = {}

    async def fake(request, **kw):
        seen["content"] = request.removed_article.content
        raise RuntimeError("stop")

    monkeypatch.setattr(runner, "run_audit", fake)
    await runner.run_case({"audit_id": 1, "url": "https://files.example/a", "title": "Başlık"}, RT, instrument=False)
    assert seen["content"] == "Başlık"


@pytest.fixture
def instrumentation(monkeypatch):
    """Install the real instrumentation, then undo it: stage `run`s restored by monkeypatch,
    handler and levels removed so no other test sees them."""
    import src.workflow.pipeline as pm
    for key in runner.STAGE_KEYS:
        mod = getattr(pm, f"{key}_stage")
        monkeypatch.setattr(mod, "run", mod.run)
    monkeypatch.setattr(runner, "_installed", {"handler": None, "wrapped": False})
    levels = {n: logging.getLogger(n).level for n in runner.CAPTURED_LOGGERS}
    yield pm
    for n, lvl in levels.items():
        lg = logging.getLogger(n)
        lg.setLevel(lvl)
        for h in [h for h in lg.handlers if isinstance(h, runner.LogCapture)]:
            lg.removeHandler(h)


async def test_concurrent_cases_keep_their_own_logs_and_stage_times(monkeypatch, instrumentation):
    pm = instrumentation

    async def fake_stage(tag, delay):
        await asyncio.sleep(delay)
        logging.getLogger("src.workflow.similarity").info("similarity for %s", tag)

    monkeypatch.setattr(pm.similarity_stage, "run", fake_stage)
    response = AuditResponse.model_validate(_record("MOVED")["response"])

    async def fake_run_audit(request, **kw):
        await pm.similarity_stage.run(request.removed_article.title, 0.05 if "A" in request.removed_article.title else 0.01)
        return response

    monkeypatch.setattr(runner, "run_audit", fake_run_audit)
    runner.install_instrumentation()
    a, b = await asyncio.gather(
        runner.run_case({"audit_id": 1, "url": "https://files.example/1", "title": "case A"}, RT),
        runner.run_case({"audit_id": 2, "url": "https://files.example/2", "title": "case B"}, RT))
    assert a["logs"] == ["similarity: similarity for case A"]
    assert b["logs"] == ["similarity: similarity for case B"]
    assert set(a["stage_times_s"]) == {"similarity"} and set(b["stage_times_s"]) == {"similarity"}
    assert a["stage_times_s"]["similarity"] >= b["stage_times_s"]["similarity"]


def test_dedupe_cases_keeps_the_newest_of_url_variants_and_shared_article_ids():
    cases = [{"audit_id": 3, "article_id": 30, "url": "https://news.example.org/haber/a,100"},
             {"audit_id": 2, "article_id": 20, "url": "http://www.news.example.org/haber/a,100/"},
             {"audit_id": 1, "article_id": 30, "url": "https://news.example.org/haber/other,200"},
             {"audit_id": 0, "article_id": 40, "url": "https://daily.example.net/haber/9"}]
    kept, dropped = runner.dedupe_cases(cases)
    assert [c["audit_id"] for c in kept] == [3, 0]
    assert [(d["audit_id"], d["duplicate_of"]) for d in dropped] == [(2, 3), (1, 3)]


async def test_wait_for_model_reports_through_say_and_returns(monkeypatch):
    lines, answers = [], iter([False, True])

    class P:
        async def check_health(self):
            return next(answers)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)
    assert await runner.wait_for_model(P(), every=1, max_wait=100, say=lines.append) is True
    assert lines[0].startswith("   model server down for") and lines[-1].startswith("   model server healthy again")


# --- intake.build_case -----------------------------------------------------------------------

def _article(**kw):
    base = {"id": 77, "url": "https://news.example.org/haber/x,1", "title": "Archive title",
            "newspaper": {"slug": "example-news"}, "publish_date": "2026-09-01",
            "versions": [{"news_text": "kısa"}, {"news_text": "y" * 900}], "news_text": ""}
    base.update(kw)
    return ArticleRecord.model_validate(base)


class BodyClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    async def get_article_with_body(self, url):
        self.calls.append(url)
        return self.answers.pop(0)


AUDIT = {"id": 5, "status": 0, "decision": None, "created_at": 1759000000, "subject_tag_notations": None,
         "content_type_notation": "genre:Current", "geographic_scope": None,
         "article": {"id": 99, "link": "https://news.example.org/haber/x,1", "title": "Audit title",
                     "newspaper": "example-np", "newspaper_country": "TR", "publish_date": "2026-08-01",
                     "status": 404}}


async def test_build_case_retries_once_and_keeps_the_scripts_field_precedence():
    c = BodyClient([None, _article()])
    case = await intake.build_case(c, audit=AUDIT, list_row={"audit_id": 5, "url": "https://ignored/",
                                                             "title": "Row", "publisher": "Example News"},
                                   discussions=0, retry_sleep_s=0)
    assert c.calls == ["https://news.example.org/haber/x,1"] * 2
    assert case == {
        "audit_id": 5, "article_id": 77, "url": "https://news.example.org/haber/x,1", "title": "Archive title",
        "newspaper": "example-news", "newspaper_country": "TR", "publish_date": "2026-09-01", "http_status": 404,
        "created_at": 1759000000,
        "list": {"publisher": "Example News", "list_state": None, "published": None, "detection_window": None},
        "probe": {"status": 0, "decision": None, "existing_tags": True, "subject_tag_notations": None,
                  "discussions": 0},
        "content": "y" * 900, "content_chars": 900}


async def test_build_case_falls_back_to_the_audit_then_the_row_and_skips_thin_bodies():
    c = BodyClient([_article(title="", newspaper=None, publish_date=None, id=0)])
    case = await intake.build_case(c, audit=AUDIT, list_row={"audit_id": 5}, retry_sleep_s=0)
    assert (case["article_id"], case["title"], case["newspaper"], case["publish_date"]) == \
        (99, "Audit title", "example-np", "2026-08-01")
    c = BodyClient([None, None])
    assert await intake.build_case(c, audit={}, list_row={"audit_id": 6, "url": "https://daily.example.net/a/1"},
                                   retry_sleep_s=0) == intake.Skip("thin_or_missing_body", 0)
    c = BodyClient([_article(versions=[{"news_text": "z" * 399}])])
    assert await intake.build_case(c, audit=AUDIT, retry_sleep_s=0) == intake.Skip("thin_or_missing_body", 399)
    assert intake.MIN_BODY_CHARS == 400


# --- applier ---------------------------------------------------------------------------------

class FakeArchive:
    """The archive as apply_results.py sees it: get_audit / count_discussions / PATCH / POST."""

    def __init__(self, audit, discussions=0, patch_ok=True, patch_lands=None, post_id=1792):
        self.audit, self.discussions = (dict(audit) if audit is not None else None), discussions
        self.patch_ok = patch_ok
        self.patch_lands = patch_ok if patch_lands is None else patch_lands
        self.post_id, self.calls = post_id, []

    async def get_audit(self, aid):
        self.calls.append(("GET", aid))
        return dict(self.audit) if self.audit is not None else None

    async def count_discussions(self, aid):
        self.calls.append(("COUNT", aid))
        return self.discussions

    async def update_audit_classification(self, aid, **payload):
        self.calls.append(("PATCH", aid, payload))
        if self.patch_lands:
            self.audit.update(payload)
        return self.patch_ok

    async def post_discussion(self, aid, comment):
        self.calls.append(("POST", aid, comment))
        if self.post_id is None:
            return None
        self.discussions += 1
        return {"id": self.post_id, "audit_id": aid, "comment": comment}


def _open_audit(aid, **kw):
    a = {"id": aid, "status": 0, "decision": None, "subject_tag_notations": [],
         "content_type_notation": None, "geographic_scope": None}
    a.update(kw)
    return a


@pytest.fixture
def frozen_stamp(monkeypatch):
    monkeypatch.setattr(comments, "_stamp", lambda: "2026-10-05")


async def _apply(fake, record, entry, *, dry_run, **kw):
    saves, lines = [], []
    out = await applier.apply_record(fake, record, ledger_entry=entry, persist=lambda: saves.append(json.dumps(entry)),
                                     template="C", do_tags=True, do_comments=True, dry_run=dry_run,
                                     pacing_s=0, say=lines.append, **kw)
    return out, saves, lines


async def test_apply_sends_the_scripts_patch_and_post_and_writes_its_ledger_entry(frozen_stamp):
    r = _record("INCONCLUSIVE", flag="RELATED_REMOVALS")
    fake, entry = FakeArchive(_open_audit(r["audit_id"])), {}
    out, saves, lines = await _apply(fake, r, entry, dry_run=False)

    payload = applier.classification_payload(r)
    assert payload == {"subject_tag_notations": [t["notation"] for t in r["classification"]["subject_tags"]],
                       "content_type_notation": r["classification"]["genre"]["notation"],
                       "geographic_scope": r["classification"]["geographic_scope"]}
    text = comments.render("C", r, tags_state=rules.TAGS_SET)
    assert [c[0] for c in fake.calls] == ["GET", "PATCH", "COUNT", "POST"]
    assert fake.calls[1][2] == payload and fake.calls[3][2] == text
    assert set(entry) == {"tags", "comment"}
    assert list(entry["tags"]) == ["ok", "at", "payload"] and entry["tags"]["ok"] is True
    assert entry["tags"]["payload"] == payload
    assert list(entry["comment"]) == ["ok", "at", "template", "tags_state", "discussion_id", "view_url"]
    assert {k: v for k, v in entry["comment"].items() if k != "at"} == {
        "ok": True, "template": "C", "tags_state": "set", "discussion_id": 1792,
        "view_url": f"https://karakutu.xyz/audits/{r['audit_id']}/discussions#/news/{r['url']}"}
    assert len(saves) == 3, "write-ahead mark, tags, comment — each persisted immediately"
    assert out.tags.written and out.comment.posted and not out.proposed_wording
    assert lines[1] == f"    tags: PATCH /audits/{r['audit_id']} {json.dumps(payload, ensure_ascii=False)}"
    assert lines[2] == "    tags: written" and lines[-1] == "    comment: posted id 1792"


async def test_dry_run_sends_nothing_and_projects_the_set_wording(frozen_stamp):
    r = _record("LIKELY_MOVED")
    fake, entry = FakeArchive(_open_audit(r["audit_id"])), {}
    out, saves, lines = await _apply(fake, r, entry, dry_run=True)
    assert [c[0] for c in fake.calls] == ["GET", "COUNT"]
    assert entry == {} and saves == []
    assert out.comment.tags_state == "set" and out.comment.text == comments.render("C", r, tags_state="set")
    assert out.tags.decision.write and out.comment.decision.write and not out.tags.attempted


async def test_existing_tags_are_never_overwritten_and_the_comment_says_proposed(frozen_stamp):
    r = _record("NOT_REMOVED")
    fake = FakeArchive(_open_audit(r["audit_id"], subject_tag_notations=["medtop:1"]))
    entry = {}
    out, _, lines = await _apply(fake, r, entry, dry_run=False)
    assert [c[0] for c in fake.calls] == ["GET", "COUNT", "POST"]
    assert entry["comment"]["tags_state"] == "proposed_existing"
    assert "tags" not in entry and out.proposed_wording
    assert "PROPOSED ONLY" in fake.calls[-1][2]


@pytest.mark.parametrize("audit,discussions,stage,calls", [
    (None, 0, "missing", ["GET"]),
    ({"status": 1}, 0, "ineligible", ["GET"]),
    ({"status": 4}, 0, "ineligible", ["GET"]),
])
async def test_missing_and_ineligible_audits_are_left_alone(frozen_stamp, audit, discussions, stage, calls):
    r = _record("INCONCLUSIVE")
    fake = FakeArchive(None if audit is None else _open_audit(r["audit_id"], **audit), discussions)
    entry = {}
    out, saves, _ = await _apply(fake, r, entry, dry_run=False)
    assert out.stage == stage and [c[0] for c in fake.calls] == calls and entry == {} and saves == []


async def test_unusable_record_makes_no_call():
    r = _record("ERROR")
    fake = FakeArchive(_open_audit(r["audit_id"]))
    out, _, lines = await _apply(fake, r, {}, dry_run=False)
    assert out.stage == "unusable" and fake.calls == []
    assert lines == [f"\n  {r['audit_id']}: SKIP — verdict ERROR is not a result — re-run the audit first"]


async def test_a_commented_audit_gets_no_second_comment_and_the_ledger_prevents_repeats(frozen_stamp):
    r = _record("INCONCLUSIVE")
    fake, entry = FakeArchive(_open_audit(r["audit_id"])), {}
    await _apply(fake, r, entry, dry_run=False)
    n_calls = len(fake.calls)
    out, _, _ = await _apply(fake, r, entry, dry_run=False)          # re-run: idempotent
    assert [c[0] for c in fake.calls[n_calls:]] == ["GET", "COUNT"]
    assert out.tags.decision.reason == "already written (apply log)"
    assert out.comment.decision.reason == "already posted (apply log)"


async def test_frontend_url_is_a_parameter(frozen_stamp):
    r = _record("INCONCLUSIVE")
    fake, entry = FakeArchive(_open_audit(r["audit_id"])), {}
    await _apply(fake, r, entry, dry_run=False, frontend_url="https://staging.example/")
    assert entry["comment"]["view_url"].startswith(f"https://staging.example/audits/{r['audit_id']}/discussions#/news/")


# --- never "PROPOSED ONLY" for codes the bot actually set ------------------------

async def test_ambiguous_patch_failure_that_landed_is_confirmed_by_a_read(frozen_stamp):
    r = _record("INCONCLUSIVE")
    fake = FakeArchive(_open_audit(r["audit_id"]), patch_ok=False, patch_lands=True)  # timeout after commit
    entry = {}
    out, _, lines = await _apply(fake, r, entry, dry_run=False)
    assert [c[0] for c in fake.calls] == ["GET", "PATCH", "GET", "COUNT", "POST"]
    assert entry["tags"]["ok"] is True and entry["tags"]["confirmed_by_read"] is True
    assert entry["comment"]["tags_state"] == "set" and "    tags: written" in lines


async def test_a_patch_that_did_not_land_stays_failed_and_says_proposed(frozen_stamp):
    r = _record("INCONCLUSIVE")
    fake = FakeArchive(_open_audit(r["audit_id"]), patch_ok=False, patch_lands=False)
    entry = {}
    out, _, lines = await _apply(fake, r, entry, dry_run=False)
    assert list(entry["tags"]) == ["ok", "at", "payload"] and entry["tags"]["ok"] is False
    assert entry["comment"]["tags_state"] == "proposed" and "    tags: FAILED" in lines


async def test_crash_after_patch_is_reconciled_from_the_write_ahead_mark(frozen_stamp):
    r = _record("INCONCLUSIVE")
    payload = applier.classification_payload(r)
    # the previous run sent the PATCH (it landed) and died before the ledger write
    fake = FakeArchive(_open_audit(r["audit_id"], **payload))
    entry = {"tags": {"ok": False, "pending": True, "at": 1.0, "payload": payload}}
    out, _, _ = await _apply(fake, r, entry, dry_run=False)
    assert out.reconciled_pending is True
    assert entry["tags"] == {"ok": True, "at": 1.0, "payload": payload, "reconciled": True}
    assert "PATCH" not in [c[0] for c in fake.calls]
    assert entry["comment"]["tags_state"] == "set"
    assert "set on this audit by the bot" in fake.calls[-1][2]


async def test_crash_before_patch_drops_the_mark_and_writes(frozen_stamp):
    r = _record("INCONCLUSIVE")
    payload = applier.classification_payload(r)
    fake = FakeArchive(_open_audit(r["audit_id"]))
    entry = {"tags": {"ok": False, "pending": True, "at": 1.0, "payload": payload}}
    out, _, _ = await _apply(fake, r, entry, dry_run=False)
    assert out.reconciled_pending is False
    assert [c[0] for c in fake.calls] == ["GET", "PATCH", "COUNT", "POST"]
    assert entry["tags"]["ok"] is True and "pending" not in entry["tags"]


async def test_dry_run_reconciles_a_pending_mark_on_a_copy_only(frozen_stamp):
    r = _record("INCONCLUSIVE")
    payload = applier.classification_payload(r)
    fake = FakeArchive(_open_audit(r["audit_id"], **payload))
    entry = {"tags": {"ok": False, "pending": True, "at": 1.0, "payload": payload}}
    before = json.dumps(entry)
    out, saves, _ = await _apply(fake, r, entry, dry_run=True)
    assert json.dumps(entry) == before and saves == []
    assert out.comment.tags_state == "set"


def test_tags_state_for_comment_is_unchanged_for_the_normal_path():
    a_clean, a_tagged = _open_audit(1), _open_audit(1, geographic_scope="national")
    f = rules.tags_state_for_comment
    assert f(a_clean, {"tags": {"ok": True}}, projected_write=False, dry_run=False) == "set"
    assert f(a_clean, {}, projected_write=True, dry_run=True) == "set"
    assert f(a_clean, {}, projected_write=True, dry_run=False) == "proposed"
    assert f(a_tagged, {}, projected_write=False, dry_run=False) == "proposed_existing"
    assert f(a_tagged, {"tags": {"ok": False, "pending": True}}, projected_write=False, dry_run=False) \
        == "proposed_existing", "an unreconciled pending mark never reads as set"


# --- KarakutuClient.get_with_status -----------------------------------------------------------

class FakeAuth:
    available = True

    def __init__(self):
        self.forced = 0

    async def get_token(self, force=False):
        self.forced += force
        return "tok"


@pytest.fixture
def archive(monkeypatch):
    box = {"answers": [], "requests": []}
    real = httpx.AsyncClient

    def handler(request):
        box["requests"].append(request)
        ans = box["answers"].pop(0)
        if isinstance(ans, Exception):
            raise ans
        return httpx.Response(ans[0], json=ans[1])

    monkeypatch.setattr(karakutu_module.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    return box


def _client():
    return KarakutuClient(base_url="https://api.test", auth=FakeAuth())


async def test_get_with_status_tells_gone_from_unreachable(archive):
    archive["answers"] = [(404, {"detail": "not found"})]
    assert await _client().get_with_status("/audits/1", backoff_s=0) == (404, {"detail": "not found"})
    archive["answers"] = [(200, {"id": 1})]
    assert await _client().get_with_status("/audits/1", backoff_s=0) == (200, {"id": 1})
    archive["answers"] = [(503, {})] * 4
    assert await _client().get_with_status("/audits/1", backoff_s=0) == (None, {"error": "HTTP 503"})
    archive["answers"] = [httpx.ConnectError("secret-host.internal refused")] * 4
    status, body = await _client().get_with_status("/audits/1", backoff_s=0)
    assert (status, body) == (None, {"error": "ConnectError"}), "type name only, never the exception text"


async def test_get_with_status_retries_5xx_then_succeeds_and_remints_on_401(archive):
    archive["answers"] = [(502, {}), (200, {"total": 0})]
    assert await _client().get_with_status("/x", params={"page_size": 1}, backoff_s=0) == (200, {"total": 0})
    assert archive["requests"][-1].url.params["page_size"] == "1"
    c = _client()
    archive["answers"] = [(401, {}), (200, {"ok": 1})]
    assert await c.get_with_status("/x", backoff_s=0) == (200, {"ok": 1}) and c.auth.forced == 1


async def test_get_with_status_disabled_client_makes_no_call(archive):
    c = _client()
    c.auth.available = False
    assert await c.get_with_status("/x") == (None, {"error": "disabled"}) and archive["requests"] == []


# --- runtime ---------------------------------------------------------------------------------

async def test_lifespan_builds_its_state_through_build_runtime():
    from src.api.app import app, lifespan
    from src.runtime import Runtime

    async with lifespan(app):
        rt = app.state.runtime
        assert isinstance(rt, Runtime)
        assert (app.state.agents, app.state.search_provider, app.state.karakutu_client, app.state.catalog,
                app.state.model) == (rt.agents, rt.search_provider, rt.karakutu_client, rt.catalog, rt.model)
