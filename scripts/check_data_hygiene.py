"""Fail if article data, secrets, personal data or generated reports enter the Git index.

Two checks over every staged/tracked blob (read from the index, i.e. what a commit would hold):

- paths: corpora, reports, exports, databases, keyfiles and `.env` files must stay ignored;
- content: no home-directory paths, email addresses, private keys, keyfile members or API
  tokens; no internal tracker ids (`KRK-` and four digits, in any file or path, binaries
  included); no URLs on real hosts; and no bare real hostnames in prose, comments or string literals. Reserved names
  pass (`.example`/`.invalid`/`.test`/`.localhost`, `example.<tld>`, internal suffixes,
  single-label and numeric hosts, IP literals). Third-party text (`LICENSE`, lockfiles) is
  exempt from the email/URL/host rules only. Outlet domains are reference data and live only in
  the files listed in `reference_files`.

Content patterns cannot recognise real article prose or real names, so no personal name is
listed here either. Never paste real article text, headlines or names (including owners' and
contributors' names) into source, prompts, tests or documentation.
"""
from pathlib import Path
import io
import ipaddress
import re
import subprocess
import sys
import tokenize

root = Path(__file__).resolve().parents[1]
allowed_json = {"web/package.json", "web/package-lock.json", "web/tsconfig.json", "web/tsconfig.app.json", "web/tsconfig.node.json",
                "docs/audit-record.v2.schema.json"}
private_dirs = {"data", "datasets", "fixtures", "testdata", "test-data", "benchmarks", "live", "reports", "exports", "artifacts", "runs", "private"}
private_suffixes = {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".parquet", ".arrow", ".db", ".sqlite", ".sqlite3", ".jwk", ".pem"}
probes = ["data/benchmark/case.json", "data/live/run.json", "tests/fixtures/article.json", "reports/review.html", "exports/run.json", "unplaced-article.json", "local.sqlite3-wal", ".env"]

# Third-party text whose URLs/emails are not ours to rewrite (licence boilerplate, lockfiles).
url_exempt = {"LICENSE", "uv.lock", "web/package-lock.json"}
# Production lookup tables of outlet hosts (source attribution, language routing).
reference_files = {"backend/audit/workflow/source.py", "backend/audit/search/urls.py", "backend/audit/workflow/web_search.py"}
# RFC 2606/6761 names and the internal suffixes the SSRF tests probe.
reserved_suffixes = (".example", ".invalid", ".test", ".localhost", ".local", ".internal", ".arpa", ".svc")
reserved_example = re.compile(r"(?:^|\.)example(?:\.[a-z]{2,3}){1,2}$")  # example.com, example.co.uk
numeric_host = re.compile(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*")  # 127.1, 0x7f.0.0.1

home_path = re.compile(r"(?<![\w.:/-])/(?:Users|home)/[\w.-]+(?=[/\s\"'`)\]]|$)|[A-Za-z]:\\{1,2}Users\\{1,2}")
email = re.compile(r"[\w.+-]+@((?:[\w-]+\.)+[^\W\d_]{2,})(?![\w.-])")
file_suffixes = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "tgz", "gz", "zip", "js", "mjs", "ts", "css", "map", "json"}
secret = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
    r"""|["'](?:d|p|q|dp|dq|qi|k)["']\s*:\s*["'][A-Za-z0-9_-]{40,}"""  # JWK private members
    r"|\beyJ[\w-]{10,}\.[\w-]{10,}\.|\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}|\bxox[abprs]-[\w-]{10,}")
# Internal tracker ids belong to the private board, not the public repository; describe the
# change in plain words instead.
tracker_id = re.compile(r"krk-[0-9]{4}", re.IGNORECASE | re.ASCII)  # ASCII only, as in binaries
tracker_id_bytes = re.compile(tracker_id.pattern.encode(), re.IGNORECASE)
url_host = re.compile(r"(?:\b[a-z][a-z0-9+.-]*:)?(?:\\?/){2}(?:[^\s/@\"'<>\\]*@)?(\[[0-9a-f:.%\w]+\]|[^\W_][^\s/:?#\"'`<>@\[\]\\(){}$]*)", re.IGNORECASE)
tlds = ("com|net|org|az|tr|cy|media|tv|xyz|info|news|io|gov|edu|me|co|uk|de|ru|fr|eu|ge|ua|am|kz|ir"
        "|uz|kg|by|pl|nl|ch|se|dk|fi|ca|au|jp|cn|kr|br|mx|online|site|press|live|today|world|ai")
bare_host = re.compile(rf"(?<![\w@.-])((?:[\w-]+\.)+(?:{tlds}))(?:\.[a-z]{{2}})?(?!\.?[\w-]|\s*\()", re.IGNORECASE)
code_suffixes = {".ts", ".tsx", ".js", ".mjs", ".css"}
code_text = re.compile(r"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`", re.DOTALL)
# Required build dependency, never an article host. Only this exact pinned FROM
# directive is exempt; comments, URLs and other registry references stay scanned.
uv_build_image = re.compile(r"FROM ghcr[.]io/astral-sh/uv:[0-9]+[.][0-9]+[.][0-9]+ AS uv")
# The public project's own clone command in the README quick start. Only this exact line in
# README.md is exempt; every other mention of that host stays scanned.
project_clone = re.compile(r"git clone https://github[.]com/karakutu-xyz/karakutu-ai-audit[.]git")
# The JSON Schema dialect identifier of the audit record contract. Only this exact string, and
# only in the two files that declare it, is exempt; every other URL there stays scanned.
json_schema_dialect = "https://json-schema" + ".org/draft/2020-12/schema"  # split: self-scan
json_schema_files = {"backend/audit/record.py", "docs/audit-record.v2.schema.json"}


