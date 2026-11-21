#!/usr/bin/env bash
# End-to-end Docker smoke suite. Run it with `make smoke` from a checkout.
#
# It clones the committed HEAD (or SMOKE_REF) into a temporary directory, builds the images
# there with no cache, and checks test mode, live mode against an offline fake model,
# persistence, access control and image hygiene.
#
# Isolation: every Compose project, volume, container and image it creates carries a fresh
# random run ID (karakutu-smoke-<id>-...), it refuses to start if any of those names already
# exist, and cleanup removes only the names it recorded. A developer's stack and data are never
# touched. The live stage runs on an internal Docker network with no route out. Building needs
# the base images and package registries; after that nothing leaves the machine.
#
# Settings (environment):
#   SMOKE_REF            commit to test (default HEAD; uncommitted changes are not included)
#   SMOKE_PROJECT        name prefix (default karakutu-smoke; a run ID is always appended)
#   SMOKE_API_PORT, SMOKE_WEB_PORT, SMOKE_SEARCH_PORT   loopback ports (default 23100-23102)
#   SMOKE_UPGRADE_FROM   optional older commit with backend/Dockerfile: build it, create a
#                        volume with it, then check the current image migrates and keeps it
#   SMOKE_KEEP_IMAGES=1  keep the built images
set -euo pipefail
# Make reads these from the environment; the suite always passes its own values, and an
# inherited one must never point a smoke step at someone else's project, volume or image.
unset PROJECT TEST_PROJECT LIVE_FILES VOLUME API_IMAGE BACKUP_DIR BACKUP_FILE COMPOSE_PROJECT_NAME COMPOSE_FILE START
# `make smoke VAR=...` passes command-line overrides to child makes through these; drop them too.
unset MAKEFLAGS MFLAGS MAKEOVERRIDES GNUMAKEFLAGS MAKELEVEL

PREFIX=${SMOKE_PROJECT:-karakutu-smoke}
case $PREFIX in
  karakutu-ai-audit*|*[!a-z0-9-]*|-*|"") echo "SMOKE_PROJECT must be lowercase letters, digits and dashes and must not start with karakutu-ai-audit"; exit 2 ;;
esac
SRC=$(git rev-parse --show-toplevel)
REF=${SMOKE_REF:-HEAD}
SHA=$(git -C "$SRC" rev-parse --verify "$REF^{commit}")
RUN_ID=$(python3 -c 'import secrets; print(secrets.token_hex(3))')
P="$PREFIX-$RUN_ID"
export KARAKUTU_API_PORT=${SMOKE_API_PORT:-23100}
export KARAKUTU_WEB_PORT=${SMOKE_WEB_PORT:-23101}
export KARAKUTU_SEARCH_PORT=${SMOKE_SEARCH_PORT:-23102}
export KARAKUTU_BIND_ADDRESS=127.0.0.1
API_DIRECT="http://127.0.0.1:$KARAKUTU_API_PORT"
UI="http://127.0.0.1:$KARAKUTU_WEB_PORT"
API=$API_DIRECT
WORK=$(mktemp -d "${TMPDIR:-/tmp}/karakutu-smoke.XXXXXX")
REPO="$WORK/repo"
# The live-stage override and fake model always come from this suite, never from the commit under
# test, so an older commit cannot bring a stack without network isolation.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cp -R "$SCRIPT_DIR/smoke" "$WORK/smoke"
export SMOKE_DIR="$WORK/smoke"
SMOKE_OVERRIDE="$SMOKE_DIR/compose.smoke.yaml"
FAILED=0
STARTED=$(date +%s)

log() { printf '%s\n' "$*"; }
pass() { log "PASS  $*"; }
fail() { log "FAIL  $*"; FAILED=$((FAILED + 1)); }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
stage() { log ""; log "== $* ($(( $(date +%s) - STARTED ))s)"; }

