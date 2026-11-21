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
from audit.demo import seed
from audit.domain import Article, AuditRequest, StageName
from audit.engine import AuditEngine
from audit.provenance import build_workflow
from audit.record import AuditRecordV1, NOTICE, RecordError, build_record, record_json_schema

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "docs" / "audit-record.v1.schema.json"
SERIALIZED_AT = "2026-11-20T09:30:00+00:00"


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_sha256(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


@pytest.fixture(scope="module")
def validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validate(validator, record):
    validator.validate(json.loads(json.dumps(record, allow_nan=False)))
    assert AuditRecordV1.model_validate(record).model_dump(mode="json", by_alias=True) == record


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


async def test_real_engine_record_with_captured_model_call(tmp_path, validator):
    engine = AuditEngine({"model_enabled": True, "model_name": "fabricated-requested-model",
                          "thinking_control": "reasoning_effort", "max_tokens": 512}, client=FakeClient())
    run, workflow = await stored_audit(tmp_path, engine)
    original = deepcopy((run, workflow))
    record = build_record(run, workflow, article_ref="ka-42", archive_id="42", serialized_at=SERIALIZED_AT)
    validate(validator, record)
    assert (run, workflow) == original
    assert record["record_id"] == run["id"] + ".r0"
    assert record["lineage"] == {"kind": "initial"}
    assert record["schema"] == "karakutu-ai-audit/audit-record"
    assert record["schema_version"] == "1.0"
    assert record["notice"] == NOTICE
    assert record["request"] == run["input"]
    assert record["result"] == run["output"]
    assert record["model"] == run["model"]
    assert record["article"] == {
        "ref": "ka-42", "archive_id": "42", "url": run["input"]["removed_article"]["url"],
        "title": run["input"]["removed_article"]["title"],
        "published_at": run["input"]["removed_article"]["published_at"],
        "content_sha256": sha256(run["input"]["removed_article"]["content"])}
    assert record["producer"]["serialized_at"] == SERIALIZED_AT
    assert not {"messages", "raw_completion", "extra_body"} & set(all_keys(record))
    step = next(step for step in run["steps"] if step["name"] == "relevance")
    stored = step["model_calls"][0]
    call = record["steps"][2]["model_calls"][0]
    assert stored["raw_completion"] is not None
    assert stored["request"]["extra_body"] == {"reasoning_effort": "none"}
    system, user = stored["request"]["messages"]
    assert system["content"] not in json.dumps(call)
    assert user["content"] not in json.dumps(call)
    assert call == {
        "requested_model": "fabricated-requested-model", "response_model": "fabricated-served-model",
        "response_format": "json_schema", "temperature": 0.0, "max_tokens": 400,
        "system_prompt_sha256": sha256(system["content"]),
        "user_prompt_sha256": sha256(user["content"]),
        "request_sha256": json_sha256(stored["request"]),
        "completion_sha256": json_sha256(stored["raw_completion"]), "parsed_output": stored["parsed_output"],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}, "error_type": None}
    provenance = record["provenance"]
    definition = workflow["definition"]
    assert provenance["workflow_id"] == run["workflow_id"]
    assert provenance["models"] == definition["models"]
    assert provenance["settings"] == definition["settings"]
    assert provenance["steps"] == [stage.value for stage in StageName]
    assert provenance["sources"] == {
        "files": len(definition["sources"]), "sha256": json_sha256(definition["sources"])}
    assert provenance["dependencies"] == {name: sha256(text) for name, text in definition["dependencies"].items()}
    assert provenance["prompts"] == {name: {"sha256": sha256(text), "text": text}
                                    for name, text in definition["prompts"].items()}
    assert record["redactions"] == []


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
    assert record["result"]["verdict"] == "AUDIT_FAILED"
    assert record["run"]["status"] == "failed"
    assert record["error"]["code"] == "audit_failed"
    assert record["run"]["synthetic"] is True
    assert all(not step["model_calls"] for step in record["steps"])
    # The store links every run to an article; no archive id was given.
    assert record["article"]["ref"] == run["article_ref"] and record["article"]["ref"].startswith("u-")
    assert record["article"]["archive_id"] is None


