"""Static inventory and isolated setup/local-run checks; never use the checkout's .env."""
import ast
from collections import defaultdict
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
# These are process/Make internals, not application settings.
INTERNAL = {
    "CURDIR", "MAKE", "COMPOSE", "WEB_PORT", "LIVE", "LIVE_START", "TEST", "STAMP",
    "TMPDIR",  # standard OS temporary directory, not a project setting
}
# Substrings alone would mistake a numeric budget and a CLI boolean for credentials.
NON_SECRETS = {"MODEL_MAX_TOKENS": "output budget", "API_TOKEN": "Make boolean flag"}
# Keep explicit and reasoned if a future tool needs a key it cannot statically expose.
EXAMPLE_ONLY = {
    "TEST_POSTGRES_URL": "Read only by the opt-in database fixture in backend/tests/conftest.py",
}


def inventory() -> dict[str, set[str]]:
    found = defaultdict(set)

    def add(name, path):
        if name not in INTERNAL:
            found[name].add(str(path.relative_to(ROOT)))

    for path in sorted((ROOT / "backend/audit").rglob("*.py")) + sorted(SCRIPTS.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                if function in {"os.getenv", "os.environ.get", "environ.get"}:
                    argument = node.args[0]
                    if isinstance(argument, ast.Constant):
                        add(argument.value, path)
                    elif isinstance(argument, ast.BinOp):
                        # Documented config family, expanded from source without importing it.
                        assert ast.unparse(argument) == "'MODEL_NAME_' + step.upper()"
                        steps = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                                     and any(isinstance(t, ast.Name) and t.id == "MODEL_STEPS"
                                             for t in n.targets))
                        for step in ast.literal_eval(steps):
                            add("MODEL_NAME_" + step.upper(), path)
                    else:
                        assert path.name == "config.py" and ast.unparse(argument) == "name"
                if (path.name == "config.py" and function in {"_bool", "_port", "_entries"}
                        and node.args and isinstance(node.args[0], ast.Constant)):
                    add(node.args[0].value, path)
            elif (isinstance(node, ast.Subscript)
                  and ast.unparse(node.value) in {"os.environ", "environ"}):
                assert isinstance(node.slice, ast.Constant), "Document a new dynamic env family"
                add(node.slice.value, path)
    for path in sorted(ROOT.glob("compose*.yaml")) + sorted(SCRIPTS.rglob("compose*.yaml")):
        for name in re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?=[:}])", path.read_text()):
            add(name, path)
    path = ROOT / "Makefile"
    text = path.read_text()
    names = re.findall(r"^([A-Z][A-Z0-9_]*)\s*\?=", text, re.M)
    names += re.findall(r"\$\(([A-Z][A-Z0-9_]*)\)", text)
    for name in names:
        add(name, path)
    for path in sorted(SCRIPTS.rglob("*.sh")) + sorted(ROOT.glob("Tiltfile*")):
        text = path.read_text()
        # Shell defaults read caller input; assigned shell locals are not settings.
        names = re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?::[-?]|[-?])", text)
        names += re.findall(r'''(?:os\.)?(?:getenv|environ\.get)\(['"]([A-Z][A-Z0-9_]*)''', text)
        for name in names:
            add(name, path)
    for path in (ROOT / "web").rglob("*"):
        if (path.suffix not in {".ts", ".tsx", ".js", ".mjs"}
                or {"node_modules", "dist"} & set(path.parts)):
            continue
        for name in re.findall(r"process\.env\.([A-Z][A-Z0-9_]*)", path.read_text()):
            add(name, path)
    return dict(found)


def example_blocks():
    blocks = {}
    comments = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.startswith("#"):
            comments.append(line)
        elif line:
            key, separator, value = line.partition("=")
            assert separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            assert key not in blocks, f"Duplicate {key}"
            blocks[key] = (value, "\n".join(comments))
            comments = []
        else:
            comments = []
    return blocks


def test_example_covers_exact_static_inventory():
    keys = set(example_blocks())
    reads = set(inventory())
    assert reads <= keys, f"Missing variables: {sorted(reads - keys)}"
    assert keys <= reads | EXAMPLE_ONLY.keys(), f"Unread variables: {sorted(keys - reads)}"
    assert all(EXAMPLE_ONLY.values())
    documentation = (ROOT / "docs/configuration.md").read_text()
    for key in keys:
        assert f"| `{key}` |" in documentation, key


