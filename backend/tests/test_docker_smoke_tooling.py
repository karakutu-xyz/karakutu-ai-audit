"""Offline checks for the Docker deployment files and the smoke suite's fake model."""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_fake_model():
    spec = importlib.util.spec_from_file_location("fake_model", ROOT / "scripts/smoke/fake_model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_published_ports_are_overridable_and_default_to_loopback():
    compose = (ROOT / "compose.yaml").read_text()
    search = (ROOT / "compose.search.yaml").read_text()
    assert '"${KARAKUTU_BIND_ADDRESS:-127.0.0.1}:${KARAKUTU_API_PORT:-2200}:2200"' in compose
    assert '"${KARAKUTU_BIND_ADDRESS:-127.0.0.1}:${KARAKUTU_WEB_PORT:-2201}:2201"' in compose
    assert '"127.0.0.1:${KARAKUTU_SEARCH_PORT:-2202}:8080"' in search


def test_api_container_sees_the_published_bind_address():
    # Without it the API cannot refuse a public bind that lacks API_AUTH_TOKEN.
    compose = (ROOT / "compose.yaml").read_text()
    assert re.search(r"^\s+KARAKUTU_BIND_ADDRESS: \$\{KARAKUTU_BIND_ADDRESS:-127\.0\.0\.1\}$", compose, re.M)


def test_make_targets_take_their_own_project_names():
    makefile = (ROOT / "Makefile").read_text()
    assert "PROJECT ?= karakutu-ai-audit" in makefile
    assert "TEST_PROJECT ?= $(PROJECT)-test" in makefile
    assert "-p karakutu-ai-audit " not in makefile


def test_fake_model_answers_json_object_requests_from_the_prompt_schema():
    from audit.model import JSON_SCHEMA_INSTRUCTION
    from audit.workflow.relevance import RelevanceResult
    from audit.workflow.web_search import WebSearchResult

    fake = load_fake_model()
    assert fake.SCHEMA_MARKER in JSON_SCHEMA_INSTRUCTION
    for result_type in (RelevanceResult, WebSearchResult):
        system = "Fictional instructions." + JSON_SCHEMA_INSTRUCTION + json.dumps(
            result_type.model_json_schema(), sort_keys=True)
        reply = fake.answer([{"role": "system", "content": system}, {"role": "user", "content": "x"}])
        result_type.model_validate_json(reply)


def test_fake_model_fills_unknown_schemas_with_valid_minimal_values():
    from pydantic import BaseModel

    class Invented(BaseModel):
        name: str
        score: float
        flags: list[str]
        nested: dict[str, int] | None = None

    fake = load_fake_model()
    system = "x" + fake.SCHEMA_MARKER + json.dumps(Invented.model_json_schema())
    Invented.model_validate_json(fake.answer([{"role": "system", "content": system}]))


def test_every_long_running_service_has_a_healthcheck():
    import yaml

    for name in ("compose.yaml", "compose.search.yaml"):
        services = yaml.safe_load((ROOT / name).read_text())["services"]
        for service, spec in services.items():
            assert "healthcheck" in spec, f"{name}:{service} has no healthcheck"


def test_backup_never_archives_the_reserved_restore_directory():
    # volume_restore.py refuses archives holding it, so a leftover copy must not poison backups.
    spec = importlib.util.spec_from_file_location("volume_restore", ROOT / "scripts/volume_restore.py")
    volume_restore = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(volume_restore)
    makefile = (ROOT / "Makefile").read_text()
    assert f"--exclude=./{volume_restore.ASIDE} -C /data ." in makefile
