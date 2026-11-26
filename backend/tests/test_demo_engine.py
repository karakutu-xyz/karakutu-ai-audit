"""Offline demo contracts, including the API queue, with fabricated inline inputs."""
import asyncio
import json
import socket
import time

from fastapi.testclient import TestClient
import pytest

from audit.api import create_app
from audit.config import Settings
from audit.demo_engine import DemoEngine
from audit.domain import AuditRequest, AuditResponse, StageName, Verdict
from audit.engine import NAMES
from audit.schemas import RunStep


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Offline demo attempted network access")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)


def request_for(number):
    return AuditRequest.model_validate({"removed_article": {
        "url": f"https://example.invalid/demo/{number}",
        "title": "Fabricated demo case",
        "content": "An invented report about a fictional public notice for offline testing.",
        "published_at": "2026-01-01T12:00:00Z",
    }})


@pytest.mark.parametrize(("number", "verdict"), [
    (2, Verdict.MOVED),
    (4, Verdict.FOUND_ONLINE),
    (5, Verdict.POSSIBLE_CENSORSHIP),
    (8, Verdict.INCONCLUSIVE),
])
async def test_demo_contract_and_deterministic_verdict(number, verdict):
    engine = DemoEngine(step_delay=0)
    config = dict(engine.public_config)
    observed = []

    async def on_step(step):
        # The engine must await each callback and persist steps in order.
        await asyncio.sleep(0)
        observed.append(step)

    await engine.start()
    result = await engine.run(request_for(number), on_step=on_step)
    repeated = await engine.run(request_for(number))
    await engine.close()

    assert engine.public_config == config == DemoEngine().public_config
    assert config["demo"] is True
    assert config["model_enabled"] is False
    assert config["model_name"] == "demo-model (offline)"
    assert config["search_provider"] == "disabled"
    assert config["archive_authenticated"] is False
    assert all(config[key] == "" for key in (
        "model_base_url", "searxng_url", "archive_base_url",
    ))
    assert set(result) == {"response", "steps", "model", "synthetic"}
    assert result["synthetic"] is True
    assert result["model"] == {"name": "demo-model (offline)", "enabled": False}
    json.dumps(result, allow_nan=False)
    response = AuditResponse.model_validate(result["response"])
    assert response.verdict == verdict == repeated["response"]["verdict"]
    assert "demo output from the offline test mode" in response.findings.lower()
    if verdict == Verdict.POSSIBLE_CENSORSHIP:
        assert "flag for human review" in response.findings
        assert "not a finding of censorship" in response.findings
    assert [stage.stage.value for stage in response.stages] == NAMES
    assert [step["name"] for step in observed] == NAMES
    assert observed == result["steps"]
    for index, step in enumerate(observed):
        RunStep.model_validate(step)
        assert set(step) == {
            "name", "status", "model", "input", "output", "model_calls", "evidence", "duration_ms",
        }
        assert step["model"] == ("demo-model (offline)" if step["model_calls"] else None)
        assert step["status"] in {"completed", "skipped"}
        assert step["output"] == response.stages[index].model_dump(mode="json")
        assert 'prior_stages' not in step['input']
        if step['status'] == 'skipped':
            assert 'skipped_because' in step['input']
        else:
            assert step['input']['article']['ref'] == 'run.input.removed_article'
            assert 'content' not in step['input']['article']
        assert step["duration_ms"] >= 0
    assert observed[0]["model_calls"][0]["demo"] is True
    assert observed[0]["model_calls"][0]["request"]["messages"]
    assert observed[0]["model_calls"][0]["parsed_output"]
    assert observed[1]["evidence"][0]["demo"] is True
    if verdict == Verdict.MOVED:
        assert response.short_circuited_at == StageName.SIMILARITY
        assert [step["status"] for step in observed] == ["completed"] * 2 + ["skipped"] * 4
        assert all(not step["model_calls"] and not step["evidence"] for step in observed[2:])
    else:
        assert response.short_circuited_at is None
        assert all(step["status"] == "completed" for step in observed)
        for step in observed[4:]:
            assert step["evidence"][0]["type"] == "search"
            assert ".example.invalid/" in step["evidence"][0]["results"][0]["url"]


