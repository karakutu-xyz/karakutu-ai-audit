"""Offline check that local web and Python dependencies match the lockfiles.

`scripts/run-local.sh` runs this before starting anything. It prints nothing when both
installs are current, and otherwise one line per stale install naming the exact command
to run. It never installs, downloads or changes anything.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

WEB_HINT = "Web dependencies are missing or out of date; run: npm --prefix web ci"
PYTHON_HINT = "Python dependencies are missing or out of date; run: uv sync --frozen"
NO_UV_HINT = "uv is not installed; see docs/getting-started.md"
# Lockfile fields that identify an install; any difference means other package contents.
IDENTITY = ("version", "resolved", "integrity")
UV_TIMEOUT_SECONDS = 60
UV_TIMEOUT_HINT = (f"uv did not answer within {UV_TIMEOUT_SECONDS} s (another uv command may "
                   "hold the environment); finish it, or run: uv sync --frozen")


def _json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _packages(path: Path) -> dict | None:
    data = _json(path)
    packages = data.get("packages") if data else None
    return packages if isinstance(packages, dict) else None


def web_current(web: Path) -> bool:
    """Compare the committed lockfile with the install, both npm's record and the disk.

    npm writes `node_modules/.package-lock.json` when an install completes; every locked
    package must appear there with the same version, source and integrity. Its folder must
    also be on disk with that version, which catches a folder deleted after the install.
    Optional packages may be absent (built for other platforms); one whose folder exists
    must match like any other.
    """
    wanted = _packages(web / "package-lock.json")
    recorded = _packages(web / "node_modules" / ".package-lock.json")
    if wanted is None or recorded is None:
        return False
    for path, entry in wanted.items():
        # The root ("") and workspace sources are not installed copies; links point elsewhere.
        if not path.startswith("node_modules/") and "/node_modules/" not in path:
            continue
        if not isinstance(entry, dict) or entry.get("link"):
            continue
        folder = web / path
        if not folder.exists():
            if entry.get("optional") or entry.get("devOptional"):
                continue
            return False
        manifest, record = _json(folder / "package.json"), recorded.get(path)
        if manifest is None or not isinstance(record, dict):
            return False
        if "version" in entry and manifest.get("version") != entry["version"]:
            return False
        if any(key in entry and record.get(key) != entry[key] for key in IDENTITY):
            return False
    return True


def python_problem(repo: Path) -> str | None:
    """Return a hint unless the project environment matches uv.lock (no network)."""
    uv = shutil.which("uv")
    if uv is None:
        return NO_UV_HINT
    try:
        result = subprocess.run(
            [uv, "sync", "--frozen", "--check", "--offline", "--quiet"],
            cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=UV_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return UV_TIMEOUT_HINT
    return None if result.returncode == 0 else PYTHON_HINT


def problems(repo: Path) -> list[str]:
    found = [] if web_current(repo / "web") else [WEB_HINT]
    hint = python_problem(repo)
    if hint:
        found.append(hint)
    return found


def main() -> int:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    found = problems(repo)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
