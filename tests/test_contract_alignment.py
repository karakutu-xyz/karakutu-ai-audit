import json
import re
from pathlib import Path

import yaml

from src.api.app import app


def _load_contract() -> dict:
    path = Path(__file__).parent.parent / "karakutu-audit-api-contract.yml"
    return yaml.safe_load(path.read_text())


def test_paths_match():
    contract = _load_contract()
    live = app.openapi()
    assert set(contract["paths"].keys()) == set(live["paths"].keys())


def test_audit_request_required_fields():
    contract = _load_contract()
    live = app.openapi()
    assert set(contract["components"]["schemas"]["AuditRequest"]["required"]) == \
           set(live["components"]["schemas"]["AuditRequest"]["required"])


def test_similar_articles_optional_max_5():
    schema = _load_contract()["components"]["schemas"]["AuditRequest"]
    assert "similar_articles" not in schema["required"]
    s = schema["properties"]["similar_articles"]
    assert s["maxItems"] == 5
    assert s.get("nullable") is True
    assert "minItems" not in s


def test_verdict_enum_membership():
    contract = _load_contract()
    live = app.openapi()
    contract_values = set(contract["components"]["schemas"]["Verdict"]["enum"])
    live_values = set(live["components"]["schemas"]["Verdict"]["enum"])
    assert contract_values == live_values


def test_stage_name_enum_membership():
    contract = _load_contract()
    live = app.openapi()
    contract_values = set(contract["components"]["schemas"]["StageName"]["enum"])
    live_values = set(live["components"]["schemas"]["StageName"]["enum"])
    assert contract_values == live_values


def test_moved_verdict_is_in_the_contract():
    # MOVED (measured republication) is a distinct tier from LIKELY_MOVED (partial or
    # unmeasured fit); the contract must carry both.
    enum = _load_contract()["components"]["schemas"]["Verdict"]["enum"]
    assert "MOVED" in enum
    assert "LIKELY_MOVED" in enum


def test_not_removed_verdict_is_in_the_contract():
    # NOT_REMOVED: the stage-3 probe found the "removed" article live at its original URL,
    # so the audit premise did not hold at run time. A descriptive verdict, not a finding.
    enum = _load_contract()["components"]["schemas"]["Verdict"]["enum"]
    assert "NOT_REMOVED" in enum


def test_stage_names_cover_the_six_steps():
    enum = _load_contract()["components"]["schemas"]["StageName"]["enum"]
    assert enum == ["classification", "similarity", "relevance", "related_removals", "source", "web_search"]


def test_response_has_six_stages_and_the_new_fields():
    schemas = _load_contract()["components"]["schemas"]
    stages = schemas["AuditResponse"]["properties"]["stages"]
    assert stages["minItems"] == 6 and stages["maxItems"] == 6
    assert schemas["AuditResponse"]["properties"]["short_circuited_at"]["enum"] == ["similarity", "relevance"]
    assert "classification" in schemas["AuditResponse"]["properties"]
    assert "flags" in schemas["AuditResponse"]["properties"]
    assert "details" in schemas["StageResult"]["properties"]
    assert set(schemas["Classification"]["properties"]) == {"subject_tags", "genre", "geographic_scope", "gist"}
    assert schemas["Classification"]["properties"]["subject_tags"]["maxItems"] == 5
    live = app.openapi()["components"]["schemas"]
    assert set(schemas["GeographicScope"]["enum"]) == set(live["GeographicScope"]["enum"]) == {"local", "regional", "national", "global_scope"}
    assert set(schemas["AuditFlag"]["enum"]) == set(live["AuditFlag"]["enum"]) == {"SOURCE_DEAD", "RELATED_REMOVALS"}


# --- article jobs (/api/v1) and /ready -------------------------------------------------------

ARTICLE_ROUTES = (("/api/v1/articles/{article_id}/audit", "post"), ("/api/v1/jobs/{job_id}", "get"),
                  ("/api/v1/articles/{article_id}/jobs/latest", "get"))