def path_violations(paths):
    return [name for name in paths if name and name not in allowed_json and (
        set(Path(name).parts) & private_dirs or Path(name).suffix in private_suffixes
        or (Path(name).name.startswith(".env") and Path(name).name != ".env.example")
        or "arweave-keyfile" in Path(name).name or tracker_id.search(name)
    )]


def is_reserved_host(host):
    host = host.strip("[]").rstrip(".,;").lower().split("%")[0]
    try:
        ipaddress.ip_address(host)
        return True  # address literals carry no article or person
    except ValueError:
        pass
    return ("." not in host or host.endswith(reserved_suffixes) or numeric_host.fullmatch(host) is not None
            or reserved_example.search(host) is not None)


def prose_spans(name, text):
    """(first line, text) pieces where a bare hostname would be content rather than code:
    strings and comments of code files, everything elsewhere."""
    if name.endswith(".py"):
        try:
            return [(tok.start[0], tok.string) for tok in tokenize.generate_tokens(io.StringIO(text).readline)
                    if tok.type in (tokenize.STRING, tokenize.COMMENT)]
        except (tokenize.TokenError, SyntaxError, IndentationError):
            return [(1, text)]
    if Path(name).suffix in code_suffixes:
        return [(text.count("\n", 0, m.start()) + 1, m.group()) for m in code_text.finditer(text)]
    return [(1, text)]


def content_violations(name, text):
    """Sorted (line, kind) pairs for one file's text; never echoes the matched value."""
    found = set()
    if name in json_schema_files:
        text = text.replace(f'"{json_schema_dialect}"', '""')
    for number, line in enumerate(text.splitlines(), 1):
        if home_path.search(line):
            found.add((number, "home-directory path"))
        if secret.search(line):
            found.add((number, "private key or token"))
        if tracker_id.search(line):
            found.add((number, "internal tracker id"))
        if name in url_exempt or (name == "README.md" and project_clone.fullmatch(line)):
            continue
        if any(m.group(1).rsplit(".", 1)[-1].lower() not in file_suffixes and not is_reserved_host(m.group(1))
               for m in email.finditer(line)):
            found.add((number, "email address"))
        if any(not is_reserved_host(m.group(1)) for m in url_host.finditer(line)):
            found.add((number, "URL on a real host"))
    if name not in url_exempt and name not in reference_files:
        for start, piece in prose_spans(name, text):
            for offset, line in enumerate(piece.splitlines()):
                if name == "backend/Dockerfile" and uv_build_image.fullmatch(line):
                    continue
                if name == "README.md" and project_clone.fullmatch(line):
                    continue
                if any(not is_reserved_host(m.group(1)) for m in bare_host.finditer(line)):
                    found.add((start + offset, "real host named"))
    return sorted(found)


def blob_findings(name, data):
    """Findings for one staged blob. Binaries are only searched for tracker ids; the path rules
    above still apply to them."""
    if b"\0" in data:
        return [f"{name}: internal tracker id (binary)"] if tracker_id_bytes.search(data) else []
    text = data.decode("utf-8", errors="replace")
    return [f"{name}:{number}: {kind}" for number, kind in content_violations(name, text)]


def index_blobs():
    """(path, bytes) for every regular file and symlink in the index — the staged content."""
    entries = subprocess.check_output(["git", "ls-files", "-s", "-z"], cwd=root).decode().split("\0")
    wanted = [(e.split("\t", 1)[1], e.split()[1]) for e in entries if e and e.split()[0] != "160000"]
    batch = subprocess.run(["git", "cat-file", "--batch"], cwd=root, check=True, capture_output=True,
                           input="".join(f"{sha}\n" for _, sha in wanted).encode()).stdout
    blobs, pos = [], 0
    for name, _ in wanted:
        header_end = batch.index(b"\n", pos)
        size = int(batch[pos:header_end].split()[2])
        blobs.append((name, batch[header_end + 1:header_end + 1 + size]))
        pos = header_end + 1 + size + 1
    return blobs


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    violations = path_violations(paths)
    if violations:
        print("Local-only data paths or tracker ids in paths are staged/tracked:", *violations, sep="\n", file=sys.stderr)
        raise SystemExit(1)
    for path in probes:
        if subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=root).returncode:
            raise SystemExit(f"Missing ignore rule: {path}")
    findings = [finding for name, data in index_blobs() for finding in blob_findings(name, data)]
    if findings:
        print("Personal data, secrets, tracker ids or real hosts in staged content:", *findings, sep="\n", file=sys.stderr)
        raise SystemExit(1)
    print("Data hygiene passed: corpora, reports, exports, databases and secrets are ignored; "
          "staged content holds no home paths, emails, keys, tokens, tracker ids or real hosts.")


if __name__ == "__main__":
    main()
