"""Mock Chat Completions transport; all article inputs and model IDs are invented."""
import json

import httpx
from openai import AsyncOpenAI, InternalServerError, AuthenticationError
from pydantic import BaseModel
import pytest

from audit.config import Settings, MODEL_STEPS
from audit.domain import Article, AuditRequest
from audit.engine import AuditEngine
from audit.model import Agent, JSON_SCHEMA_INSTRUCTION, trace
from audit.provenance import models_from_config
from audit.workflow import classification, source, web_search, related_removals
from audit.workflow.prompt_registry import DEFAULTS


class Answer(BaseModel):
    answer: bool


def completion(body, content):
    return httpx.Response(200, json={"id": "fabricated", "object": "chat.completion", "created": 0,
        "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": json.dumps(content)}}]})


def client_for(handler):
    return AsyncOpenAI(base_url="https://inference.invalid/v1", api_key="private-key-placeholder",
        max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("provider,thinking,extra", [
    ("openai_compatible", "none", {}),
    ("openai_compatible", "reasoning_effort", {"reasoning_effort": "none"}),
    ("lmstudio", "lmstudio", {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"})])
async def test_provider_wire_requests(provider, thinking, extra):
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return completion(body, {"answer": True})
    engine = AuditEngine(Settings(model_enabled=True, model_name="fabricated", model_provider=provider,
        thinking_control=thinking, response_format="json_schema").engine_config(), client=client_for(handler))
    await engine.start()
    try:
        result = await Agent(engine.model, result_type=Answer, system_prompt="rules").run("invented text")
        assert result.data.answer
        body = bodies[0]
        assert set(body) == {"model", "messages", "max_tokens", "temperature", "response_format"} | set(extra)
        assert {key: body[key] for key in extra} == extra
        assert "extra_body" not in body
        assert body["response_format"]["type"] == "json_schema"
        assert body["messages"][0]["content"] == "rules"
    finally:
        await engine.close()


@pytest.mark.parametrize("rejected_status", [400, 422])
async def test_auto_fallback_retains_both_attempts_and_caches_by_model(rejected_status):
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(rejected_status, json={"error": {"message": "PRIVATE upstream echo"}})
        return completion(body, {"answer": True})
    engine = AuditEngine(Settings(model_enabled=True, model_name="fabricated", model_provider="openai_compatible").engine_config(), client=client_for(handler))
    await engine.start()
    events = []
    token = trace.set(events)
    try:
        for name in ["fabricated", "fabricated", "other-fabricated"]:
            # Distinct per-run descriptors share the process cache.
            result = await Agent(engine.model_descriptor(name), result_type=Answer, system_prompt="rules").run("text")
            assert result.data.answer is True
        assert [e["response_format"] for e in events] == ["json_schema", "json_object", "json_object", "json_schema", "json_object"]
        expected_error = "BadRequestError" if rejected_status == 400 else "UnprocessableEntityError"
        assert events[0]["error_type"] == events[3]["error_type"] == expected_error
        assert events[1]["parsed_output"] == {"answer": True}
        assert "PRIVATE" not in str(events)
        assert bodies[1]["response_format"] == {"type": "json_object"}
        assert bodies[1]["messages"][0]["content"] == "rules" + JSON_SCHEMA_INSTRUCTION + json.dumps(Answer.model_json_schema(), sort_keys=True)
    finally:
        trace.reset(token)
        await engine.close()


@pytest.mark.parametrize("status,error", [(500, InternalServerError), (401, AuthenticationError)])
async def test_other_transport_errors_never_fallback(status, error):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "private"}})
    engine = AuditEngine(Settings(model_enabled=True, model_name="fabricated", model_provider="openai_compatible").engine_config(), client=client_for(handler))
    await engine.start()
    try:
        with pytest.raises(error):
            await Agent(engine.model, result_type=Answer, system_prompt="rules").run("text")
        assert len(requests) == 1
        assert not engine.fallback_models
    finally:
        await engine.close()


@pytest.mark.parametrize("response_format", ["json_object", "auto"])
async def test_invalid_json_object_relevance_is_a_failed_audit(response_format):
    def handler(request):
        body = json.loads(request.content)
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "unsupported"}})
        return completion(body, {"is_relevant": "invalid"})
    engine = AuditEngine(Settings(model_enabled=True, model_name="fabricated", model_provider="openai_compatible",
        response_format=response_format).engine_config(), client=client_for(handler))
    await engine.start()
    try:
        result = await engine.run(AuditRequest(removed_article=Article(url="https://fabricated.invalid/story", title="Invented", content="Fictional text")))
        assert result["response"]["verdict"] == "AUDIT_FAILED"
        steps = result["steps"]
        assert steps[2]["status"] == "failed" and steps[2]["model"] == "fabricated"
        assert steps[2]["model_calls"][-1]["error_type"] == "ValidationError"
        assert steps[1]["model"] is None
        assert all(step["model"] is None for step in steps[3:])
    finally:
        await engine.close()