projects=("$P" "$P-test" "$P-live" "$P-bind")
images=()
for project in "${projects[@]}"; do images+=("$project-karakutu-audit-api" "$project-karakutu-audit-web"); done
images+=("$P-old-api")
upgrade_container="$P-upgrade"
upgrade_volume="$P-upgrade-data"
CLONED=0
# Set only after preflight proved that no resource with this run's names existed, so cleanup
# can never remove something the suite did not create.
OWNED=0
cleanup() {
  set +e
  log ""
  log "== cleanup"
  if [ "$OWNED" != 1 ]; then
    rm -rf "$WORK"
    log "nothing created in Docker; removed the temporary directory"
    return
  fi
  if [ "$CLONED" = 1 ]; then
    for project in "${projects[@]}"; do
      docker compose -p "$project" -f "$REPO/compose.yaml" -f "$REPO/compose.test.yaml" \
        down --volumes --remove-orphans >/dev/null 2>&1
      docker compose -p "$project" -f "$REPO/compose.yaml" -f "$SMOKE_OVERRIDE" \
        down --volumes --remove-orphans >/dev/null 2>&1
    done
  fi
  docker rm -f "$upgrade_container" >/dev/null 2>&1
  docker volume rm "$upgrade_volume" >/dev/null 2>&1
  if [ "${SMOKE_KEEP_IMAGES:-0}" != "1" ]; then
    for image in "${images[@]}"; do
      docker image rm -f "$image" >/dev/null 2>&1
    done
  fi
  rm -rf "$WORK"
  log "removed the run's projects (${projects[*]}), volumes, images and temporary clone"
}
trap cleanup EXIT

# --- helpers -------------------------------------------------------------------------------
json() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$WORK/body" "$1"; }
# http METHOD PATH [curl args...] -> prints the status code, body in $WORK/body
http() {
  local method=$1 path=$2; shift 2
  curl -sS -o "$WORK/body" -w '%{http_code}' -X "$method" "$API$path" "$@" || echo 000
}
port_free() { ! python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$1"; }
wait_run() {  # wait_run RUN_ID [auth header] -> final status; body holds the run
  local id=$1 i status
  for i in $(seq 1 120); do
    if [ -n "${2:-}" ]; then http GET "/api/runs/$id" -H "$2" >/dev/null; else http GET "/api/runs/$id" >/dev/null; fi
    status=$(json 'd["status"]')
    case $status in succeeded|failed|cancelled) echo "$status"; return 0 ;; esac
    sleep 2
  done
  echo timeout
}
wait_health() {
  local i
  for i in $(seq 1 60); do
    [ "$(http GET /health)" = 200 ] && return 0
    sleep 2
  done
  return 1
}
benchmark_body() {  # benchmark_body TITLE CONTENT
  python3 -c 'import json,sys; print(json.dumps({"removed_article": {"url": "https://smoke-news.example/" + sys.argv[1].lower().replace(" ", "-"), "title": sys.argv[1], "content": sys.argv[2]}, "similar_articles": [], "label": "smoke suite"}))' "$1" "$2"
}
networks_internal() {  # networks_internal CONTAINER EXPECTED_NETWORK -> attached only to internal networks, including the expected one
  local names network internal
  [ -n "$1" ] || return 1
  names=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$1") || return 1
  case " $names " in *" $2 "*) ;; *) return 1 ;; esac
  for network in $names; do
    internal=$(docker network inspect -f '{{.Internal}}' "$network") || return 1
    [ "$internal" = true ] || return 1
  done
}
STORY="A fictional town council in an invented coastal region postponed its harbour budget vote. \
A made-up councillor said the documents were incomplete. Nothing in this text is real; it exists \
only to exercise the audit workflow inside the Docker smoke suite."

# --- preflight -----------------------------------------------------------------------------
stage "preflight"
for tool in docker git curl python3 make perl; do
  command -v "$tool" >/dev/null || { log "missing tool: $tool"; exit 2; }
done
docker info >/dev/null 2>&1 || { log "Docker is not running"; exit 2; }
# The smoke override uses `!override`/`!reset`, which need Compose 2.24.4 or newer.
compose_version=$(docker compose version --short 2>/dev/null || echo 0)
python3 -c 'import re,sys; v=tuple(int(x) for x in re.findall(r"\d+", sys.argv[1])[:3]); sys.exit(0 if v >= (2, 24, 4) else 1)' "$compose_version" \
  || { log "Docker Compose $compose_version is too old; the suite needs 2.24.4 or newer"; exit 2; }
