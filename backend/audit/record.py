"""Portable audit record v1; see docs/audit-record-schema.md for the contract."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import json
import re
from urllib.parse import unquote, unquote_plus
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from audit.domain import AuditFlag, GeographicScope, StageName, StageOutcome, Verdict

SCHEMA_VERSION = "1.0"
NOTICE = "Machine triage record. No verdict, including POSSIBLE_CENSORSHIP, is a finding of censorship; a person decides on independent evidence."
RecordId = Annotated[str, Field(pattern=r"^run_[0-9a-f]{32}\.r[0-9]+$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)")


def _rfc3339(value: str) -> str:
    """The service writes every timestamp it owns as RFC 3339 with an explicit offset."""
    if not RFC3339.fullmatch(value):
        raise ValueError("timestamp_not_rfc3339")
    datetime.fromisoformat(value)  # rejects impossible dates and times
    return value


DateTime = Annotated[str, AfterValidator(_rfc3339), Field(json_schema_extra={"format": "date-time"})]
JsonObject = dict[str, JsonValue]


class FixedObject(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Initial(FixedObject):
    kind: Literal["initial"]


class ReAudit(FixedObject):
    kind: Literal["re_audit"]
    previous_record_id: RecordId


class Correction(FixedObject):
    kind: Literal["correction"]
    previous_record_id: RecordId
    reason: Literal["record_error", "redaction"]


Lineage = Annotated[Initial | ReAudit | Correction, Field(discriminator="kind")]


class Article(FixedObject):
    url: str
    title: str
    content: str
    published_at: str | None


class Request(FixedObject):
    removed_article: Article
    similar_articles: list[Article]


class ArticleIdentity(FixedObject):
    # `ref` is this service's article reference (`ka-<archive id>` or `u-<url hash>`);
    # `archive_id` is karakutu's own article id, null when unknown.
    ref: Annotated[str, Field(min_length=1, max_length=80)] | None
    archive_id: Annotated[str, Field(min_length=1, max_length=80)] | None
    url: str
    title: str
    # As submitted: ISO 8601, the offset may be absent.
    published_at: str | None
    content_sha256: Sha256


class Run(FixedObject):
    id: str
    origin: Literal["api", "webhook", "article"]
    category: Literal["live"]
    status: Literal["succeeded", "failed"]
    synthetic: bool
    created_at: DateTime
    finished_at: DateTime
    duration_ms: int = Field(ge=0)


class ConceptTag(FixedObject):
    notation: str
    label: str


class Classification(FixedObject):
    subject_tags: list[ConceptTag] = Field(max_length=5)
    genre: ConceptTag | None
    geographic_scope: GeographicScope | None
    gist: str


class StageResult(FixedObject):
    stage: StageName
    outcome: StageOutcome
    confidence: float | None
    explanation: str
    skipped: bool
    details: JsonObject | None


class Result(FixedObject):
    verdict: Verdict
    stages: list[StageResult]
    short_circuited_at: StageName | None
    findings: str
    classification: Classification | None
    flags: list[AuditFlag]


class Error(FixedObject):
    code: Literal["audit_failed"]
    message: str


class Usage(FixedObject):
    prompt_tokens: Annotated[int, Field(ge=0)] | None
    completion_tokens: Annotated[int, Field(ge=0)] | None
    total_tokens: Annotated[int, Field(ge=0)] | None


class ModelCall(FixedObject):
    requested_model: str | None
    response_model: str | None
    response_format: str | None
    temperature: float | None
    max_tokens: int | None
    system_prompt_sha256: Sha256 | None
    user_prompt_sha256: Sha256 | None
    request_sha256: Sha256
    completion_sha256: Sha256 | None
    parsed_output: JsonObject | None
    usage: Usage | None
    error_type: str | None


class Step(FixedObject):
    name: StageName
    status: Literal["completed", "failed", "skipped"]
    model: str | None
    duration_ms: int = Field(ge=0)
    input: JsonObject
    output: JsonObject
    model_calls: list[ModelCall]
    evidence: list[JsonObject]


class Prompt(FixedObject):
    sha256: Sha256
    text: str


class Sources(FixedObject):
    files: int = Field(ge=0)
    sha256: Sha256


class Provenance(FixedObject):
    workflow_id: str
    workflow_created_at: DateTime
    format_version: int
    models: JsonObject | None
    prompts: dict[str, Prompt]
    steps: list[StageName]
    decision: str
    settings: JsonObject
    sources: Sources
    dependencies: dict[str, Sha256]


class Producer(FixedObject):
    service: Literal["karakutu-ai-audit"]
    serialized_at: DateTime


class AuditRecordV1(FixedObject):
    model_config = ConfigDict(populate_by_name=True)
    schema_name: Literal["karakutu-ai-audit/audit-record"] = Field(alias="schema")
    schema_version: str = Field(pattern=r"^1\.[0-9]+$")
    record_id: RecordId
    lineage: Lineage
    article: ArticleIdentity
    request: Request
    run: Run
    result: Result
    error: Error | None
    steps: list[Step] = Field(min_length=6, max_length=6)
    provenance: Provenance
    model: JsonObject
    producer: Producer
    redactions: list[Annotated[str, Field(pattern=r"^(?:/(?:[^~/]|~[01])*)*$")]]
    notice: Literal[NOTICE]


class RecordError(ValueError):
    """Safe refusal code, never containing stored run data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_sha256(value) -> str:
    return _sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _lineage(run_id: str, revision: int, lineage) -> dict:
    if type(revision) is not int or revision < 0:
        raise RecordError("invalid_lineage")
    try:
        value = TypeAdapter(Lineage).validate_python({"kind": "initial"} if lineage is None else lineage)
    except ValidationError:
        raise RecordError("invalid_lineage") from None
    if isinstance(value, Correction):
        valid = revision >= 1 and value.previous_record_id == f"{run_id}.r{revision - 1}"
    else:
        valid = revision == 0 and (not isinstance(value, ReAudit)
                                  or not value.previous_record_id.startswith(f"{run_id}."))
    if not valid:
        raise RecordError("invalid_lineage")
    return value.model_dump()


