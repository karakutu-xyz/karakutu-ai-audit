"""Portable records from fabricated, offline stored audits; committed schema validation."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from audit.db import Store
from audit.domain import Article, AuditRequest, StageName
from audit.engine import AuditEngine
from audit.provenance import build_workflow
from audit.record import (
    ARTICLE_URL_MAX_BYTES, AuditResult, NOTICE, RESULT_MAX_BYTES, RecordError, build_record, build_result,
    record_json_schema,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "docs" / "audit-record.v2.schema.json"


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validate(validator, record):
    validator.validate(json.loads(json.dumps(record, allow_nan=False)))
    assert AuditResult.model_validate(record).model_dump(mode="json", by_alias=True) == record


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


async def stored_audit(tmp_path, engine):
    store = Store(f"sqlite:///{tmp_path}/record-test.sqlite3")
    try:
        await engine.start()
        workflow = store.save_workflow(build_workflow(engine.public_config))
        request = AuditRequest(removed_article=Article(
            url="https://news.example/invented-ribbons", title="An invented ribbon inventory",
            content="A fictional workshop counted blue ribbons and reusable boxes. İcat edilmiş metin.",
            published_at=datetime(2026, 11, 1, 10, tzinfo=timezone.utc)),
            similar_articles=[Article(url="https://news.example/other", title="Another invented item",
                                      content="Fabricated comparison text.")])
        run = store.create_run(workflow_id=workflow["id"], category="live", origin="api",
                               input=request.model_dump(mode="json"), synthetic=engine.synthetic)
        store.start_run(run["id"])

        async def append_step(step):
            store.append_step(run["id"], step)

        result = await engine.run(request, on_step=append_step)
        # The durable callback path is exercised before complete_run replaces the list.
        assert store.run(run["id"])["steps"] == result["steps"]
        store.complete_run(run["id"], result)
        return store.run(run["id"]), store.workflow(workflow["id"])
    finally:
        await engine.close()
        store.close()


@pytest.fixture
async def finished_run(tmp_path):
    return await stored_audit(tmp_path, AuditEngine({}))


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **request):
        assert request["response_format"]["json_schema"]["name"] == "RelevanceResult"
        data = {"is_relevant": False, "confidence": .9, "reasoning": "Fictional craft inventory."}
        raw = json.dumps(data)
        envelope = {"model": "fabricated-served-model", "choices": [{"message": {"content": raw}}]}
        return SimpleNamespace(
            model=envelope["model"], usage=SimpleNamespace(model_dump=lambda **kw: {
                "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}),
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw))],
            model_dump=lambda **kw: envelope)

    async def close(self):
        pass


async def test_real_engine_record_excludes_private_payloads(tmp_path, validator):
    engine = AuditEngine({"model_enabled": True, "model_name": "fabricated-requested-model",
                          "max_tokens": 512}, client=FakeClient())
    run, workflow = await stored_audit(tmp_path, engine)
    # These deliberately identifiable private strings must never enter the projection.
    page = "Fabricated fetched page text that belongs only in private review."
    run["steps"][0]["evidence"].append({"text": page})
    original = deepcopy((run, workflow))
    record = build_record(run, workflow, article_ref="ka-42", archive_id="42")
    validate(validator, record)
    assert (run, workflow) == original
    assert record["record_id"] == run["id"] + ".r0"
    assert record["lineage"] == {"kind": "initial"}
    assert record["schema"] == "karakutu-ai-audit/audit-result"
    assert record["schema_version"] == "2.0"
    assert record["notice"] == NOTICE
    assert record["verdict"] == run["output"]["verdict"]
    assert record["findings"] == run["output"]["findings"]
    assert record["article"] == {
        "ref": "ka-42", "archive_id": "42", "url": run["input"]["removed_article"]["url"],
        "content_sha256": sha256(run["input"]["removed_article"]["content"])}
    assert record["run"]["workflow_id"] == workflow["id"]
    assert [s["stage"] for s in record["stages"]] == [s.value for s in StageName]
    text = json.dumps(record, ensure_ascii=False)
    for private in (page, run["input"]["removed_article"]["content"],
                    run["input"]["similar_articles"][0]["content"],
                    *workflow["definition"]["prompts"].values()):
        assert private not in text
    assert not {"input", "steps", "request", "model_calls", "prompts", "provenance",
                "producer", "redactions", "title", "text", "content"} & set(all_keys(record))


def test_committed_schema_matches_models():
    assert SCHEMA_PATH.read_text(encoding="utf-8") == json.dumps(record_json_schema(), indent=2, sort_keys=True) + "\n"


def test_export_script_writes_and_checks_schema(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "docs").mkdir()
    script = scripts / "export_record_schema.py"
    script.write_text((ROOT / "scripts" / script.name).read_text(encoding="utf-8"), encoding="utf-8")

    def execute(*args):
        return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)

    missing = execute("--check")
    assert missing.returncode != 0
    assert "Audit record schema is stale; run scripts/export_record_schema.py" in missing.stderr
    assert execute().returncode == 0
    path = tmp_path / "docs" / SCHEMA_PATH.name
    assert path.read_bytes() == SCHEMA_PATH.read_bytes()
    assert execute("--check").returncode == 0
    path.write_text("{}\n", encoding="utf-8")
    assert execute("--check").returncode != 0


async def test_disabled_model_record(finished_run, validator):
    run, workflow = finished_run
    record = build_record(run, workflow)
    validate(validator, record)
    assert record["verdict"] == "AUDIT_FAILED"
    assert record["run"]["status"] == "failed"
    assert record["error"] == run["error"]


@pytest.mark.parametrize("change,code", [
    ({"category": "benchmark"}, "not_live"),
    ({"status": "queued"}, "not_finished"),
    ({"status": "running"}, "not_finished"),
    ({"status": "cancelled"}, "not_finished"),
    ({"finished_at": None}, "not_finished"),
    ({"output": None, "error": {"code": "interrupted"}}, "not_finished"),
    ({"output": None, "error": None}, "no_output"),
])
async def test_refuses_ineligible_runs(finished_run, change, code):
    run, workflow = finished_run
    with pytest.raises(RecordError, match="^" + code + "$"):
        build_record({**run, **change}, workflow)


@pytest.mark.parametrize("wrong", [None, {"id": "wf_wrong"}])
async def test_refuses_wrong_workflow(finished_run, wrong):
    run, _ = finished_run
    with pytest.raises(RecordError, match="^workflow_mismatch$"):
        build_record(run, wrong)


@pytest.mark.parametrize("revision,kind,previous,reason", [
    (0, "initial", None, None),
    (0, "re_audit", "other", None),
    (1, "correction", "previous", "record_error"),
    (2, "correction", "previous", "redaction"),
])
async def test_valid_lineage(finished_run, validator, revision, kind, previous, reason):
    run, workflow = finished_run
    lineage = {"kind": kind}
    if previous:
        lineage["previous_record_id"] = (f"{run['id']}.r{revision - 1}" if previous == "previous"
                                          else "run_" + "b" * 32 + ".r0")
    if reason:
        lineage["reason"] = reason
    record = build_record(run, workflow, revision=revision, lineage=lineage)
    validate(validator, record)
    assert record["record_id"] == f"{run['id']}.r{revision}"
    assert record["lineage"] == lineage


@pytest.mark.parametrize("revision,lineage", [
    (1, {"kind": "initial"}), (-1, None), (True, None), (1.0, None), ("0", None),
    (0, {"kind": "correction", "previous_record_id": "same", "reason": "record_error"}),
    (1, {"kind": "correction", "previous_record_id": "run_" + "b" * 32 + ".r0", "reason": "redaction"}),
    (1, {"kind": "correction", "previous_record_id": "same", "reason": "unknown"}),
    (2, {"kind": "correction", "previous_record_id": "same", "reason": "record_error"}),
    (0, {"kind": "re_audit", "previous_record_id": "same"}),
    (1, {"kind": "re_audit", "previous_record_id": "run_" + "b" * 32 + ".r0"}),
    (0, {"kind": "re_audit", "previous_record_id": "invalid"}),
    (0, {"kind": "unknown"}), (0, {"kind": "initial", "unexpected": True}), (0, {}),
])
async def test_invalid_lineage(finished_run, revision, lineage):
    run, workflow = finished_run
    if lineage and lineage.get("previous_record_id") == "same":
        lineage = {**lineage, "previous_record_id": run["id"] + ".r0"}
    with pytest.raises(RecordError, match="^invalid_lineage$"):
        build_record(run, workflow, revision=revision, lineage=lineage)


@pytest.mark.parametrize("stamp", ["20261120T093000+0000", "2026-11-20 09:30:00+00:00",
                                   "2026-11-20T09:30:00", "2026-02-30T09:30:00+00:00",
                                   "2026-11-20T09:30:00+24:00"])
async def test_refuses_invalid_timestamps(finished_run, stamp):
    run, workflow = finished_run
    with pytest.raises(RecordError, match="^invalid_timestamp$"):
        build_record({**run, "finished_at": stamp}, workflow)


async def test_credentials_are_scrubbed_in_urls_and_refused_in_prose(finished_run, validator):
    run, workflow = finished_run
    run["input"]["removed_article"]["url"] = (
        "https://reader:fictional-pass@news.example/story?access_token=fictional-token")
    record = build_record(run, workflow, secret_values=("fictional-pass", "fictional-token"))
    validate(validator, record)
    assert record["article"]["url"] == (
        "https://[redacted]@news.example/story?access_token=[redacted]")
    run["output"]["findings"] = "Credential outside a URL: access_token=fictional-token"
    with pytest.raises(RecordError, match="^credential_detected$"):
        build_record(run, workflow)


@pytest.mark.parametrize("text,secret", [
    ("fictional-sensitive-marker", "fictional-sensitive-marker"),
    ("fictional%252Fsensitive+value", "fictional/sensitive"),
    ("fictional%252Fsensitive+value", "sensitive value"),
    ("fictional&#45;sensitive", "fictional-sensitive"),
    ("k7z", "k7z"),
])
async def test_secrets_refuse_in_every_decoding_state(finished_run, text, secret):
    run, workflow = finished_run
    run["output"]["findings"] = text
    with pytest.raises(RecordError, match="^secret_detected$"):
        build_record(run, workflow, secret_values=(secret, ""))


def test_endlessly_encoded_text_fails_closed():
    from audit.record import _has_credential, _contains_secret, _redact
    assert _has_credential("%" + "25" * 8 + "41")
    assert not _has_credential("Plain text with 100% of the fabricated facts & more.")
    assert _contains_secret({"fictional-key": 1234}, ("fictional",))
    assert _contains_secret(1234, ("234",))
    value = {"Authorization": "fabricated", "a/b": {"token": "fabricated"}}
    assert _redact(value) == ["/Authorization", "/a~1b/token"]
    assert value["a/b"]["token"] == "[redacted]"


async def test_evidence_uses_output_details_and_defaults_old_rows(finished_run, validator):
    run, workflow = finished_run
    by_stage = {s["stage"]: s for s in run["output"]["stages"]}
    by_stage["similarity"]["details"] = {"relocated_to": "https://news.example/sim", "old_in_new": .7,
                                           "new_in_old": .8}
    by_stage["source"]["details"] = {"self_status": "live_other"}
    web = {"relocated_to": "https://news.example/web", "old_in_new": .9, "new_in_old": .6,
           "covers_key_facts": True, "matches": ["https://mirror.example/copy"],
           "dead_same_story": [{"url": "https://mirror.example/dead", "reason": "HTTP 410"}],
           "related_same_publisher": ["https://news.example/related"],
           "search_queries": 5, "search_queries_failed": 2}
    by_stage["web_search"]["details"] = web
    run["steps"][5]["evidence"] = [{"matches": ["https://ignored.example/private"]}]
    result = build_record(run, workflow)
    validate(validator, result)
    assert result["evidence"] == {
        "relocated_to": web["relocated_to"], "old_in_new": .9, "new_in_old": .6,
        "covers_key_facts": True, "original_url_status": "live_other",
        "live_copies": web["matches"], "dead_copies": web["dead_same_story"],
        "related_same_publisher": web["related_same_publisher"],
        "search_queries": 5, "search_queries_failed": 2}
    by_stage["web_search"].pop("details")
    assert build_result(run)["evidence"]["relocated_to"] == "https://news.example/sim"
    assert build_result(run)["evidence"]["old_in_new"] == .7
    for stage in by_stage.values():
        stage["details"] = None
    evidence = build_result(run)["evidence"]
    assert evidence["live_copies"] == evidence["dead_copies"] == []
    assert all(value is None or value == [] for value in evidence.values())


@pytest.mark.parametrize("status,category,error", [
    ("queued", "live", None), ("running", "live", None),
    ("cancelled", "live", {"code": "cancelled"}),
    ("failed", "live", {"code": "interrupted"}),
    ("succeeded", "benchmark", None),
])
async def test_results_for_non_records(finished_run, status, category, error):
    run, _ = finished_run
    run.update(status=status, category=category, output=None, error=error)
    result = build_result(run)
    assert result["record_id"] is result["lineage"] is result["verdict"] is None
    assert result["evidence"] is result["findings"] is result["error"] is None
    assert result["stages"] == [] and result["flags"] == []


async def test_error_only_execution_failure_is_a_record(finished_run, validator):
    run, workflow = finished_run
    run["output"] = None
    record = build_record(run, workflow)
    validate(validator, record)
    assert record["record_id"] and record["error"]["code"] == "audit_failed"
    assert record["verdict"] is None and record["stages"] == []


async def test_schema_is_closed_and_record_constraints_are_stricter(finished_run, validator):
    record = build_record(*finished_run)
    for path in [(), ("article",), ("run",), ("stages", 0), ("lineage",), ("error",), ("evidence",)]:
        changed = deepcopy(record)
        target = changed
        for key in path:
            target = target[key]
        target["unexpected"] = "fabricated"
        assert not validator.is_valid(changed)
        with pytest.raises(ValidationError):
            AuditResult.model_validate(changed)
    for key, value in (("record_id", None), ("lineage", None)):
        assert not validator.is_valid({**record, key: value})
    for key, value in (("status", "queued"), ("finished_at", None), ("category", "benchmark")):
        assert not validator.is_valid({**record, "run": {**record["run"], key: value}})
    changed = deepcopy(record)
    changed["evidence"]["old_in_new"] = float("nan")
    with pytest.raises(ValidationError):
        AuditResult.model_validate(changed)


async def test_large_private_payload_stays_under_fixed_size_limit(finished_run, validator):
    assert RESULT_MAX_BYTES == 32768
    run, workflow = finished_run
    article = run["input"]["removed_article"]
    article["content"] = "F" * 250_000
    run["input"]["similar_articles"] = [deepcopy(article) for _ in range(5)]
    run["output"]["findings"] = "Fictional findings. " * 10000
    run["output"]["classification"] = {
        "subject_tags": [{"notation": f"medtop:{i}", "label": "Fictional topic"} for i in range(5)],
        "genre": {"notation": "genre:Current", "label": "Current"},
        "geographic_scope": "local", "gist": "Fictional gist " * 1000}
    long_url = "https://news.example/" + "x" * 600
    accepted_url = "https://news.example/" + "x" * (512 - len("https://news.example/"))
    for stage in run["output"]["stages"]:
        stage["explanation"] = "  Fictional explanation.\n " * 10000
        stage["details"] = {
            "relocated_to": accepted_url, "old_in_new": .9, "new_in_old": .8,
            "matches": [long_url] * 20 + [accepted_url] * 20,
            "dead_same_story": [{"url": url, "reason": "Fabricated reason. " * 100}
                                for url in [long_url] * 20 + [accepted_url] * 20],
            "related_same_publisher": [long_url] * 20 + [accepted_url] * 20}
    record = build_record(run, workflow)
    validate(validator, record)
    assert len(record["findings"].encode("utf-8")) == 2000 and record["findings"].endswith("…")
    assert len(record["classification"]["gist"].encode("utf-8")) == 300
    assert all(len(s["summary"].encode("utf-8")) == 300 and "\n" not in s["summary"] for s in record["stages"])
    assert len(json.dumps(record, ensure_ascii=False).encode("utf-8")) <= 16384  # ASCII: well under the bound
    for key in ("live_copies", "dead_copies", "related_same_publisher"):
        assert len(record["evidence"][key]) == 5
    assert all(len(d["reason"].encode("utf-8")) == 120 for d in record["evidence"]["dead_copies"])


async def test_size_guard_fails_closed(finished_run):
    run, workflow = finished_run
    run["input"]["removed_article"]["url"] = "https://news.example/" + "x" * ARTICLE_URL_MAX_BYTES
    with pytest.raises(RecordError, match="^too_large$"):
        build_record(run, workflow)


async def test_typical_record_is_a_few_kilobytes(finished_run):
    run, workflow = finished_run
    run["input"]["removed_article"]["content"] = "Fictional sentence. " * 12_500
    assert len(json.dumps(build_record(run, workflow), ensure_ascii=False).encode("utf-8")) < 6000


async def test_byte_caps_bound_a_worst_case_unicode_record(finished_run, validator):
    run, workflow = finished_run
    wide = "🧵"  # four UTF-8 bytes per character
    def url(n):
        return f"https://news{n}.example/" + wide * ((512 - 22) // 4)
    run["input"]["removed_article"]["url"] = "https://news.example/" + wide * ((ARTICLE_URL_MAX_BYTES - 21) // 4)
    run["output"]["findings"] = wide * 5000
    run["output"]["classification"] = {
        "subject_tags": [{"notation": wide * 100, "label": wide * 100} for _ in range(5)],
        "genre": {"notation": wide * 100, "label": wide * 100},
        "geographic_scope": "local", "gist": wide * 1000}
    for stage in run["output"]["stages"]:
        stage["explanation"] = wide * 1000
        stage["details"] = {
            "relocated_to": url(0), "matches": [url(i) for i in range(9)],
            "dead_same_story": [{"url": url(i), "reason": wide * 100} for i in range(9)],
            "related_same_publisher": [url(i) for i in range(9)]}
    record = build_record(run, workflow)
    validate(validator, record)
    size = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
    assert 16384 < size <= RESULT_MAX_BYTES
    assert len(record["findings"].encode("utf-8")) <= 2000
    assert all(len(s["summary"].encode("utf-8")) <= 300 for s in record["stages"])
    assert all(len(t["label"].encode("utf-8")) <= 120 for t in record["classification"]["subject_tags"])
    assert all(len(d["reason"].encode("utf-8")) <= 120 for d in record["evidence"]["dead_copies"])


async def test_archive_identity_and_explicit_record_overrides(finished_run):
    run, workflow = finished_run
    assert build_result(run)["article"]["archive_id"] is None
    archived = {**run, "article_ref": "ka-42"}
    assert build_result(archived)["article"]["archive_id"] == "42"
    record = build_record(run, workflow, article_ref="ka-7", archive_id="7")
    assert record["article"]["ref"] == "ka-7"
    assert record["article"]["archive_id"] == "7"
    assert run["article_ref"].startswith("u-")
