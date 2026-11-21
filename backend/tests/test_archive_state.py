"""Fixed archive states with generated wallets and fake HTTP only."""
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from audit.api import create_app
from audit.clients.karakutu import KarakutuClient
from audit.engine import AuditEngine
from audit.model import trace
from test_api import FakeEngine, settings
from test_workflow_archive_auth import wallet_jwk as generated_wallet, stub_http  # noqa: F401


@pytest.fixture
def wallet_jwk(generated_wallet):  # noqa: F811
    return generated_wallet


@pytest.mark.parametrize('wallet_kind, expected', [
    ('missing', 'wallet_missing'), ('invalid', 'wallet_unreadable'),
    ('unreadable', 'wallet_unreadable'), ('valid', 'unchecked'),
    ('inline', 'unchecked'), ('invalid_inline', 'wallet_unreadable'),
])
async def test_wallet_preflight_and_status(tmp_path, wallet_jwk, wallet_kind, expected,
                                          monkeypatch):
    path = tmp_path / 'fabricated-wallet.json'
    raw = (json.dumps(wallet_jwk) if wallet_kind not in ('invalid', 'invalid_inline')
           else 'fabricated invalid JSON')
    if wallet_kind != 'missing':
        path.write_text(raw)
    if wallet_kind == 'unreadable':
        path.chmod(0)
        if os.access(path, os.R_OK):
            # Privileged test runners bypass mode bits; emulate the non-root API.
            read_text = Path.read_text

            def read_as_api(source, *args, **kwargs):
                if source == path:
                    raise PermissionError
                return read_text(source, *args, **kwargs)

            monkeypatch.setattr(Path, 'read_text', read_as_api)
    try:
        archive = KarakutuClient('https://archive.example', wallet_path=str(path),
                                 wallet_json=raw if wallet_kind in ('inline', 'invalid_inline') else '')
        try:
            assert archive.state == expected
            engine = FakeEngine()
            engine.archive = archive
            with TestClient(create_app(settings(tmp_path, archive_base_url=archive.base_url),
                                       engine)) as client:
                response = client.get('/api/status')
                assert response.json()['archive'] == expected
                assert response.json()['archive_reads_enabled'] is True
                assert str(path) not in response.text
                assert raw not in response.text
        finally:
            await archive.close()
    finally:
        if path.exists():
            path.chmod(0o600)


async def test_disabled_wallet_is_not_opened_and_import_is_compatible(tmp_path):
    archive = KarakutuClient(wallet_path=str(tmp_path / 'missing'))
    try:
        assert archive.state == 'disabled'
        assert await archive._get('/articles') is None
        engine = FakeEngine()
        engine.archive = archive
        with TestClient(create_app(settings(tmp_path), engine)) as client:
            assert client.get('/api/status').json()['archive'] == 'disabled'
            response = client.post('/api/articles/import')
            assert response.status_code == 409
            assert response.json() == {'error': {
                'code': 'archive_unavailable', 'reason': 'not_configured'}}
    finally:
        await archive.close()


@pytest.mark.parametrize('outcome, expected', [
    ('ok', 'ok'), ('network', 'unreachable'), ('server', 'unreachable'),
    ('invalid_json', 'unreachable'), ('unauthorized', 'rejected'),
    ('forbidden', 'rejected'), ('auth', 'rejected'), ('auth_bad', 'auth_unavailable'),
    ('auth_network', 'unreachable'), ('auth_server', 'unreachable'),
])
async def test_call_outcomes_and_recovery(tmp_path, wallet_jwk, monkeypatch, outcome, expected):
    calls = []
    current = outcome

    def route(request):
        calls.append(request.url.path)
        if request.url.path == '/login':
            # An archive that cannot be reached during sign-in is unreachable, not a
            # sign-in failure.
            if current == 'auth_network':
                raise httpx.ConnectError('fabricated private upstream text', request=request)
            if current == 'auth_server':
                return httpx.Response(502, text='fabricated private upstream text')
            return httpx.Response(200, json={'challenge': 'fabricated challenge'})
        if request.url.path == '/authenticate':
            status = {'auth': 403, 'auth_bad': 400}.get(current, 200)
            return httpx.Response(status, json={'access_token': 'fabricated-token'})
        if current == 'network':
            raise httpx.ConnectError('fabricated private upstream text', request=request)
        if current == 'invalid_json':
            return httpx.Response(200, text='fabricated private upstream text')
        status = {'server': 503, 'unauthorized': 401, 'forbidden': 403}.get(current, 200)
        return httpx.Response(status, json={'items': []} if status == 200 else {
            'detail': 'fabricated private upstream text'})

    stub_http(monkeypatch, route)
    archive = KarakutuClient('https://archive.example', wallet_json=json.dumps(wallet_jwk))
    events = []
    context = trace.set(events)
    try:
        assert archive.state == 'unchecked'
        await archive._get('/audits')
        assert archive.state == expected
        if outcome in ('unauthorized', 'forbidden'):
            assert calls.count('/audits') == 2
            assert calls.count('/authenticate') == 2
        assert 'fabricated private upstream text' not in json.dumps(events)
        # A later successful call replaces the previous failure.
        current = 'ok'
        archive.auth._last_mint_attempt = 0
        await archive._get('/audits')
        assert archive.state == 'ok'
    finally:
        trace.reset(context)
        await archive.close()


