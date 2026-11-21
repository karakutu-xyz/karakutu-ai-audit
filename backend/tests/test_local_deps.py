"""The offline dependency check run by `make dev`, on fabricated lockfiles only."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_local_deps.py"
spec = importlib.util.spec_from_file_location("check_local_deps", SCRIPT)
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)

LOCK = {
    "": {"name": "fictional-web", "dependencies": {"fictional-dep": "^1.0.0"}},
    "node_modules/fictional-dep": {"version": "1.0.0", "integrity": "sha512-fictional"},
    "node_modules/fictional-native-other-os": {"version": "2.0.0", "optional": True},
    "node_modules/fictional-dev-native": {"version": "2.0.0", "devOptional": True},
    "../fictional-workspace": {"link": True},
}
INSTALLED = {key: value for key, value in LOCK.items()
             if key in ("node_modules/fictional-dep",)}


def write(web: Path, lock=LOCK, installed=INSTALLED):
    (web / "node_modules").mkdir(parents=True, exist_ok=True)
    (web / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": lock}))
    if installed is not None:
        (web / "node_modules/.package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": installed}))


def test_current_install_passes_with_absent_optional_packages(tmp_path):
    write(tmp_path)
    assert deps.web_current(tmp_path)


@pytest.mark.parametrize("installed", [
    None,  # never installed, or installed before npm kept a record
    {},  # a lockfile package is missing
    {"node_modules/fictional-dep": {"version": "0.9.0", "integrity": "sha512-fictional"}},
    {"node_modules/fictional-dep": {"version": "1.0.0", "integrity": "sha512-other"}},
    {"node_modules/fictional-dep": "not an entry"},
])
def test_missing_or_older_install_is_stale(tmp_path, installed):
    write(tmp_path, installed=installed)
    assert not deps.web_current(tmp_path)


def test_unreadable_lockfiles_are_stale(tmp_path):
    write(tmp_path)
    (tmp_path / "node_modules/.package-lock.json").write_text("{not json")
    assert not deps.web_current(tmp_path)
    assert not deps.web_current(tmp_path / "absent")


def test_missing_uv_names_the_guide(tmp_path, monkeypatch):
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    assert deps.python_problem(tmp_path) == deps.NO_UV_HINT


@pytest.mark.parametrize("code, expected", [(0, None), (1, "uv sync --frozen")])
def test_python_check_is_offline_and_reports_the_command(tmp_path, monkeypatch, code, expected):
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        return type("Done", (), {"returncode": code})()

    monkeypatch.setattr(deps.shutil, "which", lambda name: "/fictional/uv")
    monkeypatch.setattr(deps.subprocess, "run", run)
    hint = deps.python_problem(tmp_path)
    assert seen == [["/fictional/uv", "sync", "--frozen", "--check", "--offline", "--quiet"]]
    assert (hint is None) if expected is None else (expected in hint)