def test_finished_live_demo_payloads(tmp_path, validator):
    url = f"sqlite:///{tmp_path}/demo-test.sqlite3"
    seed(url)
    store = Store(url)
    count = 0
    try:
        for summary in store.runs(limit=200)["items"]:
            if summary["category"] != "live" or summary["status"] not in {"succeeded", "failed"}:
                continue
            demo = store.run(summary["id"])
            workflow = store.workflow(demo["workflow_id"])
            # Demo IDs deliberately use run_demo_*; create a new stored run with the
            # same payload to exercise the production record ID contract unchanged.
            run = store.create_run(workflow_id=workflow["id"], category="live", origin=demo["origin"],
                                   input=demo["input"], synthetic=demo["synthetic"])
            store.start_run(run["id"])
            store.complete_run(run["id"], {"response": demo["output"], "steps": demo["steps"],
                                          "model": demo["model"], "synthetic": demo["synthetic"]})
            record = build_record(store.run(run["id"]), workflow)
            validate(validator, record)
            assert record["result"] == demo["output"]
            definition = workflow["definition"]
            assert record["provenance"]["models"] == definition.get("models")
            assert set(record["provenance"]["prompts"]) == set(definition.get("prompts", {}))
            assert record["article"]["ref"] == run["article_ref"]
            count += 1
    finally:
        store.close()
    assert count == 2


@pytest.mark.parametrize("change,code", [
    ({"category": "benchmark"}, "not_live"),
    ({"status": "queued"}, "not_finished"),
    ({"status": "running"}, "not_finished"),
    ({"finished_at": None}, "not_finished"),
    ({"output": None, "error": {"code": "interrupted"}}, "no_output"),
])
async def test_refuses_ineligible_runs(finished_run, change, code):
    run, workflow = finished_run
    with pytest.raises(RecordError) as error:
        build_record({**run, **change}, workflow)
    assert error.value.code == str(error.value) == code


@pytest.mark.parametrize("wrong", [None, {"id": "wf_wrong"}])
async def test_refuses_wrong_workflow(finished_run, wrong):
    run, _ = finished_run
    with pytest.raises(RecordError, match="^workflow_mismatch$"):
        build_record(run, wrong)


@pytest.mark.parametrize("mutation", ["reversed", "missing", "duplicate"])
async def test_refuses_wrong_step_order(finished_run, mutation):
    run, workflow = finished_run
    if mutation == "reversed":
        run["steps"].reverse()
    elif mutation == "missing":
        run["steps"].pop()
    else:
        run["steps"][1] = deepcopy(run["steps"][0])
    with pytest.raises(RecordError, match="^step_order$"):
        build_record(run, workflow)


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


async def test_redaction_and_json_pointer_escaping(finished_run, validator):
    run, workflow = deepcopy(finished_run)
    event = {"Authorization": "Bearer abc", "headers": {"set-cookie": "x"},
             "a~/b": [{"ACCESS-TOKEN": "fabricated sensitive value"}],
             "prompt_tokens": 12, "total_tokens": 18, "error_type": "ValueError",
             "authorization_extra": "retained", "exception": {"nested": "omitted"}}
    run["steps"][0]["evidence"].append(event)
    run["model"]["wallet_path"] = "fabricated wallet path"
    workflow["definition"]["settings"]["API-KEY"] = "fabricated key"
    original = deepcopy((run, workflow))
    record = build_record(run, workflow)
    validate(validator, record)
    assert (run, workflow) == original
    evidence = record["steps"][0]["evidence"][0]
    assert evidence == {**event, "Authorization": "[redacted]", "headers": {"set-cookie": "[redacted]"},
                        "a~/b": [{"ACCESS-TOKEN": "[redacted]"}], "exception": "[redacted]"}
    assert record["redactions"] == sorted([
        "/steps/0/evidence/0/Authorization", "/steps/0/evidence/0/headers/set-cookie",
        "/steps/0/evidence/0/a~0~1b/0/ACCESS-TOKEN", "/steps/0/evidence/0/exception",
        "/model/wallet_path", "/provenance/settings/API-KEY"])