def test_every_variable_is_explained_and_secrets_are_empty():
    for key, (value, comment) in example_blocks().items():
        assert "Default when unset:" in comment, key
        assert "Required:" in comment, key
        if re.search("TOKEN|KEY|SECRET|PASSWORD|WALLET", key) and key not in NON_SECRETS:
            assert "# SECRET" in comment, key
            assert value == "" or re.fullmatch(r"['\"]?<[^>]+>['\"]?", value), key


def parse_settings(path):
    # Caller-set values win over the file, so strip documented keys the test process
    # may carry from other tests.
    env = {key: value for key, value in os.environ.items() if key not in example_blocks()}
    return subprocess.run([sys.executable, str(SCRIPTS / "env_requirements.py"), str(path)],
                          env=env, capture_output=True)


def parsed_records(result):
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.endswith(b"\0")
    return dict(record.decode().split("=", 1) for record in result.stdout.split(b"\0")[:-1])


def test_example_parses_and_matches_dotenv(tmp_path):
    example = tmp_path / ".env"
    example.write_text((ROOT / ".env.example").read_text())
    parsed = parsed_records(parse_settings(example))
    assert parsed == dotenv_values(example, interpolate=False)
    assert set(parsed) == set(example_blocks())


def test_make_env_output_parses_in_temporary_checkout(tmp_path):
    (tmp_path / "scripts").mkdir()
    for name in ("Makefile", ".env.example", "scripts/setup.py", "scripts/env_requirements.py"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    result = subprocess.run(["make", "env", "API_TOKEN=1"], cwd=tmp_path,
                            text=True, capture_output=True, check=True)
    values = parsed_records(parse_settings(tmp_path / ".env"))
    assert set(values) == set(example_blocks())
    for key in ("SEARXNG_SECRET", "API_AUTH_TOKEN"):
        assert values[key] and values[key] not in result.stdout + result.stderr


def test_parser_accepts_literal_assignments_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("\n  # full-line comment\nMODEL_NAME=one=two#literal\nMODEL_API_KEY=\n"
                    "SEARXNG_URL='two words'\nARCHIVE_BASE_URL=\"more words\"\n")
    values = parsed_records(parse_settings(path))
    assert values == {"MODEL_NAME": "one=two#literal", "MODEL_API_KEY": "",
                      "SEARXNG_URL": "two words", "ARCHIVE_BASE_URL": "more words"}


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("value", ["a$b", "${UNSET_VAR}", "$(touch marker)", "a\\nb"])
def test_dollar_and_backslash_are_rejected_even_quoted(tmp_path, quote, value):
    """Compose and python-dotenv would interpolate or unescape these; reject them."""
    result = runner(tmp_path, f"RUN_MODE=test\nMODEL_API_KEY={quote}{value}{quote}\n")
    assert result.returncode != 0
    assert result.stderr == "invalid .env line 2 (MODEL_API_KEY)\n"
    assert value not in result.stdout + result.stderr
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("key", ["OPTIND", "PATH", "IFS", "BASH_ENV", "PS4", "SHELLOPTS",
                                 "lower_case"])
def test_shell_reserved_or_lowercase_names_are_rejected(tmp_path, key):
    value = "BASH_VERSINFO[0]"
    result = runner(tmp_path, f"RUN_MODE=test\n{key}='{value}'\n")
    assert result.returncode != 0
    assert value not in result.stdout + result.stderr
    assert "Would start" not in result.stdout


def test_settings_not_in_example_are_rejected(tmp_path):
    result = runner(tmp_path, "RUN_MODE=test\nFICTIONAL_UNDOCUMENTED=1\n")
    assert result.returncode != 0
    assert "FICTIONAL_UNDOCUMENTED" in result.stderr
    assert "Would start" not in result.stdout


def test_caller_values_win_over_the_file_even_when_empty(tmp_path):
    path = tmp_path / ".env"
    path.write_text("RUN_MODE=test\nAPI_AUTH_TOKEN=\nKARAKUTU_API_PORT=2200\n"
                    "MODEL_NAME=from-file\n")
    env = {key: value for key, value in os.environ.items() if key not in example_blocks()}
    env.update({"API_AUTH_TOKEN": "f" * 64, "KARAKUTU_API_PORT": "3300", "MODEL_NAME": ""})
    result = subprocess.run([sys.executable, str(SCRIPTS / "env_requirements.py"), str(path)],
                            env=env, capture_output=True)
    records = parsed_records(result)
    assert records == {"RUN_MODE": "test"}


@pytest.mark.parametrize("value", [
    "fictional-private-value;touch marker", "$(touch marker)", "`touch marker`", "$VAR",
    "two words", "value&other", "value|other", "value<other", "value>other", "(value)",
    r"value\other", 'un"quoted', "un'quoted", '"unclosed', "'closed' suffix",
    "'inline' #comment", '"embedded"quote"', "value\0tail",
])
def test_unquoted_or_malformed_values_fail_without_disclosure(tmp_path, value):
    text = "RUN_MODE=test\nMODEL_API_KEY=" + value + "\n"
    result = runner(tmp_path, text)
    assert result.returncode != 0
    assert result.stderr == "invalid .env line 2 (MODEL_API_KEY)\n"
    assert value not in result.stdout + result.stderr
    assert "Would start" not in result.stdout
    assert not (tmp_path / "marker").exists()
    parsed = parse_settings(tmp_path / ".env")
    assert parsed.returncode != 0 and not parsed.stdout


@pytest.mark.parametrize("line", ["export KEY=value", " KEY=value", "9KEY=value", "value"])
def test_invalid_assignment_only_reports_line_number(tmp_path, line):
    result = runner(tmp_path, "# comment\n" + line + "\n")
    assert result.returncode != 0
    assert result.stderr == "invalid .env line 2\n"


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("value", [
    "fictional-private-value;touch marker", "`touch marker`", "a|b && c > d",
])
def test_quoted_shell_text_is_exported_literally_and_never_executed(tmp_path, quote, value):
    # Observe the preflight's environment without launching any servers.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "python3"
    wrapper.write_text('''#!/bin/bash
if [[ "$1" == - ]]; then
  "$RUNNER_TEST_PYTHON" -c 'import json,os; from pathlib import Path; Path(
os.environ["RUNNER_TEST_CAPTURE"]).write_text(json.dumps(os.environ["MODEL_API_KEY"]))'
fi
exec "$RUNNER_TEST_PYTHON" "$@"
''')
    wrapper.chmod(0o700)
    capture = tmp_path / "capture.json"
    result = runner(tmp_path, f"RUN_MODE=test\nMODEL_API_KEY={quote}{value}{quote}\n",
                    extra_env={"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                               "RUNNER_TEST_PYTHON": sys.executable,
                               "RUNNER_TEST_CAPTURE": str(capture)})
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text()) == value
    assert parsed_records(parse_settings(tmp_path / ".env"))["MODEL_API_KEY"] == value
    assert value not in result.stdout + result.stderr
    assert not (tmp_path / "marker").exists()