def _structured(output):
    """Only validated structured answers. A free-text answer (related removals) is not copied:
    the step output holds what was grounded from it, and `completion_sha256` fingerprints it."""
    return output if isinstance(output, dict) else None


def _usage(usage):
    """Token counts only; providers attach other metadata that is not ours to vouch for."""
    if not isinstance(usage, dict):
        return None
    return {key: value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
            for key, value in ((key, usage.get(key)) for key in
                               ("prompt_tokens", "completion_tokens", "total_tokens"))}


def _model_call(event: dict) -> dict:
    request = event["request"]

    def prompt_hash(role):
        message = next((m for m in request.get("messages", []) if m.get("role") == role), {})
        content = message.get("content")
        return _sha256(content) if content is not None else None

    return {
        "requested_model": request.get("model"), "response_model": event.get("response_model"),
        "response_format": event.get("response_format"), "temperature": request.get("temperature"),
        "max_tokens": request.get("max_tokens"), "system_prompt_sha256": prompt_hash("system"),
        "user_prompt_sha256": prompt_hash("user"), "request_sha256": _json_sha256(request),
        "completion_sha256": (_json_sha256(event["raw_completion"])
                              if event.get("raw_completion") is not None else None),
        "parsed_output": _structured(event.get("parsed_output")), "usage": _usage(event.get("usage")),
        "error_type": event.get("error_type"),
    }


def _step(step: dict) -> dict:
    value = deepcopy({key: step[key] for key in
                      ("name", "status", "duration_ms", "input", "output", "evidence")})
    value["model"] = step.get("model")
    if "catalog" in value["input"]:
        catalog = value["input"]["catalog"]
        value["input"]["catalog"] = {
            "omitted": "catalog", "sha256": _json_sha256(catalog),
            "topics": len(catalog.get("topics", [])), "genres": len(catalog.get("genres", [])),
        }
    for event in value["evidence"]:
        # Raw karakutu response bodies: karakutu holds them, and their shape is not ours to vouch
        # for (they may carry upstream diagnostics). What the step used is in its output.
        if event.get("type") == "archive_get" and event.get("output") is not None:
            event["output"] = {"omitted": "archive_response", "sha256": _json_sha256(event["output"])}
    value["model_calls"] = [_model_call(event) for event in step["model_calls"]]
    return value