def _schemas():
    return _load_contract()["components"]["schemas"], app.openapi()["components"]["schemas"]


def _prop_names(schema):
    return set(schema.get("properties", {}))


def test_paths_and_methods_match():
    contract, live = _load_contract()["paths"], app.openapi()["paths"]
    for p in contract:
        assert set(contract[p]) == set(live[p]), p


def test_job_schemas_match_the_models():
    contract, live = _schemas()
    for name in ("JobAccepted", "Job", "JobError", "TagsDecision", "CommentDecision", "ReadinessResponse",
                 "ErrorResponse"):
        assert name in contract and name in live, name
        assert _prop_names(contract[name]) == _prop_names(live[name]), name
        assert set(contract[name].get("required", [])) == set(live[name].get("required", [])), name


def test_job_enums_match_the_models():
    contract, live = _schemas()
    for name in ("JobStatus", "JobPhase", "JobOutcome", "TagsState"):
        assert set(contract[name]["enum"]) == set(live[name]["enum"]), name


def test_job_enums_match_the_worker_vocabulary():
    from src.jobs import models as M
    contract, _ = _schemas()
    assert set(contract["JobOutcome"]["enum"]) == set(M.OUTCOMES)
    assert set(contract["JobStatus"]["enum"]) == set(M.STATUSES)
    assert set(contract["JobPhase"]["enum"]) == set(M.PHASES)
    assert set(contract["JobError"]["properties"]["code"]["enum"]) == set(M.ERROR_MESSAGES)


def test_every_schema_in_the_contract_is_used():
    """Lean: nothing in the contract that no path reaches."""
    doc = _load_contract()
    text = json.dumps(doc["paths"]) + json.dumps(doc["components"].get("responses", {}))
    seen, todo = set(), [k.split("/")[-1] for k in _refs(text)]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo += [k.split("/")[-1] for k in _refs(json.dumps(doc["components"]["schemas"].get(name, {})))]
    assert set(doc["components"]["schemas"]) - seen == set()


def _refs(text):
    return re.findall(r'"#/components/(?:schemas|parameters|responses)/([A-Za-z]+)"', text)


def test_article_routes_take_the_bearer_token_and_the_contract_has_one_scheme():
    contract = _load_contract()
    assert set(contract["components"]["securitySchemes"]) == {"BearerAuth"}
    for path, method in ARTICLE_ROUTES:
        op = contract["paths"][path][method]
        assert op["security"] == [{"BearerAuth": []}], path
        assert {"200", "401", "404"} <= set(op["responses"]), path
        assert "requestBody" not in op, path
    post = contract["paths"]["/api/v1/articles/{article_id}/audit"]["post"]
    assert {"200", "202", "401", "404", "422", "429"} == set(post["responses"])
    assert {p.get("name") or p.get("$ref", "").split("/")[-1] for p in post["parameters"]} == {"ArticleId", "dry_run"}
    live = app.openapi()["paths"]
    assert "requestBody" not in live["/api/v1/articles/{article_id}/audit"]["post"], "no body is read"
    assert {p["name"] for p in live["/api/v1/articles/{article_id}/audit"]["post"]["parameters"]} == \
        {"article_id", "dry_run"}


def test_ready_documents_503():
    contract = _load_contract()
    assert {"200", "503"} <= set(contract["paths"]["/ready"]["get"]["responses"])


def test_ready_only_requires_status_so_the_anonymous_view_is_valid():
    contract, live = _schemas()
    for schema in (contract["ReadinessResponse"], live["ReadinessResponse"]):
        assert set(schema.get("required", [])) == {"status"}
    assert {"checks", "writes_enabled", "write_blockers"} <= _prop_names(contract["ReadinessResponse"])
    assert {"BearerAuth": []} in _load_contract()["paths"]["/ready"]["get"]["security"]