def setup(tmp_path, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / "setup.py"), "--dir", str(tmp_path),
                           *args], text=True, capture_output=True, check=True)


@pytest.mark.parametrize("api_token", [False, True])
def test_setup_creates_private_file_and_never_prints_secrets(tmp_path, api_token):
    result = setup(tmp_path, *(["--api-token"] if api_token else []))
    path = tmp_path / ".env"
    values = dotenv_values(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert re.fullmatch("[a-f0-9]{64}", values["SEARXNG_SECRET"])
    assert bool(values["API_AUTH_TOKEN"]) == api_token
    if api_token:
        assert re.fullmatch("[a-f0-9]{64}", values["API_AUTH_TOKEN"])
    for key in ("SEARXNG_SECRET", "API_AUTH_TOKEN"):
        if values[key]:
            assert values[key] not in result.stdout + result.stderr
    assert "test: no required values unset" in result.stdout
    assert "live: no required values unset" in result.stdout


def test_setup_existing_file_is_byte_for_byte_unchanged(tmp_path):
    path = tmp_path / ".env"
    content = ("RUN_MODE=live\nMODEL_ENABLED=true\nMODEL_NAME=\nMODEL_BASE_URL=\n"
               "SEARCH_PROVIDER=searxng\nSEARXNG_SECRET=\nAPI_AUTH_TOKEN=\n")
    path.write_text(content)
    path.chmod(0o640)
    before = path.stat()
    result = setup(tmp_path, "--api-token")
    assert path.read_text() == content
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert "exists, left unchanged" in result.stdout
    for key in ("MODEL_NAME", "MODEL_BASE_URL", "SEARXNG_SECRET"):
        assert key in result.stdout


def test_gitignore_covers_runtime_settings_without_creating_them():
    for name in (".env", ".env.local", "nested/.env", "nested/.env.production"):
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", name], cwd=ROOT)
        assert result.returncode == 0
    assert subprocess.run(["git", "check-ignore", "--no-index", "-q", ".env.example"],
                          cwd=ROOT).returncode == 1


FAKE_UV_SYNC = """#!/bin/sh
# Stands in for `uv sync --check`; current unless the test says otherwise.
exit "${FAKE_UV_SYNC_EXIT:-0}"
"""


def runner_repo(tmp_path):
    """A copy of the runner scripts with a current fabricated install.

    The dependency check must judge this fixture, not the checkout running the tests.
    """
    repo, bin_dir = tmp_path / "repo", tmp_path / "repo-bin"
    if repo.exists():
        return repo, bin_dir
    (repo / "scripts").mkdir(parents=True)
    for name in ("run-local.sh", "env_requirements.py", "check_local_deps.py"):
        shutil.copy2(SCRIPTS / name, repo / "scripts" / name)
    shutil.copy2(ROOT / ".env.example", repo / ".env.example")
    (repo / "web/node_modules/fictional-dep").mkdir(parents=True)
    dep = {"node_modules/fictional-dep": {"version": "1.0.0"}}
    (repo / "web/package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "fictional-web"}, **dep}}))
    (repo / "web/node_modules/.package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": dep}))
    (repo / "web/node_modules/fictional-dep/package.json").write_text('{"version": "1.0.0"}')
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(FAKE_UV_SYNC)
    (bin_dir / "uv").chmod(0o700)
    return repo, bin_dir


