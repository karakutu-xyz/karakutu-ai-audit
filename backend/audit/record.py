"""Lean audit result v2; private review details stay on this service."""
from copy import deepcopy
from datetime import datetime
import hashlib
import html
import json
import math
import re
from urllib.parse import unquote, unquote_plus
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from audit.domain import AuditFlag, GeographicScope, StageName, StageOutcome, Verdict

SCHEMA_VERSION = "2.0"
# UTF-8 content budget: article URL 8192 B, two identities of 512 B each, findings
# 2000 B, six 300 B summaries, gist 300 B, twelve 120 B tag fields (five topics + genre),
# sixteen 512 B evidence URLs, five 120 B reasons: 23,548 B before JSON overhead.
# JSON escaping/metadata can expand this; the serialized 32 KiB check is authoritative.
RESULT_MAX_BYTES = 32768
ARTICLE_URL_MAX_BYTES = 8192
ARTICLE_ID_MAX_BYTES = 512
NOTICE = "Machine triage record. No verdict, including POSSIBLE_CENSORSHIP, is a finding of censorship; a person decides on independent evidence."
RecordId = Annotated[str, Field(pattern=r"^run_[0-9a-f]{32}\.r[0-9]+$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EvidenceURL = Annotated[str, Field(max_length=512)]
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)")


def _rfc3339(value: str) -> str:
    if not RFC3339.fullmatch(value):
        raise ValueError("timestamp_not_rfc3339")
    datetime.fromisoformat(value)
    return value


DateTime = Annotated[str, AfterValidator(_rfc3339), Field(json_schema_extra={"format": "date-time"})]


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


def _identity(value: str) -> str:
    if len(value.encode("utf-8")) > ARTICLE_ID_MAX_BYTES:
        raise ValueError("identity_too_large")
    return value


Identity = Annotated[str, Field(min_length=1, max_length=ARTICLE_ID_MAX_BYTES),
                     AfterValidator(_identity)]


class ArticleIdentity(FixedObject):
    ref: Identity | None
    archive_id: Identity | None
    url: str
    content_sha256: Sha256


class ResultRun(FixedObject):
    id: str
    origin: Literal["api", "webhook", "article", "benchmark"]
    category: Literal["live", "benchmark"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    created_at: DateTime
    finished_at: DateTime | None
    workflow_id: str


class ConceptTag(FixedObject):
    notation: str
    label: str


class Classification(FixedObject):
    subject_tags: list[ConceptTag] = Field(max_length=5)
    genre: ConceptTag | None
    geographic_scope: GeographicScope | None
    gist: str = Field(max_length=300)


class StageSummary(FixedObject):
    stage: StageName
    outcome: StageOutcome
    summary: str = Field(max_length=300)


class DeadCopy(FixedObject):
    url: EvidenceURL
    reason: str = Field(max_length=120)


class Evidence(FixedObject):
    relocated_to: EvidenceURL | None
    old_in_new: float | None
    new_in_old: float | None
    covers_key_facts: bool | None
    original_url_status: str | None
    live_copies: list[EvidenceURL] = Field(max_length=5)
    dead_copies: list[DeadCopy] = Field(max_length=5)
    related_same_publisher: list[EvidenceURL] = Field(max_length=5)
    search_queries: int | None
    search_queries_failed: int | None


class Error(FixedObject):
    code: Literal["audit_failed"]
    message: str


class AuditResult(FixedObject):
    model_config = ConfigDict(populate_by_name=True)
    schema_name: Literal["karakutu-ai-audit/audit-result"] = Field(alias="schema")
    schema_version: Literal["2.0"]
    record_id: RecordId | None
    lineage: Lineage | None
    article: ArticleIdentity
    run: ResultRun
    verdict: Verdict | None
    flags: list[AuditFlag]
    findings: str | None = Field(max_length=2000)
    classification: Classification | None
    stages: list[StageSummary] = Field(max_length=6)
    evidence: Evidence | None
    error: Error | None
    notice: Literal[NOTICE]


RecordErrorCode = Literal[
    "not_live", "not_finished", "no_output", "workflow_mismatch", "invalid_timestamp",
    "invalid_lineage", "credential_detected", "secret_detected", "too_large", "invalid_result",
]


class RecordError(ValueError):
    """Safe refusal code, never containing stored run data."""

    def __init__(self, code: RecordErrorCode):
        self.code = code
        super().__init__(code)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


REDACT_KEYS = {
    "authorization", "proxy_authorization", "cookie", "set_cookie", "api_key", "apikey", "x_api_key",
    "password", "passwd", "secret", "client_secret", "token", "access_token", "refresh_token",
    "id_token", "bearer", "jwk", "wallet", "wallet_json", "wallet_path", "private_key", "keyfile",
    "mnemonic", "traceback", "exception", "exc_info", "stack_trace",
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
        name = key.lower().replace("-", "_")
        if isinstance(value, dict) and name in REDACT_KEYS:
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


def _cap(value, limit):
    """Cap text at `limit` UTF-8 bytes, ending a cut text with an ellipsis."""
    if not isinstance(value, str):
        return ""
    data = value.encode("utf-8")
    if len(data) <= limit:
        return value
    return data[:limit - len("…".encode("utf-8"))].decode("utf-8", "ignore") + "…"


def _url(value):
    return value if isinstance(value, str) and value and len(value.encode("utf-8")) <= 512 else None


def _text(value):
    return value if isinstance(value, str) else ""


def _tag(value):
    if not isinstance(value, dict):
        return None
    return {"notation": _text(value.get("notation")), "label": _text(value.get("label"))}


def _classification(value):
    if not isinstance(value, dict):
        return None
    return {"subject_tags": [t for t in map(_tag, _items(value.get("subject_tags"))) if t],
            "genre": _tag(value.get("genre")), "geographic_scope": value.get("geographic_scope"),
            "gist": _text(value.get("gist"))}


def _items(value):
    return value if isinstance(value, list) else []


def _urls(values):
    return [url for value in _items(values) if (url := _url(value))]


def _share(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _has_result(run):
    return run.get("output") is not None or (run.get("error") or {}).get("code") == "audit_failed"


def _recordable(run):
    return (run["category"] == "live" and run["status"] in {"succeeded", "failed"}
            and bool(run.get("finished_at")) and _has_result(run)
            and (run.get("error") or {}).get("code") != "interrupted"
            # Seed fixtures have descriptive IDs and cannot be portable records.
            and re.fullmatch(r"run_[0-9a-f]{32}", run["id"]) is not None)


def build_result(run, *, lineage=None, revision=0, secret_values=()) -> dict:
    """Build an outgoing result or refuse with a fixed code, never stored diagnostics."""
    try:
        return _build_result(run, lineage=lineage, revision=revision, secret_values=secret_values)
    except RecordError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise RecordError("invalid_result") from None


def _build_result(run, *, lineage, revision, secret_values) -> dict:
    """Project saved output only; never inspect step evidence, prompts or transcripts."""
    article = run["input"]["removed_article"]
    ref = run.get("article_ref")
    # Linked article metadata also covers URL-owned references enriched by import.
    archive_id = run.get("archive_id")
    if archive_id is None:
        archive_id = run.get("article_archive_id")
    if archive_id is None and ref and ref.startswith("ka-"):
        archive_id = ref[3:] or None
    output = run.get("output") or {}
    stages = {s.get("stage"): s for s in _items(output.get("stages")) if isinstance(s, dict)}

    def details(stage):
        value = stages.get(stage, {}).get("details")
        return value if isinstance(value, dict) else {}

    web, sim, source = (details(s) for s in ("web_search", "similarity", "source"))
    relocation = web if web.get("relocated_to") else sim
    classification = _classification(output.get("classification"))
    eligible = _recordable(run)
    result = {
        "schema": "karakutu-ai-audit/audit-result", "schema_version": SCHEMA_VERSION,
        "record_id": f"{run['id']}.r{revision}" if eligible else None,
        "lineage": _lineage(run["id"], revision, lineage) if eligible else None,
        "article": {"ref": ref, "archive_id": archive_id,
                    "url": article["url"], "content_sha256": _sha256(article["content"])},
        "run": {key: run.get(key) for key in ("id", "origin", "category", "status",
                                             "created_at", "finished_at", "workflow_id")},
        "verdict": output.get("verdict"), "flags": output.get("flags") or [],
        "findings": output.get("findings"),
        "classification": classification,
        "stages": [{"stage": stage.value,
                    "outcome": stages.get(stage.value, {}).get("outcome", "inconclusive"),
                    "summary": " ".join(_text(stages.get(stage.value, {}).get("explanation")).split())}
                   for stage in StageName] if output else [],
        "evidence": {
            "relocated_to": _url(relocation.get("relocated_to")),
            "old_in_new": _share(relocation.get("old_in_new")),
            "new_in_old": _share(relocation.get("new_in_old")),
            "covers_key_facts": (web.get("covers_key_facts")
                                 if type(web.get("covers_key_facts")) is bool else None),
            "original_url_status": source.get("self_status"),
            "live_copies": _urls(web.get("matches")),
            "dead_copies": [{"url": d["url"], "reason": _text(d.get("reason"))}
                            for d in _items(web.get("dead_same_story"))
                            if isinstance(d, dict) and _url(d.get("url"))],
            "related_same_publisher": _urls(web.get("related_same_publisher")),
            "search_queries": _count(web.get("search_queries")),
            "search_queries_failed": _count(web.get("search_queries_failed")),
        } if output else None,
        "error": deepcopy(run.get("error")) if (run.get("error") or {}).get("code") == "audit_failed" else None,
        "notice": NOTICE,
    }
    _redact(result)
    if any(_has_credential(text) for text in _strings(result)):
        raise RecordError("credential_detected")
    secrets = tuple(s for s in secret_values if s)
    serialized = json.dumps(result, allow_nan=False, ensure_ascii=False)
    if _contains_secret(result, secrets) or any(secret in serialized for secret in secrets):
        raise RecordError("secret_detected")
    # Scrubbing may expand short credential values to the redaction marker.
    if result["evidence"]:
        evidence = result["evidence"]
        evidence["relocated_to"] = _url(evidence["relocated_to"])
        for key in ("live_copies", "related_same_publisher"):
            evidence[key] = _urls(evidence[key])[:5]
        evidence["dead_copies"] = [d for d in evidence["dead_copies"] if _url(d["url"])][:5]
        for dead in evidence["dead_copies"]:
            dead["reason"] = _cap(dead["reason"], 120)
    result["findings"] = _cap(result["findings"], 2000) if result["findings"] is not None else None
    for stage in result["stages"]:
        stage["summary"] = _cap(stage["summary"], 300)
    if result["classification"]:
        classification = result["classification"]
        classification["gist"] = _cap(classification["gist"], 300)
        classification["subject_tags"] = classification["subject_tags"][:5]
        for tag in [*classification["subject_tags"], classification["genre"]]:
            if tag:
                for key in ("notation", "label"):
                    tag[key] = _cap(tag[key], 120)
    if any(_has_credential(text) for text in _strings(result)):
        raise RecordError("credential_detected")
    serialized = json.dumps(result, allow_nan=False, ensure_ascii=False)
    if (len(result["article"]["url"].encode("utf-8")) > ARTICLE_URL_MAX_BYTES
            or any(isinstance(result["article"][key], str)
                   and len(result["article"][key].encode("utf-8")) > ARTICLE_ID_MAX_BYTES
                   for key in ("ref", "archive_id"))
            or len(serialized.encode("utf-8")) > RESULT_MAX_BYTES):
        raise RecordError("too_large")
    return AuditResult.model_validate(result).model_dump(mode="json", by_alias=True)


def build_record(run, workflow, *, article_ref=None, archive_id=None, revision=0, lineage=None,
                 secret_values=()) -> dict:
    if run["category"] != "live":
        raise RecordError("not_live")
    if (run["status"] not in {"succeeded", "failed"} or not run.get("finished_at")
            or (run.get("error") or {}).get("code") == "interrupted"):
        raise RecordError("not_finished")
    if not _has_result(run):
        raise RecordError("no_output")
    if workflow is None or workflow.get("id") != run["workflow_id"]:
        raise RecordError("workflow_mismatch")
    for key in ("created_at", "finished_at"):
        try:
            _rfc3339(run[key])
        except (ValueError, TypeError):
            raise RecordError("invalid_timestamp") from None
    lineage = _lineage(run["id"], revision, lineage)
    if not _recordable(run):
        raise RecordError("invalid_lineage")
    return build_result({**run,
                         "article_ref": article_ref if article_ref is not None else run.get("article_ref"),
                         "archive_id": archive_id if archive_id is not None else run.get("archive_id")},
                        lineage=lineage, revision=revision, secret_values=secret_values)


def record_json_schema() -> dict:
    schema = AuditResult.model_json_schema(by_alias=True)
    schema["properties"]["record_id"] = TypeAdapter(RecordId).json_schema()
    schema["properties"]["lineage"] = TypeAdapter(Lineage).json_schema(ref_template="#/$defs/{model}")
    schema["properties"]["lineage"].pop("$defs", None)
    run = schema["$defs"]["ResultRun"]["properties"]
    run["status"] = {"type": "string", "enum": ["succeeded", "failed"]}
    run["category"] = {"type": "string", "const": "live"}
    run["origin"] = {"type": "string", "enum": ["api", "webhook", "article"]}
    run["finished_at"] = TypeAdapter(DateTime).json_schema()
    return {**schema, "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:karakutu-ai-audit:audit-record:2",
            "title": "karakutu-ai-audit audit record v2"}