for port in "$KARAKUTU_API_PORT" "$KARAKUTU_WEB_PORT" "$KARAKUTU_SEARCH_PORT"; do
  port_free "$port" || { log "port $port is in use; set SMOKE_API_PORT/SMOKE_WEB_PORT/SMOKE_SEARCH_PORT"; exit 2; }
done
# Read the whole inventory into a file first: each listing must succeed, and grep reads a file
# (no pipe that could end early and hide a match).
docker ps -a --format '{{.Label "com.docker.compose.project"}} {{.Names}}' > "$WORK/inventory" \
  && docker volume ls --format '{{.Label "com.docker.compose.project"}} {{.Name}}' >> "$WORK/inventory" \
  && docker network ls --format '{{.Label "com.docker.compose.project"}} {{.Name}}' >> "$WORK/inventory" \
  && docker image ls --format '{{.Repository}}' >> "$WORK/inventory" \
  || { log "cannot list Docker resources; refusing to start"; exit 2; }
# Compose names volumes and networks <project>_<name> and containers <project>-<service>-<n>.
if grep -qE "(^| )$P([-_ ]|$)" "$WORK/inventory"; then
  log "resources named $P already exist; refusing to touch them"; exit 2
fi
OWNED=1
if [ -n "$(git -C "$SRC" status --porcelain)" ]; then
  log "note: uncommitted changes are not tested; the suite uses commit ${SHA:0:12}"
fi
log "commit ${SHA:0:12}, run $P, ports $KARAKUTU_API_PORT-$KARAKUTU_SEARCH_PORT"

# --- fresh clone and build -----------------------------------------------------------------
stage "fresh clone and no-cache build"
git clone --quiet --no-hardlinks "$SRC" "$REPO"
git -C "$REPO" checkout --quiet --detach "$SHA"
CLONED=1
cd "$REPO"
MAKEVARS=(PROJECT="$P-live" TEST_PROJECT="$P-test")
# An older Makefile with fixed project names would act on a developer's own stack: refuse it
# before anything is built or started.
# Allowlist, not blocklist: every Compose project and every volume the planned commands name must
# belong to this run.
make -n test down test-reset backup restore "${MAKEVARS[@]}" BACKUP_DIR=. BACKUP_FILE=Makefile > "$WORK/plan" 2>/dev/null || true
grep -oE -- '-p [^ ]+' "$WORK/plan" | cut -d' ' -f2 | sort -u > "$WORK/plan-projects" || true
grep -oE -- '-v "?[^":/ ]+"?:/data' "$WORK/plan" | sed -E 's/^-v "?//; s/"?:\/data$//' | sort -u > "$WORK/plan-volumes" || true
plan_ok=1
grep -q -- "-p $P-test " "$WORK/plan" || plan_ok=0
while read -r project; do
  case $project in "$P-live"|"$P-test") ;; *) plan_ok=0 ;; esac
done < "$WORK/plan-projects"
while read -r volume; do
  [ "$volume" = "$P-live_karakutu-audit-data" ] || plan_ok=0
done < "$WORK/plan-volumes"
if [ "$plan_ok" != 1 ]; then
  log "commit ${SHA:0:12} plans Compose projects or volumes outside this run ($(tr '\n' ' ' < "$WORK/plan-projects")$(tr '\n' ' ' < "$WORK/plan-volumes")); refusing to start"
  exit 2
fi
check "clone has no .env or data/" test ! -e .env -a ! -e data
docker compose -p "$P" -f compose.yaml build --no-cache --quiet karakutu-audit-api karakutu-audit-web
export SMOKE_API_IMAGE="$P-karakutu-audit-api" SMOKE_WEB_IMAGE="$P-karakutu-audit-web"
for image in "$SMOKE_API_IMAGE" "$SMOKE_WEB_IMAGE"; do
  size=$(docker image inspect -f '{{.Size}}' "$image")
  log "image $image: $(( size / 1000000 )) MB"
  uid=$(docker run --rm --network none --entrypoint id "$image" -u)
  check "$image runs as non-root (uid $uid)" test "$uid" != 0
done

