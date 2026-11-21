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
# Fields that identify an installed package; any difference means a different install.
IDENTITY = ("version", "resolved", "integrity")


def _packages(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    packages = data.get("packages") if isinstance(data, dict) else None
    return packages if isinstance(packages, dict) else None


def web_current(web: Path) -> bool:
    """Compare the committed lockfile with npm's record of what it installed.

    npm writes `node_modules/.package-lock.json` after every install. A package in the
    lockfile that is not installed, or installed at another version, means the install
    predates the lockfile. Optional packages may be absent (other platforms).
    """
    wanted = _packages(web / "package-lock.json")
    installed = _packages(web / "node_modules" / ".package-lock.json")
    if wanted is None or installed is None:
        return False
    for name, entry in wanted.items():
        if not name or not isinstance(entry, dict) or entry.get("link"):
            continue
        have = installed.get(name)
        if have is None:
            if entry.get("optional") or entry.get("devOptional"):
                continue
            return False
        if not isinstance(have, dict) or any(
                key in entry and have.get(key) != entry[key] for key in IDENTITY):
            return False
    return True


def python_problem(repo: Path) -> str | None:
    """Return a hint unless the project environment matches uv.lock (no network)."""
    uv = shutil.which("uv")
    if uv is None:
        return NO_UV_HINT
    result = subprocess.run(
        [uv, "sync", "--frozen", "--check", "--offline", "--quiet"],
        cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False,
    )
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