@pytest.mark.parametrize("captured", [True, False])
async def test_step_model_mapping_is_used_for_requests_and_records(monkeypatch, captured):
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return completion(body, {"is_relevant": True, "confidence": .8, "reasoning": "Invented scope"}
            if body["model"].endswith("relevance") else {"answer": True})

    async def exercise(agent):
        await Agent(agent.model, result_type=Answer, system_prompt="invented rules").run("invented text")
    async def cls(article, agents, catalog):
        await exercise(agents.coarse)
        await exercise(agents.fine)
        return classification.ClassificationOutcome(explanation="Invented metadata")
    async def related(article, agent, **kwargs):
        await exercise(agent)
        return related_removals.RelatedRemovalsEvidence(reason="Invented absence")
    async def src(article, agent, search):
        await exercise(agent)
        return source.SourceEvidence(reasoning="Invented source")
    async def web(article, search, agent, **kwargs):
        await exercise(agent)
        return web_search.WebSearchEvidence(reasoning="Invented search")
    monkeypatch.setattr(classification, "run", cls)
    monkeypatch.setattr(related_removals, "run", related)
    monkeypatch.setattr(source, "run", src)
    monkeypatch.setattr(web_search, "run", web)
    config = Settings(model_enabled=True, model_name="fabricated-default", model_provider="openai_compatible",
                      **{"model_name_" + step: "env-" + step for step in MODEL_STEPS}).engine_config()
    engine = AuditEngine(config, client=client_for(handler))
    await engine.start()
    models = models_from_config(engine.public_config)
    models["steps"] = {step: ("saved-" if captured else "env-") + step for step in MODEL_STEPS}
    try:
        result = await engine.run(AuditRequest(removed_article=Article(url="https://fabricated.invalid/story", title="Invented", content="Fictional text")), models=models if captured else None,
            prompts=DEFAULTS)
        assert result["response"]["verdict"] == "INCONCLUSIVE"
        assert [body["model"] for body in bodies] == [models["steps"][step] for step in ["classification", "classification", "relevance", "related_removals", "source", "web_search"]]
        assert {step["name"]: step["model"] for step in result["steps"]} == {**models["steps"], "similarity": None}
    finally:
        await engine.close()


async def test_model_discovery_filters_embeddings_and_resolves_only_default():
    def handler(request):
        return httpx.Response(200, json={"object": "list", "data": [{"id": name, "object": "model", "created": 0, "owned_by": "fabricated"}
            for name in ["embed-fabricated", "rerank-fabricated", "fabricated-chat"]]})
    engine = AuditEngine(Settings(model_enabled=True, model_provider="openai_compatible", model_name_source="auto").engine_config(), client=client_for(handler))
    await engine.start()
    try:
        assert engine.public_config['model_name'] == 'fabricated-chat'
        assert engine.agents.source.model.name == 'auto'
        assert engine.agents.relevance.model.name == 'fabricated-chat'
        assert await engine.available_models() == (['fabricated-chat'], None)
    finally:
        await engine.close()


async def test_model_discovery_errors_are_safe_and_disabled_mode_does_not_call_provider():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": "PRIVATE upstream error"}})
    engine = AuditEngine(Settings(model_enabled=True, model_name="fabricated").engine_config(), client=client_for(handler))
    await engine.start()
    try:
        assert await engine.available_models() == ([], 'models_unavailable')
        assert len(requests) == 1
    finally:
        await engine.close()
    engine = AuditEngine(Settings().engine_config(), client=client_for(handler))
    await engine.start()
    try:
        assert await engine.available_models() == ([], None)
        assert len(requests) == 1
    finally:
        await engine.close()