# --- image hygiene -------------------------------------------------------------------------
stage "image hygiene"
# Secrets and databases are forbidden anywhere in the application trees. Corpus formats and
# private-data folder names are forbidden outside installed packages (libraries ship their own
# `datasets/` modules and package metadata).
forbidden_files='(^|/)\.env($|\.)|\.(sqlite3?|db)($|[.-])|keyfile|wallet[^/]*\.json$|\.jwk$|\.(pem|key|p12|pfx)$'
forbidden_data='\.(json|jsonl|ndjson|csv|tsv|parquet|arrow|bak)(\.(gz|bz2|xz|zst|zip))?$|\.html\.report$|\.(gz|bz2|xz|zst|zip|tar|tgz|7z)$|(^|/)(data|datasets|reports|exports|fixtures|testdata|test-data|benchmarks|live|artifacts|private|runs)/'
for image in "$SMOKE_API_IMAGE" "$SMOKE_WEB_IMAGE"; do
  cid=$(docker create --network none "$image")
  docker export "$cid" | tar -t > "$WORK/files.txt"
  docker rm "$cid" >/dev/null
  app_files=$(grep -E '^(app|usr/share/nginx/html|etc/nginx)/' "$WORK/files.txt" || true)
  check "$image has application files to inspect" test -n "$app_files"
  # Case-insensitive: export.CSV or backup.SQLITE3 are the same data.
  bad=$( { printf '%s\n' "$app_files" | grep -v '/site-packages/' | grep -iE "$forbidden_files|$forbidden_data";
           printf '%s\n' "$app_files" | grep '/site-packages/' | grep -iE "$forbidden_files" | grep -vE '/certifi/cacert\.pem$'; } || true)
  check "$image holds no .env, database, key, corpus or data files" test -z "$bad"
  [ -z "$bad" ] || printf '      %s\n' $bad
  data_files=$(grep -E '^data/.+' "$WORK/files.txt" || true)
  check "$image has an empty /data" test -z "$data_files"
done

stage "compose files"
for files in "compose.yaml" "compose.yaml compose.test.yaml" "compose.search.yaml" \
             "compose.yaml $SMOKE_OVERRIDE"; do
  args=(); for f in $files; do args+=(-f "$f"); done
  SMOKE_API_TOKEN=x check "docker compose config: $files" docker compose -p "$P" "${args[@]}" config --quiet
done

# --- test mode -----------------------------------------------------------------------------
stage "test mode (make test)"
make --no-print-directory test "${MAKEVARS[@]}"
TEST=(docker compose -p "$P-test" -f compose.yaml -f compose.test.yaml)
check "health reports mode test" test "$(http GET /health)" = 200 -a "$(json 'd["mode"]')" = test
published=$("${TEST[@]}" port karakutu-audit-api 2200)
check "API is published on loopback only by default ($published)" test "${published%:*}" = 127.0.0.1
web_published=$("${TEST[@]}" port karakutu-audit-web 2201)
check "UI is published on loopback only by default ($web_published)" test "${web_published%:*}" = 127.0.0.1
for service in karakutu-audit-api karakutu-audit-web; do
  health=$(docker inspect -f '{{.State.Health.Status}}' "$("${TEST[@]}" ps -q "$service")")
  check "$service container is healthy ($health)" test "$health" = healthy
done
http GET "/api/runs?category=benchmark" >/dev/null
check "demo runs are listed ($(json 'd["total"]'))" test "$(json 'sum(r["id"].startswith("run_demo_") for r in d["items"]) > 0')" = True
code=$(http POST /api/benchmarks/runs -H 'Content-Type: application/json' --data "$(benchmark_body 'Smoke test-mode case' "$STORY")")
check "test-mode benchmark accepted ($code)" test "$code" = 202
id=$(json 'd["id"]')
status=$(wait_run "$id")
check "test-mode benchmark finished ($status, $(json 'd["verdict"]'))" test "$status" = succeeded
API=$UI
code=$(http POST /api/benchmarks/runs -H 'Content-Type: application/json' -H "Origin: http://localhost:$KARAKUTU_WEB_PORT" \
  --data "$(benchmark_body 'Smoke browser case' "$STORY")")
