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


def test_postgres_is_optional_and_loopback_only():
    import yaml

    text = (ROOT / "compose.postgres.yaml").read_text()
    service = yaml.safe_load(text)["services"]["db"]
    assert service["image"] == "postgres:17-alpine"
    assert service["ports"] == ["127.0.0.1:5432:5432"]
    assert "POSTGRES_PASSWORD:?" in service["environment"]["POSTGRES_PASSWORD"]
    assert "healthcheck" in service and service["volumes"]
    makefile = (ROOT / "Makefile").read_text()
    assert "LIVE_FILES ?= -f compose.yaml" in makefile
    assert "$(MAKE) test-postgres" in makefile
    assert "backup: storage-sqlite" in makefile
    assert "restore: storage-sqlite" in makefile


def test_postgres_runner_cleans_up_without_printing_passwords(tmp_path):
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    events = tmp_path / "events"
    fake = '''#!/usr/bin/env python3
import os
from pathlib import Path
import re
import sys
name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "docker":
    with open(os.environ["POSTGRES_TEST_EVENTS"], "a") as output:
        output.write(" ".join(args) + "\\n")
    if args[0] == "run":
        assert re.fullmatch("[a-f0-9]{32}", os.environ["POSTGRES_PASSWORD"])
        if os.environ["POSTGRES_TEST_FAIL"] == "run":
            sys.exit(1)
        print("invented-container-id")
else:
    assert args[:3] == ["run", "pytest", "-q"]
    from urllib.parse import urlsplit
    url = urlsplit(os.environ["TEST_POSTGRES_URL"])
    assert re.fullmatch("[a-f0-9]{32}", url.password)
    assert url.hostname == "127.0.0.1" and url.port == 25432
    sys.exit(1 if os.environ["POSTGRES_TEST_FAIL"] == "pytest" else 0)
'''
    for name in ("docker", "uv"):
        path = bin_dir / name
        path.write_text(fake)
        path.chmod(0o700)
    # Avoid even reserving a port; the fake Docker command never opens a socket.
    python = bin_dir / "python3"
    python.write_text("#!/bin/sh\nif [ \"$1\" = - ]; then cat >/dev/null; "
                      "echo 25432; else exec \"$POSTGRES_TEST_PYTHON\" \"$@\"; fi\n")
    python.chmod(0o700)
    import sys
    for failure in ("none", "pytest", "run"):
        events.write_text("")
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
               "POSTGRES_TEST_EVENTS": str(events), "POSTGRES_TEST_FAIL": failure,
               "POSTGRES_TEST_PYTHON": sys.executable}
        result = subprocess.run(["bash", str(ROOT / "scripts/test_postgres.sh")],
                                env=env, capture_output=True, text=True)
        assert result.returncode == (0 if failure == "none" else 1)
        commands = events.read_text().splitlines()
        assert commands[0].startswith("run --rm -d")
        assert "-e POSTGRES_PASSWORD " in commands[0]
        assert "127.0.0.1:25432:5432" in commands[0]
        if failure == "run":
            assert len(commands) == 1
        else:
            assert commands[-1] == "rm -f invented-container-id"
        assert result.stdout == result.stderr == ""


def test_volume_guard_refuses_postgres_before_any_database_access():
    import os
    import subprocess
    import sys

    makefile = (ROOT / "Makefile").read_text()
    guard = re.search(r"^\t  '(import os,sys;[^\n]+)'$", makefile, re.M).group(1)
    url = "postgresql+psycopg://audit:fictional-private-password@db.example.invalid/audit"
    env = {**os.environ, "DATABASE_URL": url,
           "PYTHONPATH": str(ROOT / "backend")}
    refused = subprocess.run([sys.executable, "-c", guard], env=env,
                             capture_output=True, text=True)
    assert refused.returncode == 1
    assert refused.stderr.strip() == (
        "backup/restore supports SQLite only; use PostgreSQL provider backups")
    assert "fictional-private-password" not in refused.stdout + refused.stderr
    env["DATABASE_URL"] = ""
    allowed = subprocess.run([sys.executable, "-c", guard], env=env,
                             capture_output=True, text=True)
    assert allowed.returncode == 0 and not allowed.stdout + allowed.stderr
