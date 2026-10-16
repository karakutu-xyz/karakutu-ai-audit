"""HTTP-layer hardening: correlation ids, and not handing internals to the caller.

`/api/audit` is unauthenticated and fetches caller-supplied URLs, so anything the error path
returns is effectively public. These tests pin the two properties that follow from that.
"""
import pytest
from fastapi import FastAPI

from src.api.app import CORRELATION_ID, correlation_id_middleware, general_exception_handler


# --- correlation id -----------------------------------------------------------


async def test_every_response_carries_a_correlation_id(client):
    resp = await client.get("/health")

    assert resp.status_code == 200
    cid = resp.headers.get("X-Correlation-ID")
    assert cid, "no X-Correlation-ID header on the response"
    assert len(cid) >= 8


async def test_a_caller_supplied_correlation_id_is_honoured(client):
    """A caller stitching its own trace to ours must get its id back, not a new one."""
    resp = await client.get("/health", headers={"X-Correlation-ID": "caller-trace-123"})

    assert resp.headers["X-Correlation-ID"] == "caller-trace-123"


async def test_two_requests_get_different_correlation_ids(client):
    a = (await client.get("/health")).headers["X-Correlation-ID"]
    b = (await client.get("/health")).headers["X-Correlation-ID"]

    assert a != b


async def test_a_hostile_correlation_id_is_sanitised(client):
    """The id lands in log lines, so control characters (log injection / forged lines) and
    unbounded length must not survive."""
    hostile = "abc\r\n2026-01-01 FAKE LOG LINE\x00" + "x" * 500
    resp = await client.get("/health", headers={"X-Correlation-ID": hostile})

    cid = resp.headers["X-Correlation-ID"]
    assert "\n" not in cid and "\r" not in cid and "\x00" not in cid
    assert len(cid) <= 64


async def test_the_correlation_id_is_bound_during_the_request():
    """The contextvar must actually be set while the handler runs — that is what puts the id
    on every log line the audit emits.

    Uses a throwaway app: registering a probe route on the shared `app` leaks into
    `test_contract_alignment.py::test_paths_match`, which asserts the exact path set.
    """
    from httpx import ASGITransport, AsyncClient

    seen = {}
    probe = FastAPI()
    probe.middleware("http")(correlation_id_middleware)

    @probe.get("/_probe")
    async def _probe_route():
        seen["cid"] = CORRELATION_ID.get()
        return {"ok": True}

    transport = ASGITransport(app=probe)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.get("/_probe", headers={"X-Correlation-ID": "bound-check"})

    assert seen["cid"] == "bound-check"


def test_the_context_is_reset_after_the_request():
    """Outside a request the default must be restored, or ids leak between tasks."""
    assert CORRELATION_ID.get() == "-"


# --- the 500 body -------------------------------------------------------------


async def test_an_unhandled_error_does_not_return_the_exception_text():
    """`detail=str(exc)` used to be returned here. On this service that leaks upstream URLs,
    internal hostnames, filesystem paths and raw SDK error bodies to an unauthenticated
    caller. Only the correlation id may cross the boundary."""
    from httpx import ASGITransport, AsyncClient

    probe = FastAPI()
    probe.middleware("http")(correlation_id_middleware)
    probe.add_exception_handler(Exception, general_exception_handler)

    secret = "postgres://user:hunter2@internal-db.svc.cluster.local:5432/archive"

    @probe.get("/boom")
    async def _boom():
        raise RuntimeError(secret)

    transport = ASGITransport(app=probe, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/boom")

    assert resp.status_code == 500
    body = resp.json()
    assert secret not in resp.text
    assert "hunter2" not in resp.text
    assert "RuntimeError" not in resp.text
    assert body["error"] == "Internal server error"
    # The operator still needs a handle to find the traceback in the logs.
    assert "Correlation id:" in body["detail"]
    assert resp.headers.get("X-Correlation-ID")


@pytest.mark.parametrize("path", ["/health", "/docs"])
async def test_normal_responses_are_unaffected(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200