check "browser-style submit through the UI on port $KARAKUTU_WEB_PORT accepted ($code)" test "$code" = 202
code=$(http POST /api/benchmarks/runs -H 'Content-Type: application/json' -H 'Origin: http://elsewhere.example.test' --data '{}')
check "a foreign Origin is refused ($code)" test "$code" = 403
API=$API_DIRECT
make --no-print-directory down "${MAKEVARS[@]}"
check "make down stops the containers" test -z "$("${TEST[@]}" ps -q)"
check "make down keeps the test volume" test -n "$(docker volume ls -q --filter "name=^$P-test_karakutu-audit-data$")"
make --no-print-directory test-reset "${MAKEVARS[@]}"
check "make test-reset removes the test volume" test -z "$(docker volume ls -q --filter "name=^$P-test_karakutu-audit-data$")"

# --- access: fail closed on a public bind without a token ----------------------------------
stage "access: non-loopback bind without token"
set +e
KARAKUTU_BIND_ADDRESS=0.0.0.0 API_AUTH_TOKEN= perl -e 'alarm shift; exec @ARGV' 90 \
  docker compose -p "$P-bind" -f compose.yaml -f compose.test.yaml \
  run --rm --no-deps -T karakutu-audit-api > "$WORK/bind.log" 2>&1
bind_exit=$?
set -e
check "API refuses to start on 0.0.0.0 without API_AUTH_TOKEN (exit $bind_exit)" \
  test "$bind_exit" -ne 0 -a "$bind_exit" -ne 142
check "the refusal names the reason" grep -q "Non-loopback KARAKUTU_BIND_ADDRESS requires API_AUTH_TOKEN" "$WORK/bind.log"

# --- live mode against the fake model ------------------------------------------------------
stage "live mode against the offline fake model (isolated network, reached through the UI)"
SMOKE_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
export SMOKE_API_TOKEN
LIVE=(docker compose -p "$P-live" -f compose.yaml -f "$SMOKE_OVERRIDE")
"${LIVE[@]}" up -d --wait karakutu-fake-model karakutu-audit-api karakutu-audit-web
API=$UI
AUTH="Authorization: Bearer $SMOKE_API_TOKEN"
api_container=$("${LIVE[@]}" ps -q karakutu-audit-api)
# Isolation is a precondition, not a result: without it no article is submitted.
if networks_internal "$api_container" "$P-live_smoke-isolated" \
   && networks_internal "$("${LIVE[@]}" ps -q karakutu-fake-model)" "$P-live_smoke-isolated" \
   && [ -z "$(docker port "$api_container")" ]; then
  pass "API and fake model have no route out and the API is not published ($P-live_smoke-isolated only)"
else
  fail "live stack is not isolated (internal network $P-live_smoke-isolated, unpublished API)"
  log "aborting before any request reaches the live stack"
  exit 1
fi
check "health reports mode live" test "$(http GET /health)" = 200 -a "$(json 'd["mode"]')" = live
check "request without token gets 401" test "$(http GET /api/status)" = 401
check "request with token gets 200" test "$(http GET /api/status -H "$AUTH")" = 200
check "allowed host with token gets 200" test "$(http GET /api/status -H "$AUTH" -H 'Host: audit.example.test')" = 200
check "allowed host without token gets 401" test "$(http GET /api/status -H 'Host: audit.example.test')" = 401
check "unknown Host header gets 400" test "$(http GET /api/status -H "$AUTH" -H 'Host: elsewhere.example.test')" = 400
check "UI serves the app" test "$(http GET /)" = 200
check "UI serves a page address opened directly (/runs/$P-deep-link)" \
  test "$(http GET "/runs/$P-deep-link")" = 200 -a "$(grep -c '<div id="root"' "$WORK/body")" = 1

http GET /api/models -H "$AUTH" >/dev/null
check "provider models listed, embeddings filtered" test "$(json '"fake-relevance" in d["available"] and "fake-embedding" not in d["available"]')" = True
code=$(http POST /api/models/steps -H "$AUTH" -H 'Content-Type: application/json' \
  --data '{"step": "relevance", "model": "fake-relevance", "author": "Smoke suite", "note": "per-step check"}')
check "per-step model assignment accepted ($code)" test "$code" = 200