REDACT_KEYS = {
    "authorization", "proxy_authorization", "cookie", "set_cookie", "api_key", "apikey", "x_api_key",
    "password", "passwd", "secret", "client_secret", "token", "access_token", "refresh_token",
    "id_token", "bearer", "jwk", "wallet", "wallet_json", "wallet_path", "private_key", "keyfile",
    "mnemonic", "traceback", "exception", "exc_info", "stack_trace",
}
# Evidence holds upstream response bodies verbatim; their error text may echo exceptions.
# The service's own evidence events never use these keys (they record `error_type` only).
UPSTREAM_ERROR_KEYS = {
    "error", "errors", "message", "detail", "error_message", "error_description", "stack",
    "stacktrace",
}


# Query or fragment parameters that carry credentials in URLs, matched like REDACT_KEYS.
URL_SECRET_PARAMS = REDACT_KEYS | {
    "key", "sig", "signature", "auth", "session", "sessionid", "sid", "credential", "credentials",
    "x_amz_signature", "x_amz_credential", "x_amz_security_token", "x_goog_signature",
    "x_goog_credential",
}
# Absolute and scheme-relative URLs (`//host`), with either slash direction as browsers accept.
URL = re.compile(r"(?:[a-z][a-z0-9+.-]*:)?[/\\]{2}[^\s\"'<>]+", re.IGNORECASE)
URL_USERINFO = re.compile(r"^((?:[a-z][a-z0-9+.-]*:)?[/\\]{2})[^/\\?#]*@", re.IGNORECASE)
# The marker counts as redacted only when it is the whole value.
_NOT_MARKER = r"(?!\[redacted\](?:[&#;\s\"'<>]|$))"


def _key_name(name: str) -> str:
    return unquote_plus(name).lower().replace("-", "_")


DECODE_ROUNDS = 5
_PARAM_NAMES = "|".join(sorted(re.escape(name).replace("_", "[-_]") for name in URL_SECRET_PARAMS))
# A credential parameter with a value, or URL userinfo, in fully decoded text.
CREDENTIAL = re.compile(
    rf"(?:^|[?&#;/\\\s])(?:{_PARAM_NAMES})\s*=\s*{_NOT_MARKER}[^&#;\s]", re.IGNORECASE)
# Everything up to the last `@` of an authority; it must be empty or exactly the marker.
USERINFO = re.compile(r"[/\\]{2}([^/\\?#\s]*)@")


def _has_credential(text: str) -> bool:
    states = _decodings(text)
    return states is None or any(
        CREDENTIAL.search(state) or any(info not in ("", "[redacted]") for info in USERINFO.findall(state))
        for state in states)


def _scrub_params(part: str) -> str:
    # `;` also separates, so an HTML-escaped `&amp;` splits into `amp` and the real parameter.
    pieces = re.split(r"([&;])", part)
    for index in range(0, len(pieces), 2):
        name, sep, value = pieces[index].partition("=")
        if sep and (_key_name(name) in URL_SECRET_PARAMS or _has_credential(value)):
            pieces[index] = f"{name}=[redacted]"
    return "".join(pieces)


def _scrub_url(match) -> str:
    """Drop userinfo and credential parameters; every other byte of the URL is kept."""
    url = URL_USERINFO.sub(r"\1[redacted]@", match.group())
    url, hash_, fragment = url.partition("#")
    base, mark, query = url.partition("?")
    url = base + mark + _scrub_params(query) if mark else base
    return url + hash_ + _scrub_params(fragment) if hash_ else url


def _pointer(path) -> str:
    return "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in path)


def _redact(value, path=()) -> list[str]:
    pointers = []
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = ((str(index), item) for index, item in enumerate(value))
    else:
        return pointers
    for key, item in list(items):
        child = (*path, key)
        slot = int(key) if isinstance(value, list) else key
        if len(child) == 4 and child[:2] == ("provenance", "prompts") and key == "text":
            continue
        name = key.lower().replace("-", "_")
        in_evidence = len(path) >= 3 and path[0] == "steps" and path[2] == "evidence"
        if isinstance(value, dict) and (name in REDACT_KEYS or (in_evidence and name in UPSTREAM_ERROR_KEYS)):
            value[slot] = "[redacted]"
            pointers.append(_pointer(child))
        elif isinstance(item, str):
            scrubbed = URL.sub(_scrub_url, item)
            if scrubbed != item:
                value[slot] = scrubbed
                pointers.append(_pointer(child))
        else:
            pointers.extend(_redact(item, child))
    return pointers


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _decodings(text: str) -> set[str] | None:
    """Every state reachable by single URL-decode (with or without `+` as space) and HTML-unescape
    steps, up to DECODE_ROUNDS deep; None when new states still appear at that depth (fail closed)."""
    states, frontier = {text}, {text}
    for _ in range(DECODE_ROUNDS):
        frontier = {step(state) for state in frontier for step in (unquote, unquote_plus, html.unescape)}
        frontier -= states
        if not frontier:
            return states
        states |= frontier
    return None