async def test_accepts_captured_prompts_and_labels_per_step_models():
    engine = DemoEngine(step_delay=0)
    models = {"provider": "lmstudio", "response_format": "json_schema", "default": "demo-model (offline)",
              "steps": {"classification": "demo-model (offline)", "relevance": "fabricated-screen"}}
    result = await engine.run(request_for(0), prompts={"relevance": "Fabricated prompt"}, models=models)
    assert await engine.available_models() == ([], None)
    by_name = {step["name"]: step for step in result["steps"]}
    if by_name["relevance"]["model_calls"]:
        assert by_name["relevance"]["model"] == "fabricated-screen"
        assert by_name["relevance"]["model_calls"][0]["request"]["model"] == "fabricated-screen"
    assert by_name["similarity"]["model"] is None
    assert "Fabricated prompt" not in json.dumps(result)


async def test_a_few_fabricated_urls_produce_different_verdicts():
    engine = DemoEngine(step_delay=0)
    results = [await engine.run(request_for(number)) for number in (5, 2, 4, 8)]
    assert {result["response"]["verdict"] for result in results} == {
        "MOVED", "FOUND_ONLINE", "POSSIBLE_CENSORSHIP", "INCONCLUSIVE",
    }


async def test_unseeded_article_uses_its_normalized_ref_for_the_demo_scenario():
    engine = DemoEngine(step_delay=0)
    request = request_for(5)
    original = await engine.run(request, prompts={"relevance": "Fictional prompt"}, models={"steps": {}})
    equivalent = request.model_copy(deep=True)
    equivalent.removed_article.url = "HTTPS://EXAMPLE.INVALID/demo/5#fictional-fragment"
    equivalent.removed_article.title = "Another fictional title at the same article ref"
    equivalent.removed_article.content = "A revised fictional report."
    repeated = await engine.run(equivalent)
    assert original["response"] == repeated["response"]
    assert original["synthetic"] is True
    assert "simulated results, not real evidence" in original["response"]["findings"]


async def test_delay_per_step_and_cancellation_propagates(monkeypatch):
    engine = DemoEngine()
    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("audit.demo_engine.asyncio.sleep", record_sleep)
    await engine.run(request_for(2))
    assert delays == [0.4] * 6  # Skipped steps also yield for the running timeline.

    async def cancel_sleep(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr("audit.demo_engine.asyncio.sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await engine.run(request_for(2))


def test_demo_engine_completes_benchmark_through_api(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), DemoEngine(step_delay=0))
    with TestClient(app) as client:
        submitted = client.post("/api/benchmarks/runs", json={
            **request_for(5).model_dump(mode="json"), "label": "Fabricated offline demo",
        })
        assert submitted.status_code == 202
        run_id = submitted.json()["run"]["id"]
        for _ in range(100):
            fetched = client.get(f"/api/runs/{run_id}/details")
            assert fetched.status_code == 200
            run = fetched.json()
            if run["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Offline demo background run did not complete")
        assert run["status"] == "succeeded"
        assert run["synthetic"] is True
        assert run["output"]["verdict"] == "POSSIBLE_CENSORSHIP"
        AuditResponse.model_validate(run["output"])
        assert [step["name"] for step in run["steps"]] == NAMES


def test_test_mode_cancels_demo_during_step_delay_and_keeps_progress(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, mode="test"))
    with TestClient(app) as client:
        assert isinstance(app.state.audit_engine, DemoEngine)
        # Cancel during a subsequent step's real delay after progress is saved.
        app.state.audit_engine.step_delay = 0.1
        run_id = client.post("/api/benchmarks/runs", json={
            **request_for(5).model_dump(mode="json"),
        }).json()["run"]["id"]
        for _ in range(100):
            before = client.get(f"/api/runs/{run_id}/details").json()
            if before["steps"]:
                break
            time.sleep(0.005)
        else:
            pytest.fail("Demo did not emit its first step")
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 200
        assert response.json()["record_id"] is None
        stopped = client.get(f"/api/runs/{run_id}/details").json()
        assert stopped["status"] == "cancelled"
        assert stopped["verdict"] is None and stopped["output"] is None
        assert stopped["steps"][:len(before["steps"])] == before["steps"]
        assert 1 <= len(stopped["steps"]) < 6
        assert stopped["summary"]["verdict_label"] == "Stopped by a person"
        assert stopped["summary"]["failed_steps"] == []
        assert client.get(f"/api/runs/{run_id}/details").json() == stopped


async def test_demo_step_inputs_never_repeat_a_body_held_in_the_title():
    body = "Invented demo body sentence. " * 80
    request = request_for(1)
    request = request.model_copy(update={"removed_article": request.removed_article.model_copy(
        update={"title": body, "content": body})})
    engine = DemoEngine(step_delay=0)
    await engine.start()
    result = await engine.run(request)
    await engine.close()
    for step in result["steps"]:
        assert body not in json.dumps(step["input"])
        assert len(json.dumps(step["input"])) < 2048
