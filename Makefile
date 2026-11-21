# One command per run mode. Both serve the UI at http://localhost:2201 and the API at
# http://localhost:2200, so starting one mode stops the other first.
# PROJECT, TEST_PROJECT and the KARAKUTU_*_PORT variables can be overridden, so a second
# stack (for example the smoke suite) never shares containers, volumes or ports with yours.
COMPOSE := docker compose
PROJECT ?= karakutu-ai-audit
TEST_PROJECT ?= $(PROJECT)-test
WEB_PORT := $(or $(KARAKUTU_WEB_PORT),2201)
LIVE_FILES ?= -f compose.yaml
LIVE := $(COMPOSE) -p $(PROJECT) $(LIVE_FILES)
TEST := $(COMPOSE) -p $(TEST_PROJECT) -f compose.yaml -f compose.test.yaml
# The live data volume and the API image that backup and restore run in (Compose's default names).
VOLUME ?= $(PROJECT)_karakutu-audit-data
API_IMAGE ?= $(PROJECT)-karakutu-audit-api
STAMP := $(shell date -u +%Y%m%dT%H%M%SZ)

.PHONY: env dev test live down test-reset logs smoke backup restore test-postgres storage-sqlite

# Mock data only: offline demo engine, fake integrations, its own database and volume.
test:
	$(LIVE) down
	$(TEST) up --build -d --wait karakutu-audit-api karakutu-audit-web
	@echo "TEST mode (demo data) at http://localhost:$(WEB_PORT)"

# Real model, search and read-only archive from .env. No write-back to karakutu.
live:
	@test -f .env || { echo "live mode needs .env; copy .env.example and fill it"; exit 1; }
	$(TEST) down
	$(LIVE) up --build -d --wait
	@echo "LIVE mode at http://localhost:$(WEB_PORT)"

down:
	$(TEST) down
	$(LIVE) down

# Drop the test database volume; the next `make test` reseeds the demo data.
test-reset:
	$(TEST) down --volumes

logs:
	$(LIVE) logs -f karakutu-audit-api

# Inspect the effective database setting before SQLite volume operations.
storage-sqlite:
	@$(LIVE) run --rm --no-deps -T --entrypoint python karakutu-audit-api -c \
	  'import os,sys; from audit.config import database_backend; url=os.environ.get("DATABASE_URL", ""); sys.exit("backup/restore supports SQLite only; use PostgreSQL provider backups" if url and database_backend(url).get_backend_name() != "sqlite" else 0)'

# Copy the live data volume into BACKUP_DIR while the API is stopped, then start it again.
# Paths are resolved by the shell (not Make's abspath), so they may contain spaces.
backup: storage-sqlite
	@test -n "$(BACKUP_DIR)" -a -d "$(BACKUP_DIR)" || { echo "set BACKUP_DIR to an existing directory"; exit 1; }
	$(LIVE) stop karakutu-audit-api
	dir="$$(cd "$(BACKUP_DIR)" && pwd)" \
	  && docker run --rm --network none --user "$$(id -u):$$(id -g)" --entrypoint tar \
	       -v "$(VOLUME)":/data:ro -v "$$dir":/backup \
	       "$(API_IMAGE)" -czf "/backup/karakutu-audit-data-$(STAMP).tar.gz" --exclude=./.pre-restore -C /data . \
	  && echo "backup written to $$dir/karakutu-audit-data-$(STAMP).tar.gz" \
	  || { $(LIVE) up -d --wait karakutu-audit-api; echo "backup failed; the API is running again"; exit 1; }
	$(LIVE) up -d --wait karakutu-audit-api

# Replace the live data volume with BACKUP_FILE. The archive and its database are checked first,
# so a damaged backup changes nothing (scripts/volume_restore.py). START=0 leaves the API stopped
# afterwards (used for a rollback to an older version).
START ?= 1
restore: storage-sqlite
	@test -f "$(BACKUP_FILE)" || { echo "set BACKUP_FILE to a backup archive"; exit 1; }
	$(LIVE) stop karakutu-audit-api
	file="$$(cd "$$(dirname "$(BACKUP_FILE)")" && pwd)/$$(basename "$(BACKUP_FILE)")" \
	  && docker run --rm --network none --entrypoint python \
	       -v "$(VOLUME)":/data -v "$$file":/backup.tar.gz:ro \
	       -v "$(CURDIR)/scripts/volume_restore.py":/restore/volume_restore.py:ro \
	       "$(API_IMAGE)" /restore/volume_restore.py /backup.tar.gz /data \
	  || { if [ "$(START)" = 1 ]; then $(LIVE) up -d --wait karakutu-audit-api; fi; \
	       echo "restore refused or rolled back; the data is unchanged"; exit 1; }
	@if [ "$(START)" = 1 ]; then $(LIVE) up -d --wait karakutu-audit-api; \
	 else echo "data restored; the API is left stopped (START=0)"; fi

# End-to-end Docker check from a fresh clone of the committed HEAD. Offline after the image
# builds, own project names, ports and volumes; cleans up after itself. See docs/deploy-docker.md.
smoke:
	scripts/docker_smoke.sh
	$(MAKE) test-postgres

# Starts only a throwaway local database; no application server.
test-postgres:
	bash scripts/test_postgres.sh

# Create settings once; API_TOKEN=1 opts into generating a bearer token.
API_TOKEN ?= 0
env:
	python3 scripts/setup.py $(if $(filter 1,$(API_TOKEN)),--api-token,)

# Local processes; RUN_MODE=live make dev selects live without editing the file.
dev:
	scripts/run-local.sh
