"""The offline dependency check run by `make dev`, on fabricated lockfiles only."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_local_deps.py"
spec = importlib.util.spec_from_file_location("check_local_deps", SCRIPT)
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)

LOCK = {
    "": {"name": "fictional-web", "dependencies": {"fictional-dep": "^1.0.0"}},
    "node_modules/fictional-dep": {"version": "1.0.0", "integrity": "sha512-fictional",
                                   "resolved": "https://registry.invalid/fictional-dep.tgz"},
    "node_modules/fictional-dep/node_modules/fictional-nested": {"version": "3.0.0"},
    "node_modules/@fictional/scoped": {"version": "4.0.0"},
    "node_modules/fictional-native-other-os": {"version": "2.0.0", "optional": True},
    "node_modules/fictional-dev-native": {"version": "2.0.0", "devOptional": True},
    "node_modules/fictional-linked": {"resolved": "packages/linked", "link": True},
    "packages/linked": {"version": "0.0.0"},
}
INSTALLED = {
    "node_modules/fictional-dep": "1.0.0",
    "node_modules/fictional-dep/node_modules/fictional-nested": "3.0.0",
    "node_modules/@fictional/scoped": "4.0.0",
}


def write(web: Path, installed=INSTALLED, recorded=None):
    """Lay out an install; `recorded` replaces npm's install record (default: matching)."""
    web.mkdir(parents=True, exist_ok=True)
    (web / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": LOCK}))
    for path, version in installed.items():
        (web / path).mkdir(parents=True, exist_ok=True)
        (web / path / "package.json").write_text(json.dumps({"version": version}))
    if recorded is None:
        recorded = {path: LOCK[path] for path in installed}
    (web / "node_modules").mkdir(parents=True, exist_ok=True)
    (web / "node_modules/.package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": recorded}))


def test_current_install_passes_with_absent_optional_packages(tmp_path):
    write(tmp_path)
    assert deps.web_current(tmp_path)


def test_a_present_optional_package_must_match(tmp_path):
    write(tmp_path, {**INSTALLED, "node_modules/fictional-native-other-os": "1.9.0"})
    assert not deps.web_current(tmp_path)


@pytest.mark.parametrize("missing", sorted(INSTALLED))
def test_a_missing_package_folder_is_stale(tmp_path, missing):
    # For example an interrupted install, or a folder deleted after npm recorded it.
    write(tmp_path, {k: v for k, v in INSTALLED.items() if k != missing})
    assert not deps.web_current(tmp_path)


@pytest.mark.parametrize("field, value", [
    ("version", "0.9.0"), ("integrity", "sha512-older"),
    ("resolved", "https://registry.invalid/older.tgz"),
])
def test_a_lockfile_change_at_the_same_installed_version_is_stale(tmp_path, field, value):
    # For example a pull that changes a package's source or integrity, not its version.
    recorded = {path: dict(LOCK[path]) for path in INSTALLED}
    recorded["node_modules/fictional-dep"][field] = value
    write(tmp_path, recorded=recorded)
    assert not deps.web_current(tmp_path)


def test_a_folder_without_an_install_record_is_stale(tmp_path):
    write(tmp_path, recorded={path: LOCK[path] for path in INSTALLED
                              if path != "node_modules/@fictional/scoped"})
    assert not deps.web_current(tmp_path)
    (tmp_path / "node_modules/.package-lock.json").unlink()
    assert not deps.web_current(tmp_path)


def test_a_present_but_unreadable_optional_package_is_stale(tmp_path):
    write(tmp_path)
    (tmp_path / "node_modules/fictional-native-other-os").mkdir()
    assert not deps.web_current(tmp_path)


def test_an_older_installed_version_is_stale(tmp_path):
    write(tmp_path, {**INSTALLED, "node_modules/@fictional/scoped": "3.9.0"})
    assert not deps.web_current(tmp_path)


def test_unreadable_files_are_stale(tmp_path):
    write(tmp_path)
    (tmp_path / "node_modules/fictional-dep/package.json").write_text("{not json")
    assert not deps.web_current(tmp_path)
    assert not deps.web_current(tmp_path / "absent")


def test_missing_uv_names_the_guide(tmp_path, monkeypatch):
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    assert deps.python_problem(tmp_path) == deps.NO_UV_HINT


@pytest.mark.parametrize("code, expected", [(0, None), (1, deps.PYTHON_HINT),
                                            ("timeout", deps.UV_TIMEOUT_HINT)])
def test_python_check_is_offline_bounded_and_names_the_command(tmp_path, monkeypatch,
                                                                code, expected):
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs["timeout"]))
        if code == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return type("Done", (), {"returncode": code})()

    monkeypatch.setattr(deps.shutil, "which", lambda name: "/fictional/uv")
    monkeypatch.setattr(deps.subprocess, "run", run)
    assert deps.python_problem(tmp_path) == expected
    assert seen == [(["/fictional/uv", "sync", "--frozen", "--check", "--offline", "--quiet"],
                     deps.UV_TIMEOUT_SECONDS)]