def runner(tmp_path, text=None, *, mode=None, extra_env=None):
    path = tmp_path / ".env"
    if text is not None:
        path.write_text(text)
    repo, bin_dir = runner_repo(tmp_path)
    env = {key: value for key, value in os.environ.items() if key not in example_blocks()}
    if mode:
        env["RUN_MODE"] = mode
    env.update(extra_env or {})
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    return subprocess.run(["bash", str(repo / "scripts/run-local.sh"), "--check", "--env-file",
                           str(path)], cwd=tmp_path, env=env, text=True, capture_output=True)


def test_runner_stops_on_a_stale_web_install_with_the_command(tmp_path):
    result = runner(tmp_path, "RUN_MODE=test\n")
    assert result.returncode == 0, result.stderr
    assert "dependencies" not in result.stdout + result.stderr  # Quiet when current.
    installed = tmp_path / "repo/web/node_modules/fictional-dep/package.json"
    installed.write_text(installed.read_text().replace("1.0.0", "0.9.0"))
    result = runner(tmp_path)
    assert result.returncode != 0
    assert "run: npm --prefix web ci" in result.stderr
    assert "uv sync" not in result.stderr
    assert "Would start" not in result.stdout


def test_runner_stops_on_a_stale_python_install_with_the_command(tmp_path):
    result = runner(tmp_path, "RUN_MODE=test\n", extra_env={"FAKE_UV_SYNC_EXIT": "1"})
    assert result.returncode != 0
    assert "run: uv sync --frozen" in result.stderr
    assert "npm" not in result.stderr
    assert "Would start" not in result.stdout


def test_runner_missing_file_names_setup(tmp_path):
    result = runner(tmp_path)
    assert result.returncode != 0
    assert "make env" in result.stderr