@pytest.mark.parametrize("location", ["value", "key", "prompt"])
async def test_secret_scan_includes_keys_and_prompt_templates(finished_run, location):
    run, workflow = deepcopy(finished_run)
    secret = "fabricated-sensitive-marker"
    if location == "value":
        run["steps"][0]["evidence"].append({"description": "prefix " + secret + " suffix"})
    elif location == "key":
        run["model"]["prefix " + secret] = None
    else:
        workflow["definition"]["prompts"]["relevance"] = "template " + secret
    with pytest.raises(RecordError, match="^secret_detected$"):
        build_record(run, workflow, secret_values=(secret,))


async def test_secret_scan_after_redaction_and_covers_short_values(finished_run, validator):
    run, workflow = finished_run
    run["model"].update({"token": "fabricated-sensitive-marker"})
    record = build_record(run, workflow, secret_values=("fabricated-sensitive-marker", ""))
    validate(validator, record)
    assert record["model"] == {**run["model"], "token": "[redacted]"}
    run["model"]["short"] = "k7z"
    with pytest.raises(RecordError) as error:
        build_record(run, workflow, secret_values=("k7z",))
    assert error.value.code == "secret_detected"


async def test_completion_hash_changes_with_the_exact_completion(tmp_path):
    engine = AuditEngine({"model_enabled": True, "model_name": "fabricated-requested-model"},
                         client=FakeClient())
    run, workflow = await stored_audit(tmp_path, engine)
    call = run["steps"][2]["model_calls"][0]
    first = build_record(run, workflow)["steps"][2]["model_calls"][0]["completion_sha256"]
    call["raw_completion"] = {**call["raw_completion"], "id": "fabricated-other-completion"}
    second = build_record(run, workflow)["steps"][2]["model_calls"][0]["completion_sha256"]
    assert first and second and first != second


async def test_upstream_error_text_in_evidence_is_redacted(finished_run, validator):
    run, workflow = finished_run
    run["steps"][1]["evidence"].append({"type": "fetch_candidates", "error_type": None, "output": {
        "error": "RuntimeError: fabricated upstream diagnostic", "detail": {"message": "fabricated"}}})
    record = build_record(run, workflow)
    validate(validator, record)
    assert "fabricated upstream diagnostic" not in json.dumps(record)
    assert {"/steps/1/evidence/" + str(len(run["steps"][1]["evidence"]) - 1) + "/output/" + key
            for key in ("error", "detail")} <= set(record["redactions"])
    assert record["error"] == run["error"]  # the service's own fixed error message stays


async def test_archive_response_bodies_become_digests(finished_run, validator):
    run, workflow = finished_run
    body = {"id": 7, "detail": "fabricated", "notes": "RuntimeError: fabricated upstream diagnostic"}
    run["steps"][1]["evidence"].append({"type": "archive_get", "path": "/articles", "params": {"url": "x"},
                                        "status": 200, "error_type": None, "output": body})
    run["steps"][1]["evidence"].append({"type": "archive_get", "path": "/articles", "params": None,
                                        "status": None, "error_type": "ConnectError", "output": None})
    record = build_record(run, workflow)
    validate(validator, record)
    assert "fabricated upstream diagnostic" not in json.dumps(record)
    stored, failed = record["steps"][1]["evidence"][-2:]
    assert stored["output"] == {"omitted": "archive_response", "sha256": json_sha256(body)}
    assert (stored["path"], stored["status"]) == ("/articles", 200)
    assert failed["output"] is None and failed["error_type"] == "ConnectError"


@pytest.mark.parametrize("stamp", ["20261120T093000+0000", "2026-11-20 09:30:00+00:00",
                                   "2026-11-20T09:30:00", "2026-02-30T09:30:00+00:00"])
async def test_serialized_at_must_be_rfc3339(finished_run, stamp):
    run, workflow = finished_run
    with pytest.raises(ValidationError):
        build_record(run, workflow, serialized_at=stamp)


