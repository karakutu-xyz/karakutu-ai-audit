#!/usr/bin/env bash
# Isolated PostgreSQL storage checks. Never source operator settings or print secrets.
set -euo pipefail
set +x
cd "$(dirname "$0")/.."
password=$(openssl rand -hex 16)
container="audit-postgres-test-$(openssl rand -hex 8)"
container_id=""
cleanup() {
  if [ -n "$container_id" ]; then
    docker rm -f "$container_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Ask the kernel for a free loopback port; exclude application and tooling ports.
port=$(python3 - <<'PY'
import socket
while True:
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        port = connection.getsockname()[1]
    if port not in {2200, 2201, 2202, 2230, 2231, 10350}:
        print(port)
        break
PY
)
# Environment forwarding keeps the password out of the docker command's argv/output.
container_id=$(POSTGRES_PASSWORD="$password" docker run --rm -d --name "$container" \
  -e POSTGRES_PASSWORD -e POSTGRES_USER=audit -e POSTGRES_DB=audit \
  -p "127.0.0.1:$port:5432" postgres:17-alpine)
ready=false
for _ in $(seq 1 60); do
  if docker exec "$container" pg_isready -U audit -d audit >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [ "$ready" != true ]; then
  echo "PostgreSQL did not become ready" >&2
  exit 1
fi
TEST_POSTGRES_URL="postgresql+psycopg://audit:$password@127.0.0.1:$port/audit" \
  uv run pytest -q -m "postgres or not postgres" \
  backend/tests/test_db.py backend/tests/test_api.py backend/tests/test_articles.py \
  backend/tests/test_postgres_storage.py backend/tests/test_migrate_sqlite_to_postgres.py
