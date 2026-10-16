#!/usr/bin/env bash
# Local development server: creates a venv on first run, installs the runtime deps and starts
# uvicorn with --reload on $PORT (default 2200). The project requires Python 3.11 exactly;
# override the interpreter with PYTHON=/path/to/python3.11.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3.11}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found (the project requires Python 3.11; set PYTHON=...)" >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q --disable-pip-version-check -r requirements.txt

exec uvicorn src.api.app:app --reload --port "${PORT:-2200}"