@pytest.mark.parametrize("text", [
    "", "RUN_MODE=test\nMODEL_ENABLED=true\nSEARCH_PROVIDER=searxng\n",
])
def test_runner_test_never_starts_search(tmp_path, text):
    result = runner(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert "Would start one API" in result.stdout
    assert "Would start the UI" in result.stdout
    assert "Would start SearXNG" not in result.stdout


def test_runner_live_check_and_mode_override(tmp_path):
    setup(tmp_path, "--api-token")
    values = dotenv_values(tmp_path / ".env")
    result = runner(tmp_path, mode="live")
    assert result.returncode == 0, result.stderr
    assert "Would start SearXNG" in result.stdout
    for key in ("SEARXNG_SECRET", "API_AUTH_TOKEN"):
        assert values[key] not in result.stdout + result.stderr
    result = runner(tmp_path, mode="test")
    assert result.returncode == 0
    assert "Would start SearXNG" not in result.stdout


@pytest.mark.parametrize("text, names", [
    ("RUN_MODE=live\nMODEL_ENABLED=false\n", ["MODEL_ENABLED"]),
    ("RUN_MODE=live\nMODEL_ENABLED=true\nMODEL_BASE_URL=\nMODEL_NAME=\n"
     "SEARCH_PROVIDER=searxng\n", ["MODEL_BASE_URL", "MODEL_NAME", "SEARXNG_SECRET"]),
    ("RUN_MODE=test\nKARAKUTU_BIND_ADDRESS=0.0.0.0\n", ["API_AUTH_TOKEN"]),
    ("RUN_MODE=test\nKARAKUTU_WEB_PORT=invalid\n", ["KARAKUTU_WEB_PORT"]),
    ("RUN_MODE=invalid\n", ["RUN_MODE"]),
])
def test_runner_missing_values_fail_by_name(tmp_path, text, names):
    result = runner(tmp_path, text)
    assert result.returncode != 0
    for name in names:
        assert name in result.stdout


@pytest.mark.parametrize("provider", ["lmstudio", "openai_compatible"])
def test_runner_allows_unauthenticated_local_model(tmp_path, provider):
    result = runner(tmp_path, "RUN_MODE=live\nMODEL_ENABLED=true\nMODEL_API_KEY=\n"
                    f"MODEL_PROVIDER={provider}\nMODEL_BASE_URL=http://127.0.0.1:1234/v1\n")
    assert result.returncode == 0, result.stderr
    assert "live: no required values unset" in result.stdout


def test_runner_parser_process_failure_aborts(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "python3"
    wrapper.write_text("#!/bin/bash\nexit 7\n")
    wrapper.chmod(0o700)
    result = runner(tmp_path, "RUN_MODE=test\n", extra_env={
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
    })
    assert result.returncode != 0
    assert "Would start" not in result.stdout


def test_runner_control_variables_cannot_be_overwritten(tmp_path):
    value = "fictional_array[$(touch marker)]"
    result = runner(tmp_path, f"RUN_MODE=test\ncheck='{value}'\n")
    assert result.returncode != 0
    assert value not in result.stdout + result.stderr
    assert not (tmp_path / "marker").exists()


def test_worktree_changes_pass_content_hygiene():
    # The standard gate examines the index. Also scan unstaged/new card files here.
    spec = importlib.util.spec_from_file_location("hygiene", SCRIPTS / "check_data_hygiene.py")
    hygiene = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hygiene)
    paths = subprocess.check_output(
        ["git", "ls-files", "--modified", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode().split("\0")
    for name in paths:
        if not name or not (ROOT / name).is_file():
            continue
        assert not hygiene.content_violations(name, (ROOT / name).read_text()), name


def test_invalid_mode_value_is_not_printed(tmp_path):
    private_value = "fictional-private-mode"
    result = runner(tmp_path, "RUN_MODE=" + private_value + "\n")
    assert result.returncode != 0
    assert private_value not in result.stdout + result.stderr


# A cap for a hung run, never a pace: every wait below returns as soon as its event happens,
# so a slow machine only makes the test slower, not red.
RUNNER_HANG_SECONDS = 120
# Shell, not Python: a fake must start in milliseconds even when the machine is busy.
FAKE_CHILD = """#!/bin/sh
name=${0##*/}
log() { printf '%s %s\\n' "$name" "$*" >> "$RUNNER_TEST_EVENTS"; }
if [ "$name" = docker ] || [ "$1" = sync ]; then
  log "$*"
  exit 0
fi
# The handler is set before "started" is logged, so a stop is never missed.
trap '${nap:+kill "$nap"} 2>/dev/null; log stopped; exit 0' TERM
log started
[ "$name" = uv ] && log "search_url ${SEARXNG_URL-}"
while :; do
  sleep 1 & nap=$!
  wait "$nap"
done
"""


@pytest.mark.parametrize("search", [False, True])
def test_runner_terminates_children_using_only_fake_commands(tmp_path, search):
    # Never start servers or Docker. PATH substitutes tiny processes that only wait.
    import signal
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    events = tmp_path / "events"
    for name in ("uv", "node", "docker"):
        executable = bin_dir / name
        executable.write_text(FAKE_CHILD)
        executable.chmod(0o700)
    env_path = tmp_path / ".env"
    env_path.write_text("RUN_MODE=live\nMODEL_ENABLED=true\nSEARXNG_SECRET=fictional\n"
                        + ("SEARCH_PROVIDER=searxng\nSEARXNG_URL=\nKARAKUTU_SEARCH_PORT=2252\n"
                           if search else "SEARCH_PROVIDER=disabled\n"))
    env = {key: value for key, value in os.environ.items() if key not in example_blocks()}
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["RUNNER_TEST_EVENTS"] = str(events)
    repo, _ = runner_repo(tmp_path)
    process = subprocess.Popen(["bash", str(repo / "scripts/run-local.sh"), "--env-file",
                                str(env_path)], cwd=tmp_path, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def seen():
        return events.read_text() if events.exists() else ""

    try:
        deadline = time.monotonic() + RUNNER_HANG_SECONDS
        while not ("uv started" in seen() and "node started" in seen()):
            if process.poll() is not None:
                out, err = process.communicate()
                pytest.fail(f"Runner exited {process.returncode} before both children "
                            f"started.\nevents:\n{seen()}\nstdout:\n{out}\nstderr:\n{err}")
            if time.monotonic() > deadline:
                pytest.fail(f"Fake API/UI did not start.\nevents:\n{seen()}")
            time.sleep(0.02)
        process.send_signal(signal.SIGINT)
        _, err = process.communicate(timeout=RUNNER_HANG_SECONDS)
        text = seen()
        assert "uv stopped" in text and "node stopped" in text, (text, err)
        # The offline dependency check runs before anything starts.
        assert text.index("uv sync --frozen --check --offline") < text.index("uv started")
        assert ("docker " in text) == search
        if search:
            assert "uv search_url http://127.0.0.1:2252" in text
            assert "up -d --wait" in text
            assert "stop karakutu-audit-search" in text
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=RUNNER_HANG_SECONDS)


@pytest.mark.parametrize('assignment', ['', 'SEARXNG_URL=\n'])
def test_runner_derives_search_url_from_published_port(tmp_path, assignment):
    result = runner(tmp_path, 'RUN_MODE=live\nMODEL_ENABLED=true\nSEARCH_PROVIDER=searxng\n'
                    'SEARXNG_SECRET=fictional\nKARAKUTU_SEARCH_PORT=2252\n' + assignment)
    assert result.returncode == 0, result.stderr
    assert 'Derived SEARXNG_URL=http://127.0.0.1:2252' in result.stderr
    assert 'Would start SearXNG' in result.stdout


@pytest.mark.parametrize('url, port', [
    ('http://127.0.0.1:2202', '2252'), ('http://localhost:2252', ''),
    ('http://[::1]:2202', '2252'), ('http://localhost', '2252'),
    ('http://127.0.0.2:2202', '2252'), ('http://[0:0:0:0:0:0:0:1]:2202', '2252'),
    ('http://localhost.:2202', '2252'), ('http://app.localhost:2202', '2252'),
    ('http://127.1:2202', '2252'), ('http://2130706433:2202', '2252'),
    ('http://0x7f.1:2202', '2252'),
])
def test_runner_refuses_loopback_port_mismatch_before_start(tmp_path, url, port):
    result = runner(tmp_path, 'RUN_MODE=live\nMODEL_ENABLED=true\nSEARCH_PROVIDER=searxng\n'
                    f'SEARXNG_SECRET=fictional\nSEARXNG_URL={url}\n'
                    f'KARAKUTU_SEARCH_PORT={port}\n')
    assert result.returncode != 0
    assert 'SEARXNG_URL loopback port' in result.stderr
    assert f'KARAKUTU_SEARCH_PORT={port or "2202"}' in result.stderr
    assert 'clear it to derive it' in result.stderr
    assert 'Would start' not in result.stdout


@pytest.mark.parametrize('url', ['https://search.example:4452', 'http://127.0.0.1:2252',
                                'http://localhost:2252', 'http://[::1]:2252'])
def test_runner_leaves_remote_and_matching_urls_alone(tmp_path, url):
    result = runner(tmp_path, 'RUN_MODE=live\nMODEL_ENABLED=true\nSEARCH_PROVIDER=searxng\n'
                    'SEARXNG_SECRET=fictional\nKARAKUTU_SEARCH_PORT=2252\n'
                    f'SEARXNG_URL={url}\n')
    assert result.returncode == 0, result.stderr
    assert 'Derived SEARXNG_URL' not in result.stderr


def test_runner_test_mode_ignores_search_port_mismatch(tmp_path):
    result = runner(tmp_path, 'RUN_MODE=test\nSEARCH_PROVIDER=searxng\n'
                    'KARAKUTU_SEARCH_PORT=2252\nSEARXNG_URL=http://127.0.0.1:2202\n')
    assert result.returncode == 0, result.stderr
    assert 'Derived SEARXNG_URL' not in result.stderr
    assert 'Would start SearXNG' not in result.stdout