async def test_timestamps_need_an_offset_but_published_at_is_kept_as_submitted(finished_run, validator):
    run, workflow = finished_run
    run["input"]["removed_article"]["published_at"] = "2026-11-01T10:00:00"
    record = build_record(run, workflow)
    validate(validator, record)
    assert record["article"]["published_at"] == "2026-11-01T10:00:00"
    run["finished_at"] = "2026-11-20T10:00:00"
    with pytest.raises(RecordError) as error:
        build_record(run, workflow)
    assert error.value.code == "invalid_timestamp"
    run["finished_at"], run["created_at"] = "2026-11-20T10:00:00+00:00", "2026-11-20T09:00:00+00:00"
    with pytest.raises(ValidationError):
        build_record(run, workflow, serialized_at="2026-11-20T10:00:00")


async def test_classification_catalog_is_replaced_by_digest(finished_run, validator):
    run, workflow = finished_run
    catalog = {"topics": [{"notation": "medtop:1", "label": "Fictional topic"}],
               "genres": [{"notation": "genre:Current"}, {"notation": "genre:Opinion"}],
               "locale": "en-GB"}
    run["steps"][0]["input"]["catalog"] = deepcopy(catalog)
    record = build_record(run, workflow)
    validate(validator, record)
    assert record["steps"][0]["input"]["catalog"] == {
        "omitted": "catalog", "sha256": json_sha256(catalog), "topics": 1, "genres": 2}
    assert run["steps"][0]["input"]["catalog"] == catalog


async def test_model_call_missing_fields_and_first_message_hashes(finished_run, validator):
    run, workflow = finished_run
    request = {"messages": [{"role": "system", "content": "first system"},
                            {"role": "system", "content": "second system"}]}
    run["steps"][0]["model_calls"] = [{"request": request, "error_type": "ValueError"},
                                      {"request": {}, "parsed_output": [None, "fabricated", 3, True]}]
    record = build_record(run, workflow)
    validate(validator, record)
    first, second = record["steps"][0]["model_calls"]
    assert first == {
        "requested_model": None, "response_model": None, "response_format": None,
        "temperature": None, "max_tokens": None, "system_prompt_sha256": sha256("first system"),
        "user_prompt_sha256": None, "request_sha256": json_sha256(request),
        "completion_sha256": None, "parsed_output": None, "usage": None, "error_type": "ValueError"}
    assert second["system_prompt_sha256"] is second["user_prompt_sha256"] is None
    assert second["parsed_output"] is None  # only structured (object) answers are kept


async def test_duration_includes_queue_time_and_preserves_timestamp_strings(finished_run, validator):
    run, workflow = finished_run
    run.update(created_at="2026-11-20T09:00:00+00:00", finished_at="2026-11-20T10:00:02.125+01:00")
    run["input"]["removed_article"]["published_at"] = None
    record = build_record(run, workflow)
    validate(validator, record)
    assert record["run"]["duration_ms"] == 2125
    assert record["run"]["finished_at"] == run["finished_at"]
    assert record["article"]["published_at"] is None


async def test_schema_forbids_extra_fields_and_constrains_enums(finished_run, validator):
    record = build_record(*finished_run)
    for path in [(), ("request",), ("request", "removed_article"), ("article",), ("run",),
                 ("result",), ("result", "stages", 0), ("steps", 0), ("provenance",),
                 ("provenance", "sources"), ("provenance", "prompts", "relevance"), ("producer",),
                 ("lineage",), ("error",)]:
        changed = deepcopy(record)
        value = changed
        for key in path:
            value = value[key]
        value["unexpected"] = "fabricated"
        assert not validator.is_valid(changed)
        with pytest.raises(ValidationError):
            AuditRecordV1.model_validate(changed)
    for field in ("verdict", "flags", "stages"):
        changed = deepcopy(record)
        if field == "stages":
            changed["result"]["stages"][0]["stage"] = "invented_stage"
        else:
            changed["result"][field] = ["invented_flag"] if field == "flags" else "INVENTED_VERDICT"
        assert not validator.is_valid(changed)
    for size in (5, 7):
        changed = deepcopy(record)
        changed["steps"] = (changed["steps"] * 2)[:size]
        assert not validator.is_valid(changed)


