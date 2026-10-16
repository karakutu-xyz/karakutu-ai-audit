"""CLI ⇄ endpoint parity: the same run record through `scripts/open_audit/apply_results.py` (the
operator's CLI, executed as a script) and through `POST /api/v1/articles/{id}/audit` + the job
worker must send the SAME PATCH payload, the SAME POST text, and leave the SAME ledger entry
(timestamps aside).

Both sides run against their own FakeArchive holding the same open audit; the comment date is frozen.
"""
import asyncio
import json
import runpy
import sys
from pathlib import Path

import dotenv
import pytest
from httpx import ASGITransport, AsyncClient

import src.clients
from src.api.app import app
from src.archive_flow import comments, runner
from src.jobs import models as M
from src.jobs.store import MemoryJobStore
from src.jobs.worker import JobWorker
from tests._service_support import (
    AUTH, INCONCLUSIVE_RELATED, LIKELY_MOVED, FakeArchive, art, fake_runtime, open_audit, record_by, response_for,
    service_settings,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "open_audit" / "apply_results.py"


def _strip_at(entry):
    return {k: {kk: vv for kk, vv in v.items() if kk != "at"} if isinstance(v, dict) else v
            for k, v in entry.items()}


def _run_worker(monkeypatch, aid, source, audit_kw):
    async def fake_run_audit(request, **kw):
        return response_for(record_by(source))
    monkeypatch.setattr(runner, "run_audit", fake_run_audit)
    archive = FakeArchive([open_audit(aid, **audit_kw)])
    cfg = service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    store = MemoryJobStore()

    async def go():
        worker = JobWorker(store, fake_runtime(archive), cfg)
        saved = {k: getattr(app.state, k, None) for k in ("job_store", "job_worker", "intake_lock")}
        app.state.job_store, app.state.job_worker, app.state.intake_lock = store, None, None
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(f"/api/v1/articles/{art(aid)}/audit", headers=AUTH)
        finally:
            for k, v in saved.items():
                setattr(app.state, k, v)
        assert r.status_code == 202 and r.json()["dry_run"] is False
        job = await worker.process(await store.claim_next(60))
        return job, await store.get_ledger(aid)
    job, ledger = asyncio.run(go())
    return archive, job, ledger


def _run_cli(monkeypatch, tmp_path, record, audit_kw):
    archive = FakeArchive([open_audit(record["audit_id"], **audit_kw)])
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"results": [record]}, ensure_ascii=False))
    monkeypatch.setattr(src.clients, "get_client", lambda: archive)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(results), "--template", "C", "--tags", "--comments",
                                      "--apply"])
    runpy.run_path(str(SCRIPT), run_name="__main__")
    log = json.loads((tmp_path / "results-apply_log.json").read_text())
    return archive, log[str(record["audit_id"])]


@pytest.mark.parametrize("source,audit_kw", [
    (INCONCLUSIVE_RELATED, {}),                                       # tags written → comment says "set"
    (LIKELY_MOVED, {"subject_tag_notations": ["medtop:01000000"]}),   # human tags kept → PROPOSED ONLY
])
def test_cli_and_endpoint_send_the_same_writes_and_ledger(monkeypatch, tmp_path, source, audit_kw):
    monkeypatch.setattr(comments, "_stamp", lambda: "2026-10-05")
    aid = source
    w_archive, job, w_ledger = _run_worker(monkeypatch, aid, source, audit_kw)
    assert job.outcome == M.APPLIED
    record = job.data["record"]
    c_archive, c_ledger = _run_cli(monkeypatch, tmp_path, record, audit_kw)

    assert w_archive.writes() == c_archive.writes(), "same PATCH payload and same POST text"
    kinds = ("GET", "COUNT", "PATCH", "POST")
    cli_calls = [c for c in c_archive.calls if c[0] in kinds]
    worker_calls = [c for c in w_archive.calls if c[0] in kinds]
    # The worker's read-only pre-check (one discussion count) comes first; the apply phase after
    # it makes exactly the CLI's calls, in the CLI's order.
    assert worker_calls[-len(cli_calls):] == cli_calls
    assert _strip_at(w_ledger) == _strip_at(c_ledger)
    posted = [c for c in w_archive.writes() if c[0] == "POST"]
    assert len(posted) == 1 and posted[0][2].startswith(comments.BOT_PREFIX)
