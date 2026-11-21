"""The hygiene guard's content rules. Offending strings are assembled at runtime so this file
itself passes the guard it tests."""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_data_hygiene.py"
_spec = importlib.util.spec_from_file_location("check_data_hygiene", _SCRIPT)
hygiene = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hygiene)

REAL = "realnews" + ".com"  # a non-reserved host, never written out whole
SLASHES = "/" + "/"


def kinds(name, text):
    return {kind for _, kind in hygiene.content_violations(name, text)}


@pytest.mark.parametrize("text,kind", [
    ("/" + "Users/someone/projects/x", "home-directory path"),
    ("cd `/" + "Users/someone`", "home-directory path"),
    ("/" + "home/someone/.cache", "home-directory path"),
    ("C:" + "\\Users\\someone\\x", "home-directory path"),
    ('"C:' + '\\\\Users\\\\someone"', "home-directory path"),
    ("contact: someone@" + REAL, "email address"),
    ("kişi@haberı" + ".com", "email address"),
    ("-----BEGIN " + "RSA PRIVATE KEY-----", "private key or token"),
    ("-----BEGIN " + "PGP PRIVATE KEY BLOCK-----", "private key or token"),
    ('{"d": "' + "A" * 64 + '"}', "private key or token"),
    ('{"k": "' + "A" * 64 + '"}', "private key or token"),
    ("token = " + "eyJ" + "a" * 12 + "." + "b" * 12 + ".c", "private key or token"),
    ("key = " + "AKIA" + "A" * 16, "private key or token"),
    ("see https:" + SLASHES + REAL + "/haber/1", "URL on a real host"),
    ("see HTTPS:" + SLASHES + "WWW." + REAL.upper() + ":8443/a", "URL on a real host"),
    ("see https:" + SLASHES + "haberı" + ".com.tr/a", "URL on a real host"),
    ("src=" + SLASHES + REAL + "/a.js", "URL on a real host"),
    ('"https:\\/\\/' + REAL + '\\/a"', "URL on a real host"),
    ("wss:" + SLASHES + REAL + "/socket", "URL on a real host"),
    ("https:" + SLASHES + "example." + REAL + "/a", "URL on a real host"),
    ("https:" + SLASHES + "news.example.org." + REAL + "/a", "URL on a real host"),
])
def test_flags_personal_data_secrets_and_real_urls(text, kind):
    assert kind in kinds("backend/audit/anything.md", text)


@pytest.mark.parametrize("text", [
    "https://news.example.org/haber/1", "https://a.example/x", "https://x.invalid/y",
    "https://app.test/a", "http://localhost:2200/docs", "http://127.0.0.1:8080/",
    "http://[::1]:8080/", "http://0x7f.0.0.1/", "http://127.1/", "http://searxng:8080/search",
    "http://metadata.google.internal/", "http://printer.local/", "https://news.example.co.uk/a",
    "https://user:password@archive.example/path", "reviewer@archive.example",
    "https://news.example.org/home/gundem/x", "logo@2x.png", "react@18.x", "x@w.T",
    '{"d": "x", "kty": "RSA"}',  # a fabricated stub, not key material
    "total = a // b", "// a comment after code", 'f"sqlite:///{data_dir}"',
    'f"https://www.{host}/a"', "https://www.site.example/a",
])
def test_reserved_and_test_namespaces_pass(text):
    assert kinds("docs/notes.md", text) == set()


@pytest.mark.parametrize("code", [
    'logger' + '.info("x")', "caplog" + ".info", "resp" + ".media", "self" + ".me = 1",
    "settings" + ".io",
])
def test_attribute_access_in_code_is_not_a_host(code):
    assert kinds("backend/tests/test_x.py", code) == set()
    assert kinds("web/src/x.ts", code) == set()


def test_css_selectors_are_not_hosts():
    assert kinds("web/src/styles.css", ".indicator i" + ".online { color: red }") == set()


@pytest.mark.parametrize("name,line", [
    ("backend/tests/test_x.py", f'URL = "{REAL}/a"'),
    ("backend/tests/test_x.py", f"# observed on {REAL}"),
    ("backend/tests/test_x.py", f'"""docstring\n    seen on {REAL.upper()} once\n"""'),
    ("web/src/x.tsx", f'const hint = "{REAL}"'),
    ("docs/workflow.md", f"seen on a/{REAL}"),
    (".env.example", f"# e.g. {REAL}"),
    ("docs/workflow.md", f"It was {REAL}."),
])
def test_bare_real_hosts_are_flagged_in_content(name, line):
    assert "real host named" in kinds(name, line)


def test_bare_hosts_are_allowed_only_in_reference_tables_and_third_party_text():
    line = f'"outlet": "{REAL}"'
    assert kinds("backend/audit/workflow/source.py", line) == set()
    assert kinds("backend/audit/engine.py", line) == {"real host named"}
    assert kinds("LICENSE", "http://www." + REAL + "/licenses/") == set()
    assert kinds("uv.lock", "-----BEGIN " + "PRIVATE KEY-----") == {"private key or token"}