async def test_article_reference_follows_the_run_and_article_origin(finished_run, validator):
    run, workflow = finished_run
    run = {**run, "article_ref": "u-0123456789abcdef", "origin": "article"}
    record = build_record(run, workflow)
    validate(validator, record)
    assert record["article"]["ref"] == "u-0123456789abcdef"
    assert record["run"]["origin"] == "article"
    explicit = build_record(run, workflow, article_ref="ka-7", archive_id="7")
    assert (explicit["article"]["ref"], explicit["article"]["archive_id"]) == ("ka-7", "7")


async def test_model_call_keeps_token_counts_and_structured_answers_only(tmp_path, validator):
    engine = AuditEngine({"model_enabled": True, "model_name": "fabricated-requested-model"},
                         client=FakeClient())
    run, workflow = await stored_audit(tmp_path, engine)
    call = run["steps"][2]["model_calls"][0]
    call["usage"] = {**call["usage"], "diagnostic": "RuntimeError: fabricated usage diagnostic",
                     "completion_tokens_details": {"reasoning_tokens": 3}, "total_tokens": True}
    run["steps"][3]["model_calls"] = [{"request": {"model": "m"}, "error_type": None,
        "parsed_output": "NONE\nRuntimeError: fabricated raw completion diagnostic",
        "raw_completion": {"choices": []}}]
    record = build_record(run, workflow)
    validate(validator, record)
    text = json.dumps(record)
    assert "fabricated usage diagnostic" not in text and "fabricated raw completion" not in text
    assert record["steps"][2]["model_calls"][0]["usage"] == {
        "prompt_tokens": call["usage"]["prompt_tokens"],
        "completion_tokens": call["usage"]["completion_tokens"], "total_tokens": None}
    free_text = record["steps"][3]["model_calls"][0]
    assert free_text["parsed_output"] is None and free_text["completion_sha256"]


@pytest.mark.parametrize("stamp", ["2026-11-20T09:30:00+00:60", "2026-11-20T09:30:00+24:00"])
async def test_rfc3339_offsets_are_range_checked(finished_run, stamp):
    run, workflow = finished_run
    with pytest.raises(ValidationError):
        build_record(run, workflow, serialized_at=stamp)


async def test_credentials_in_urls_are_redacted_everywhere(finished_run, validator):
    run, workflow = finished_run
    article = run["input"]["removed_article"]
    article["url"] = "https://news.example/story?id=7&access_token=fictional%2Fcredential#sig=abc"
    run["steps"][4]["evidence"].append({"type": "fetch_candidates", "error_type": None, "output": {
        "live": [{"url": "https://reader:fictional-pass@mirror.example/a?Key=1&page=2"}],
        "note": "see https://cdn.example/x?X-Amz-Signature=fabricated and more text"}})
    record = build_record(run, workflow)
    validate(validator, record)
    text = json.dumps(record)
    for leaked in ("fictional%2Fcredential", "fictional-pass", "sig=abc", "Key=1", "=fabricated"):
        assert leaked not in text
    assert record["article"]["url"] == "https://news.example/story?id=7&access_token=[redacted]#sig=[redacted]"
    live = record["steps"][4]["evidence"][-1]["output"]
    assert live["live"][0]["url"] == "https://[redacted]@mirror.example/a?Key=[redacted]&page=2"
    assert live["note"] == "see https://cdn.example/x?X-Amz-Signature=[redacted] and more text"
    assert {"/article/url", "/request/removed_article/url"} <= set(record["redactions"])


async def test_secret_scan_sees_url_encoded_values(finished_run):
    run, workflow = finished_run
    run["input"]["removed_article"]["content"] += " ref=fictional%2Fcredential+value"
    for secret in ("fictional/credential", "credential value"):
        with pytest.raises(RecordError) as error:
            build_record(run, workflow, secret_values=(secret,))
        assert error.value.code == "secret_detected"


async def test_nested_url_encoding_is_scrubbed_and_scanned(finished_run, validator):
    run, workflow = finished_run
    inner = "https%3A%2F%2Fmirror.example%2Fx%3Faccess_token%3Dfictional%252Fcredential"
    run["input"]["removed_article"]["url"] = f"https://news.example/story?id=7&next={inner}"
    record = build_record(run, workflow, secret_values=("fictional/credential",))
    validate(validator, record)
    assert record["article"]["url"] == "https://news.example/story?id=7&next=[redacted]"
    run["input"]["removed_article"]["content"] += " " + inner.replace("%25", "%2525")
    with pytest.raises(RecordError) as error:
        build_record(run, workflow)
    assert error.value.code == "credential_detected"


