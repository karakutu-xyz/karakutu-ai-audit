#!/usr/bin/env bash
# Local API/UI; Docker is needed only for bundled live search.
set -euo pipefail
repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
env_file=./.env
check=0
mode_override=${RUN_MODE-}
while (( $# )); do
  case "$1" in
    --check) check=1; shift ;;
    --env-file)
      [[ $# -ge 2 ]] || { echo '--env-file needs a path' >&2; exit 2; }
      env_file=$2; shift 2 ;;
    *) echo 'Usage: run-local.sh [--check] [--env-file PATH]' >&2; exit 2 ;;
  esac
done
[[ -f "$env_file" ]] || { echo 'Missing .env; run make env first.' >&2; exit 1; }
# Imported keys must not replace the runner's paths, flags or caller override.
readonly repo_dir env_file check mode_override
# Export literal assignments, never shell code. An empty record signals success:
# Bash 3.2 cannot wait for a process substitution to check its exit status.
env_parsed=0
while IFS= read -r -d '' kv; do
  if [[ -z "$kv" ]]; then
    env_parsed=1
  else
    # The parser emits a complete KEY=value assignment for export.
    # shellcheck disable=SC2163
    export "$kv"
  fi
done < <(python3 "$repo_dir/scripts/env_requirements.py" "$env_file" && printf '\0')
(( env_parsed )) || exit 1
# A one-command mode selection takes precedence over the file.
export RUN_MODE=${mode_override:-${RUN_MODE:-test}}
if [[ "$RUN_MODE" == live && ${SEARCH_PROVIDER:-disabled} == searxng ]]; then
  # Resolve/validate before preflight, Docker, or either server; also runs for --check.
  search_url=$(python3 "$repo_dir/scripts/env_requirements.py" --local-search)
  export SEARXNG_URL="$search_url"
fi
python3 - "$repo_dir/scripts" <<'PY'
import os
import sys
sys.path.insert(0, sys.argv[1])
from env_requirements import report
if not report(os.environ, os.environ["RUN_MODE"]):
    raise SystemExit(1)
PY
# A pulled checkout can keep an older install; stop before Vite fails on a missing
# import. Offline and quiet when current; prints the exact install command otherwise.
python3 "$repo_dir/scripts/check_local_deps.py" "$repo_dir" || exit 1
api_port=${KARAKUTU_API_PORT:-2200}
web_port=${KARAKUTU_WEB_PORT:-2201}
# An explicit proxy override is useful when the API is reached through a local proxy.
export KARAKUTU_API_PROXY=${KARAKUTU_API_PROXY:-http://127.0.0.1:$api_port}
search=0
if [[ "$RUN_MODE" == live && ${SEARCH_PROVIDER:-disabled} == searxng ]]; then
  search=1
fi
if (( check )); then
  (( ! search )) || echo 'Would start SearXNG with compose.search.yaml (loopback).'
  echo 'Would start one API process with uvicorn audit.api:app (no access log).'
  echo 'Would start the UI with Vite and the configured API proxy (loopback).'
  exit 0
fi
cd "$repo_dir"
export PYTHONPATH=backend
# Trace contents must remain local, as in Tilt.
export LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false
children=()
search_started=0
cleanup() {
  trap - EXIT INT TERM
  if (( ${#children[@]} )); then
    kill "${children[@]}" 2>/dev/null || true
    wait "${children[@]}" 2>/dev/null || true
  fi
  if (( search_started )); then
    docker compose -p karakutu-ai-audit -f compose.search.yaml stop \
      karakutu-audit-search >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if (( search )); then
  search_started=1
  docker compose -p karakutu-ai-audit -f compose.search.yaml up -d --wait
fi
uv run uvicorn audit.api:app --host 127.0.0.1 --port "$api_port" --no-access-log &
children+=("$!")
# exec Vite directly so its process is the child we terminate on Ctrl-C.
(cd web && exec node node_modules/vite/bin/vite.js \
  --host 127.0.0.1 --port "$web_port" --strictPort) &
children+=("$!")
# Bash 3.2 (macOS) has no wait -n. Stop the peer when either process exits.
while kill -0 "${children[0]}" 2>/dev/null && kill -0 "${children[1]}" 2>/dev/null; do
  sleep 1
done
for child in "${children[@]}"; do
  if ! kill -0 "$child" 2>/dev/null; then
    wait "$child"
    exit $?
  fi
done