def test_findings_never_echo_the_matched_value():
    value = "someone@" + REAL
    assert all(value not in str(v) for v in hygiene.content_violations("a.md", value))


def test_path_rules_still_reject_private_data_paths():
    assert hygiene.path_violations(["data/live/x.txt", "a.jsonl", ".env.local", "web/package.json",
                                    ".env.example", "README.md"]) == ["data/live/x.txt", "a.jsonl",
                                                                       ".env.local"]


def test_only_exact_pinned_uv_build_directive_is_exempt():
    directive = "FROM " + "ghcr" + ".io/astral-sh/uv:0.12.2 AS uv"
    assert kinds("backend/Dockerfile", directive) == set()
    assert kinds("docs/notes.md", directive) == {"real host named"}
    for line in [directive.replace(":0.12.2", ":latest"), "# " + directive,
                 directive + " # " + REAL, "FROM " + REAL + "/fabricated:1.0.0 AS uv"]:
        assert "real host named" in kinds("backend/Dockerfile", line)
    assert "URL on a real host" in kinds("backend/Dockerfile", "# https:" + SLASHES + REAL)


def test_only_exact_readme_clone_line_is_exempt():
    host = "git" + "hub" + ".com"
    line = "git clone https:" + SLASHES + host + "/karakutu-xyz/karakutu-ai-audit.git"
    assert kinds("README.md", line) == set()
    assert kinds("docs/getting-started.md", line) == {"URL on a real host", "real host named"}
    for other in [line + " # " + REAL, "  " + line, line.replace("karakutu-ai-audit.git", "other.git"),
                  "See https:" + SLASHES + host + "/karakutu-xyz/karakutu-ai-audit"]:
        assert kinds("README.md", other) & {"URL on a real host", "real host named"}


def test_only_the_quoted_json_schema_dialect_is_exempt_in_the_record_contract():
    host = "json-" + "schema" + ".org"
    dialect = "https:" + SLASHES + host + "/draft/2020-12/schema"
    line = f'  "$schema": "{dialect}",'
    for name in ("docs/audit-record.v1.schema.json", "backend/audit/record.py"):
        assert kinds(name, line) == set()
        assert kinds(name, f'  "$id": "https:{SLASHES}{host}/other",') & {"URL on a real host"}
        assert kinds(name, line + " https:" + SLASHES + REAL) & {"URL on a real host"}
    assert kinds("docs/other.md", line) & {"URL on a real host"}


TRACKER = "K" + "RK-"  # the internal tracker prefix, never written out with a number


@pytest.mark.parametrize("name,line", [
    ("docs/notes.md", f"Posting is {TRACKER}0017."),
    ("CHANGELOG.md", f"- Add records ({TRACKER}0016): models"),
    ("backend/audit/demo.py", f"    # {TRACKER}0014: a cancelled status"),
    ("backend/audit/demo.py", f'LABEL = "{TRACKER}0001"'),
    ("web/src/x.tsx", f"// see {TRACKER.lower()}0042"),
    ("uv.lock", f"# {TRACKER}12345"),
    ("docs/notes.md", f"ref_{TRACKER}0001"),
    ("docs/notes.md", f"branch x-{TRACKER.lower()}0039-fix"),
    ("docs/notes.md", f"X{TRACKER}0001"),
])
def test_internal_tracker_ids_are_flagged_everywhere(name, line):
    assert "internal tracker id" in kinds(name, line)


@pytest.mark.parametrize("line", [
    f"{TRACKER}12", f"{TRACKER}123", f"{TRACKER}\u0661\u0662\u0663\u0664", "karakutu-0001", "\u212aRK-0001", "krk_0001", f"{TRACKER}abcd",
])
def test_lookalikes_of_tracker_ids_pass(line):
    assert "internal tracker id" not in kinds("docs/notes.md", line)


def test_tracker_findings_never_echo_the_id():
    value = f"{TRACKER}0039"
    assert all(value not in str(v) for v in hygiene.content_violations("a.md", value))


def test_tracker_ids_in_paths_are_rejected():
    assert hygiene.path_violations([f"docs/{TRACKER}0001-notes.md", "docs/notes.md"]) == [
        f"docs/{TRACKER}0001-notes.md"]


def test_binary_blobs_are_searched_for_tracker_ids_only():
    assert hygiene.blob_findings("web/x.png", b"\x89PNG\0" + f"{TRACKER}0001".encode()) == [
        "web/x.png: internal tracker id (binary)"]
    assert hygiene.blob_findings("web/x.png", b"\x89PNG\0someone@" + REAL.encode()) == []
    assert hygiene.blob_findings("a.md", f"x {TRACKER}0001".encode()) == ["a.md:1: internal tracker id"]