async def test_html_escaped_parameters_are_scrubbed(finished_run, validator):
    run, workflow = finished_run
    run["input"]["removed_article"]["content"] += (
        ' <a href="https://news.example/story?id=7&amp;access_token=fictional-html-token">x</a>')
    record = build_record(run, workflow)
    validate(validator, record)
    assert "fictional-html-token" not in json.dumps(record)
    assert "&amp;access_token=[redacted]" in record["request"]["removed_article"]["content"]


async def test_credentials_in_prompt_templates_refuse_the_record(finished_run):
    run, workflow = finished_run
    prompts = workflow["definition"]["prompts"]
    name = next(iter(prompts))
    prompts[name] += "\nSee https://reader:fictional-pass@news.example/reference for context."
    with pytest.raises(RecordError) as error:
        build_record(run, workflow)
    assert error.value.code == "credential_detected"


def test_endlessly_encoded_text_fails_closed():
    from audit.record import _has_credential
    assert _has_credential("%" + "25" * 8 + "41")
    assert not _has_credential("Plain text with 100% of the fabricated facts & more.")


@pytest.mark.parametrize("link", ["//reader:fictional-password@news.example/story",
                                  "https:\\\\reader:fictional-password@news.example/story",
                                  "https://news.example/x?access_token=[redacted]fictional-secret"])
async def test_scheme_relative_and_fake_marker_credentials(finished_run, validator, link):
    run, workflow = finished_run
    run["input"]["removed_article"]["content"] += f" {link} end"
    record = build_record(run, workflow)
    validate(validator, record)
    assert "fictional-password" not in json.dumps(record) and "fictional-secret" not in json.dumps(record)
    prompts = workflow["definition"]["prompts"]
    prompts[next(iter(prompts))] += f"\n{link}\n"
    with pytest.raises(RecordError) as error:
        build_record(run, workflow)
    assert error.value.code == "credential_detected"


@pytest.mark.parametrize("secret", ["fictional+credential", "fictional%2Bcredential", "fictional credential"])
async def test_secret_scan_checks_every_decoding_state(finished_run, secret):
    run, workflow = finished_run
    run["input"]["removed_article"]["content"] += " ref fictional%252Bcredential"
    with pytest.raises(RecordError) as error:
        build_record(run, workflow, secret_values=(secret,))
    assert error.value.code == "secret_detected"


async def test_secret_scan_checks_each_single_transformation(finished_run):
    run, workflow = finished_run
    run["input"]["removed_article"]["content"] += " ref fictional%26amp%3Bcredential"
    with pytest.raises(RecordError) as error:
        build_record(run, workflow, secret_values=("fictional&amp;credential",))
    assert error.value.code == "secret_detected"


async def test_userinfo_must_be_exactly_the_marker(finished_run, validator):
    run, workflow = finished_run
    link = "https://[redacted]@reader:fictional-password@news.example/reference"
    run["input"]["removed_article"]["content"] += f" {link} end"
    record = build_record(run, workflow)
    validate(validator, record)
    assert "fictional-password" not in json.dumps(record)
    prompts = workflow["definition"]["prompts"]
    prompts[next(iter(prompts))] += f"\n{link}\n"
    with pytest.raises(RecordError) as error:
        build_record(run, workflow)
    assert error.value.code == "credential_detected"


async def test_numeric_secrets_are_found_in_numbers(tmp_path):
    engine = AuditEngine({"model_enabled": True, "model_name": "fabricated-requested-model"},
                         client=FakeClient())
    run, workflow = await stored_audit(tmp_path, engine)
    run["steps"][2]["model_calls"][0]["usage"]["prompt_tokens"] = 918273645
    with pytest.raises(RecordError) as error:
        build_record(run, workflow, secret_values=("918273645",))
    assert error.value.code == "secret_detected"