code=$(http POST /api/benchmarks/runs -H "$AUTH" -H 'Content-Type: application/json' --data "$(benchmark_body 'Smoke live case' "$STORY")")
check "live benchmark accepted ($code)" test "$code" = 202
live_id=$(json 'd["id"]')
status=$(wait_run "$live_id" "$AUTH")
log "      live run $status, verdict $(json 'd["verdict"]'), error $(json '(d.get("error") or {}).get("code")')"
# The fake model's answers and the unreachable .example article make this deterministic.
check "live benchmark succeeded with the expected verdict ($status, $(json 'd["verdict"]'))" \
  test "$status/$(json 'd["verdict"]')" = succeeded/INCONCLUSIVE
check "all six steps recorded" test "$(json '[s["name"] for s in d["steps"]]')" = "['classification', 'similarity', 'relevance', 'related_removals', 'source', 'web_search']"
check "relevance step completed on its assigned model" \
  test "$(json 'next(s["status"] + "/" + str(s["model"]) for s in d["steps"] if s["name"] == "relevance")')" = completed/fake-relevance
check "json_schema was rejected, then json_object parsed" \
  test "$(json 'any(c.get("error_type") == "BadRequestError" and c.get("response_format") == "json_schema" for s in d["steps"] for c in s["model_calls"]) and any(c.get("response_format") == "json_object" and c.get("parsed_output") is not None and c.get("error_type") is None for s in d["steps"] for c in s["model_calls"])')" = True

# --- persistence ---------------------------------------------------------------------------
stage "persistence and interruption"
code=$(http POST /api/benchmarks/runs -H "$AUTH" -H 'Content-Type: application/json' \
  --data "$(benchmark_body 'Smoke slow case' "SMOKE-SLOW $STORY")")
slow_id=$(json 'd["id"]')
for i in $(seq 1 30); do
  http GET "/api/runs/$slow_id" -H "$AUTH" >/dev/null
  [ "$(json 'd["status"]')" = running ] && break
  sleep 1
done
check "slow run is running before the restart" test "$(json 'd["status"]')" = running
"${LIVE[@]}" restart karakutu-audit-api >/dev/null
wait_health
http GET "/api/runs/$live_id" -H "$AUTH" >/dev/null
check "finished run survives an API restart" test "$(json 'd["status"]')" = "$status"
http GET "/api/runs/$slow_id" -H "$AUTH" >/dev/null
check "run in flight during the restart is marked interrupted" \
  test "$(json 'd["status"] + "/" + ((d.get("error") or {}).get("code") or "")')" = failed/interrupted
"${LIVE[@]}" down >/dev/null 2>&1
"${LIVE[@]}" up -d --wait karakutu-fake-model karakutu-audit-api karakutu-audit-web >/dev/null
http GET "/api/runs/$live_id" -H "$AUTH" >/dev/null
check "runs survive down/up in the named volume" test "$(json 'd["id"]')" = "$live_id"

# --- backup and restore (the documented make targets) --------------------------------------
stage "backup and restore"
BACKUPVARS=(PROJECT="$P-live" "LIVE_FILES=-f compose.yaml -f $SMOKE_OVERRIDE"
            API_IMAGE="$SMOKE_API_IMAGE" VOLUME="$P-live_karakutu-audit-data")
mkdir "$WORK/back ups"  # a space in the path on purpose
make --no-print-directory backup "${BACKUPVARS[@]}" BACKUP_DIR="$WORK/back ups" >/dev/null
backup_file=$(find "$WORK/back ups" -name "karakutu-audit-data-*.tar.gz" -type f | head -1)
check "make backup wrote an archive" test -s "$backup_file"
check "API is back after the backup" wait_health
code=$(http POST /api/benchmarks/runs -H "$AUTH" -H 'Content-Type: application/json' \
  --data "$(benchmark_body 'Smoke after backup' "$STORY")")