@pytest.mark.parametrize('failure, reason', [
    ('missing', 'wallet_missing'), ('invalid', 'wallet_unreadable'),
    ('auth', 'rejected'), ('auth_bad', 'auth_unavailable'), ('network', 'unreachable'),
    ('server', 'unreachable'), ('rejected', 'rejected'),
])
async def test_import_reasons_and_silent_lazy_fill(tmp_path, wallet_jwk, monkeypatch,
                                                failure, reason):
    raw = json.dumps(wallet_jwk)
    path = tmp_path / 'fabricated-wallet.json'
    if failure != 'missing':
        path.write_text('invalid' if failure == 'invalid' else raw)

    def route(request):
        if request.url.path == '/login':
            return httpx.Response(200, json={'challenge': 'fabricated challenge'})
        if request.url.path == '/authenticate':
            return httpx.Response({'auth': 403, 'auth_bad': 400}.get(failure, 200),
                                  json={'access_token': 'fabricated-token'})
        if failure == 'network':
            raise httpx.ConnectError('fabricated upstream text', request=request)
        return httpx.Response(403 if failure == 'rejected' else 503,
                              text='fabricated upstream text')

    stub_http(monkeypatch, route)
    archive = KarakutuClient('https://archive.example', wallet_path=str(path))
    engine = FakeEngine()
    engine.archive = archive
    try:
        app = create_app(settings(tmp_path, archive_base_url=archive.base_url), engine)
        with TestClient(app) as client:
            app.state.store.import_articles([
                {'article_id': 42, 'url': 'https://paper.example/fabricated'}])
            # Enrichment failure is silent and still updates archive state.
            response = client.get('/api/articles/ka-42')
            assert response.status_code == 200 and response.json()['content'] == ''
            assert client.get('/api/status').json()['archive'] == reason
            response = client.post('/api/articles/import')
            assert response.status_code == 409
            assert response.json() == {'error': {'code': 'archive_unavailable', 'reason': reason}}
            status = client.get('/api/status').text
            assert str(path) not in status + response.text
            assert raw not in status + response.text
            assert 'fabricated upstream text' not in status + response.text
            assert 'fabricated-token' not in status + response.text
    finally:
        await archive.close()


@pytest.mark.parametrize('http_status, expected', [(200, 'ok'), (503, 'unreachable')])
async def test_engine_start_uses_archive_outcome_without_failing(monkeypatch, http_status, expected):
    stub_http(monkeypatch, lambda request: httpx.Response(http_status, json={'concepts': []}))
    engine = AuditEngine({'archive_base_url': 'https://archive.example'})
    try:
        await engine.start()
        assert engine.archive.state == expected
    finally:
        await engine.close()


async def test_wallet_can_be_repaired_after_startup(tmp_path, wallet_jwk, monkeypatch):
    stub_http(monkeypatch, lambda request: httpx.Response(200, json={
        'challenge': 'fabricated challenge', 'access_token': 'fabricated-token'}))
    path = tmp_path / 'fabricated-wallet.json'
    archive = KarakutuClient('https://archive.example', wallet_path=str(path))
    try:
        assert archive.state == 'wallet_missing'
        path.write_text('invalid')
        await archive._get('/articles')
        assert archive.state == 'wallet_unreadable'
        path.write_text(json.dumps(wallet_jwk))
        await archive._get('/articles')
        assert archive.state == 'ok'
    finally:
        await archive.close()


@pytest.mark.parametrize('retry_outcome', ['network', 'server', 'auth'])
async def test_refused_request_then_failed_refresh_or_retry(monkeypatch, wallet_jwk,
                                                          retry_outcome):
    counts = {'auth': 0, 'read': 0}

    def route(request):
        if request.url.path == '/login':
            return httpx.Response(200, json={'challenge': 'fabricated challenge'})
        if request.url.path == '/authenticate':
            counts['auth'] += 1
            status = 503 if retry_outcome == 'auth' and counts['auth'] == 2 else 200
            return httpx.Response(status, json={'access_token': 'fabricated-token'})
        counts['read'] += 1
        if counts['read'] == 1:
            return httpx.Response(401)
        if retry_outcome == 'network':
            raise httpx.ConnectError('fabricated upstream text', request=request)
        return httpx.Response(503)

    stub_http(monkeypatch, route)
    archive = KarakutuClient('https://archive.example', wallet_json=json.dumps(wallet_jwk))
    try:
        assert await archive._get('/articles') is None
        # A 503 during the re-sign-in means the archive server is down: unreachable too.
        assert archive.state == 'unreachable'
    finally:
        await archive.close()