def _contains_secret(value, secrets) -> bool:
    if isinstance(value, str):
        forms = _decodings(value)
        return forms is None or any(secret in form for secret in secrets for form in forms)
    if isinstance(value, dict):
        return any(_contains_secret(key, secrets) or _contains_secret(item, secrets) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item, secrets) for item in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A provider can echo a numeric secret as a number (for example a token count).
        return any(secret in form for secret in secrets for form in {str(value), json.dumps(value)})
    return False


def build_record(run, workflow, *, article_ref=None, archive_id=None, revision=0, lineage=None,
                 secret_values=(), serialized_at=None) -> dict:
    if run["category"] != "live":
        raise RecordError("not_live")
    if run["status"] not in {"succeeded", "failed"} or not run.get("finished_at"):
        raise RecordError("not_finished")
    if run.get("output") is None:
        raise RecordError("no_output")
    if workflow is None or workflow.get("id") != run["workflow_id"]:
        raise RecordError("workflow_mismatch")
    if [step["name"] for step in run["steps"]] != [stage.value for stage in StageName]:
        raise RecordError("step_order")
    lineage = _lineage(run["id"], revision, lineage)
    article = run["input"]["removed_article"]
    definition = workflow["definition"]
    if not all(RFC3339.fullmatch(run[key]) for key in ("created_at", "finished_at")):
        raise RecordError("invalid_timestamp")
    created, finished = (datetime.fromisoformat(run[key]) for key in ("created_at", "finished_at"))
    duration = finished - created
    record = deepcopy({
        "schema": "karakutu-ai-audit/audit-record", "schema_version": SCHEMA_VERSION,
        "record_id": f"{run['id']}.r{revision}", "lineage": lineage,
        "article": {"ref": article_ref if article_ref is not None else run.get("article_ref"),
                    "archive_id": archive_id, "url": article["url"],
                    "title": article["title"], "published_at": article["published_at"],
                    "content_sha256": _sha256(article["content"])},
        "request": run["input"],
        "run": {**{key: run[key] for key in ("id", "origin", "category", "status", "synthetic",
                                            "created_at", "finished_at")},
                "duration_ms": round(duration.total_seconds() * 1000)},
        "result": run["output"], "error": run["error"],
        "steps": [_step(step) for step in run["steps"]],
        "provenance": {
            "workflow_id": workflow["id"], "workflow_created_at": workflow["created_at"],
            "format_version": definition["format_version"], "models": definition.get("models"),
            "prompts": {name: {"sha256": _sha256(text), "text": text}
                        for name, text in definition.get("prompts", {}).items()},
            "steps": [step["name"] for step in definition["steps"]], "decision": definition["decision"],
            "settings": definition["settings"],
            "sources": {"files": len(definition["sources"]), "sha256": _json_sha256(definition["sources"])},
            "dependencies": {name: _sha256(text) for name, text in definition["dependencies"].items()},
        },
        "model": run["model"],
        "producer": {"service": "karakutu-ai-audit", "serialized_at": serialized_at if serialized_at is not None
                     else datetime.now(timezone.utc).isoformat()},
        "redactions": [], "notice": NOTICE,
    })
    record["redactions"] = sorted(_redact(record))
    # Final fail-closed pass over every string, prompt templates included (they stay byte-exact,
    # so a credential there refuses the record instead of being redacted).
    if any(_has_credential(text) for text in _strings(record)):
        raise RecordError("credential_detected")
    if _contains_secret(record, tuple(s for s in secret_values if s)):
        raise RecordError("secret_detected")
    model = AuditRecordV1.model_validate(record)
    record = model.model_dump(mode="json", by_alias=True)
    serialized = json.dumps(record, allow_nan=False, ensure_ascii=False)
    if any(secret in serialized for secret in secret_values if secret):
        raise RecordError("secret_detected")
    return record


def record_json_schema() -> dict:
    return {**AuditRecordV1.model_json_schema(by_alias=True),
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:karakutu-ai-audit:audit-record:1",
            "title": "karakutu-ai-audit audit record v1"}