after_id=$(json 'd["id"]')
check "a run created after the backup is accepted ($code)" test "$code" = 202
wait_run "$after_id" "$AUTH" >/dev/null
head -c 200 "$backup_file" > "$WORK/damaged.tar.gz"
set +e
make --no-print-directory restore "${BACKUPVARS[@]}" BACKUP_FILE="$WORK/damaged.tar.gz" > "$WORK/restore-damaged.log" 2>&1
damaged_exit=$?
set -e
check "make restore refuses a damaged archive (exit $damaged_exit)" test "$damaged_exit" -ne 0
check "API is running again after the refused restore" wait_health
check "the refused restore left the newer run in place" test "$(http GET "/api/runs/$after_id" -H "$AUTH")" = 200
make --no-print-directory restore "${BACKUPVARS[@]}" BACKUP_FILE="$backup_file" >/dev/null
check "API is back after the restore" wait_health
check "restored data holds the runs from the backup" test "$(http GET "/api/runs/$live_id" -H "$AUTH")" = 200
check "restored data no longer holds the run made after the backup" \
  test "$(http GET "/api/runs/$after_id" -H "$AUTH")" = 404
"${LIVE[@]}" down --volumes >/dev/null 2>&1
API=$API_DIRECT

# --- optional upgrade from an older commit -------------------------------------------------
if [ -n "${SMOKE_UPGRADE_FROM:-}" ]; then
  stage "upgrade from ${SMOKE_UPGRADE_FROM}"
  OLD="$WORK/old"
  git clone --quiet --no-hardlinks "$SRC" "$OLD"
  git -C "$OLD" checkout --quiet --detach "$(git -C "$SRC" rev-parse --verify "$SMOKE_UPGRADE_FROM^{commit}")"
  if [ ! -f "$OLD/backend/Dockerfile" ]; then
    fail "upgrade source ${SMOKE_UPGRADE_FROM} has no backend/Dockerfile (it predates the Docker stack)"
  else
    docker build --quiet -f "$OLD/backend/Dockerfile" -t "$P-old-api" "$OLD" >/dev/null
    inside() {  # inside METHOD PATH [JSON] -> "status body" via Python inside the container
      docker exec "$upgrade_container" python -c 'import json,sys,urllib.request as u
m,p=sys.argv[1],sys.argv[2]; data=sys.argv[3].encode() if len(sys.argv)>3 else None
r=u.Request("http://127.0.0.1:2200"+p,data=data,method=m,headers={"Content-Type":"application/json"})
try: resp=u.urlopen(r,timeout=10); print(resp.status, resp.read().decode())
except u.HTTPError as e: print(e.code, e.read().decode())' "$@"
    }
    wait_inside() { for i in $(seq 1 60); do inside GET /health 2>/dev/null | grep -q '^200' && return 0; sleep 2; done; return 1; }
    docker volume create "$upgrade_volume" >/dev/null
    docker run -d --name "$upgrade_container" --network none -e DATA_DIR=/data -v "$upgrade_volume:/data" "$P-old-api" >/dev/null
    wait_inside
    inside POST /api/benchmarks/runs "$(benchmark_body 'Smoke upgrade case' "$STORY")" | cut -d' ' -f2- > "$WORK/body"
    old_id=$(json 'd["id"]')
    sleep 5
    docker rm -f "$upgrade_container" >/dev/null
    docker run -d --name "$upgrade_container" --network none -e DATA_DIR=/data -v "$upgrade_volume:/data" "$SMOKE_API_IMAGE" >/dev/null
    check "current image starts on the older volume" wait_inside
    inside GET "/api/runs/$old_id" | cut -d' ' -f2- > "$WORK/body"
    check "run created by the older image is still there" test "$(json 'd["id"]')" = "$old_id"
    versions=$(docker exec "$upgrade_container" python -c 'import sqlite3
from audit.db import SCHEMA_VERSION
db = sqlite3.connect("file:/data/audit.sqlite3?mode=ro", uri=True)
print(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)')
    check "older volume migrated to the current schema (database, code: $versions)" \
      test "${versions% *}" = "${versions#* }"
    for path in /api/prompts /api/models /api/articles; do
      check "GET $path works on the migrated volume" test "$(inside GET "$path" | cut -d' ' -f1)" = 200
    done
    docker rm -f "$upgrade_container" >/dev/null
  fi
fi

# --- result --------------------------------------------------------------------------------
stage "result"
if [ "$FAILED" -eq 0 ]; then
  log "SMOKE PASSED for ${SHA:0:12}"
else
  log "SMOKE FAILED: $FAILED check(s) for ${SHA:0:12}"
  exit 1
fi
