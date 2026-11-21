"""Keep the Docker context default-deny, including private directories nested in source."""
from fnmatch import fnmatchcase
from pathlib import Path

from test_data_hygiene import hygiene

ROOT = Path(__file__).resolve().parents[2]


def rules():
    return [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def included(path, patterns):
    # The allowlist uses only globs and directory rules. Match each ancestor as
    # well as the file, since ignoring a directory also ignores its descendants.
    parts = path.split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    result = True
    for rule in patterns:
        pattern = rule.removeprefix("!").rstrip("/")
        variants = [pattern, pattern.removeprefix("**/")]
        if any(fnmatchcase(candidate, variant) for candidate in candidates for variant in variants):
            result = rule.startswith("!")
    return result


def test_docker_context_excludes_private_data_and_has_no_private_allow_rules():
    patterns = rules()
    assert patterns[0] == "*"
    private = hygiene.private_dirs | {".env", ".env.local", ".env.example"}
    for rule in patterns:
        if rule.startswith("!"):
            components = set(rule[1:].strip("/").split("/"))
            assert not components & private
            assert not any(part.startswith(".env") for part in components)
            assert rule not in {"!*", "!**", "!**/*"}
    for directory in private:
        for parent in ["", "backend/audit/", "web/src/"]:
            assert not included(parent + directory, patterns)
            assert not included(parent + directory + "/fabricated.txt", patterns)
    for path in [".git/config", "backend/tests/test_api.py", "README.md",
                 "web/node_modules/package/index.js", "web/dist/index.html",
                 "backend/audit/__pycache__/config.pyc", "web/src/wallet-test.json",
                 "web/src/arweave-keyfile-test.json", "backend/audit/invented-keyfile.txt", "backend/audit/local.sqlite3-wal",
                 "backend/audit/local.db-shm", "web/src/key.pem", "web/src/key.jwk",
                 "backend/audit/fabricated-corpus.jsonl", "backend/audit/any-key.json",
                 "backend/audit/search/cases.ndjson", "web/src/rows.csv", "web/src/rows.tsv",
                 "backend/audit/table.parquet", "backend/audit/table.arrow",
                 "web/src/review.html.report", "web/src/fabricated.json",
                 "backend/audit/corpus.csv.gz", "backend/audit/rows.jsonl.zst", "web/src/export.zip",
                 "backend/audit/snapshot.tar", "backend/audit/snapshot.tgz", "backend/audit/audit.sqlite3.bak",
                 "backend/audit/export.CSV", "backend/audit/backup.SQLITE3", "backend/audit/corpus.JSONL.GZ",
                 "web/src/report.HTML.REPORT", "backend/audit/Rows.Csv", "backend/audit/local.DB",
                 "web/src/data.Json", "backend/audit/archive.ZIP", "backend/audit/.ENV",
                 "backend/audit/.Env.local", "backend/audit/Private/notes.txt", "web/src/DATA/x.txt",
                 "backend/audit/Reports/x.txt", "web/src/Arweave-Keyfile-x.txt", "backend/audit/Wallet-1.JSON"]:
        assert not included(path, patterns)
    for path in ["pyproject.toml", "uv.lock", "backend/audit/api.py",
                 "backend/audit/search/fetch.py", "searxng/settings.yml", "compose.yaml",
                 "compose.search.yaml", "compose.test.yaml", "compose.wallet.yaml", "web/package.json", "web/package-lock.json",
                 "web/tsconfig.json", "web/index.html", "web/vite.config.ts",
                 "web/src/main.tsx", "web/nginx.conf"]:
        assert included(path, patterns)
