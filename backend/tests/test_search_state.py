"""Search health uses bounded fake HTTP; status exposes only fixed labels."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from audit.api import create_app
from audit.engine import Search
from audit.model import trace
from test_api import FakeEngine, settings


@pytest.mark.parametrize('outcome, expected', [
    ('ok', 'ok'), ('connect', 'unreachable'), ('timeout', 'unreachable'),
    ('transport', 'unreachable'), ('forbidden', 'rejected'), ('server', 'rejected'),
    ('invalid_json', 'rejected'), ('error_object', 'rejected'), ('results_not_list', 'rejected'),
])
async def test_search_checked_outcomes_and_recovery(outcome, expected):
    current = outcome

    def route(request):
        errors = {'connect': httpx.ConnectError, 'timeout': httpx.ReadTimeout,
                  'transport': httpx.RemoteProtocolError}
        if current in errors:
            raise errors[current]('fabricated private upstream text', request=request)
        if current == 'invalid_json':
            return httpx.Response(200, text='fabricated private upstream text')
        if current == 'error_object':  # a 200 without a results list is a failed query
            return httpx.Response(200, json={'error': 'fabricated private upstream text'})
        if current == 'results_not_list':
            return httpx.Response(200, json={'results': 'fabricated private upstream text'})
        return httpx.Response({'forbidden': 403, 'server': 503}.get(current, 200),
                             json={'results': []})

    search = Search('https://search.example/private', transport=httpx.MockTransport(route))
    assert search.state == 'unchecked'
    events = []
    context = trace.set(events)
    try:
        assert await search.search_checked('fictional query') == ([], outcome != 'ok')
        assert search.state == expected
        assert 'fabricated private upstream text' not in str(events)
        current = 'ok'
        assert await search.search('fictional query') == []
        assert search.state == 'ok'
    finally:
        trace.reset(context)


@pytest.mark.parametrize('answer, expected', [(200, 'ok'), (403, 'ok'), (404, 'ok'),
                                             (503, 'ok'), (None, 'unreachable'),
                                             ('timeout', 'unreachable')])
def test_status_probe_is_cached_and_safe(tmp_path, monkeypatch, answer, expected):
    calls = []
    now = [0.0]
    monkeypatch.setattr('audit.engine.time', SimpleNamespace(monotonic=lambda: now[0]))

    def route(request):
        calls.append(request)
        assert request.url.path == '/private/healthz'
        assert request.extensions['timeout']['connect'] == 2
        if answer is None:
            raise httpx.ConnectError('fabricated private upstream text', request=request)
        if answer == 'timeout':
            raise httpx.ReadTimeout('fabricated private upstream text', request=request)
        return httpx.Response(answer, text='fabricated private upstream text')

    engine = FakeEngine()
    engine.search = Search('https://search.example/private', transport=httpx.MockTransport(route))
    with TestClient(create_app(settings(tmp_path, search_provider='searxng'), engine)) as client:
        for _ in range(3):
            response = client.get('/api/status')
            assert response.status_code == 200
            assert response.json()['search'] == expected
            assert response.json()['search_provider'] == 'searxng'
            assert 'search.example' not in response.text
            assert 'private' not in response.text
        assert len(calls) == 1
        now[0] += 31
        assert client.get('/api/status').json()['search'] == expected
        assert len(calls) == 2


async def test_probe_keeps_rejection_until_a_real_search_succeeds():
    status = 403
    available = True

    def route(request):
        if not available:
            raise httpx.ConnectError('fabricated upstream text', request=request)
        return httpx.Response(200 if request.url.path == '/healthz' else status,
                             json={'results': []})

    search = Search('https://search.example', transport=httpx.MockTransport(route))
    assert (await search.search_checked('fictional'))[1]
    assert await search.probe() == 'rejected'
    available = False
    search._probe_at = None
    assert await search.probe() == 'unreachable'
    available = True
    search._probe_at = None
    assert await search.probe() == 'rejected'
    # A real query that cannot connect does not clear the remembered rejection either.
    available = False
    assert (await search.search_checked('fictional'))[1]
    assert search.state == 'unreachable'
    available = True
    search._probe_at = None
    assert await search.probe() == 'rejected'
    status = 200
    assert await search.search_checked('fictional') == ([], False)
    assert await search.probe() == 'ok'


async def test_concurrent_probes_coalesce_and_new_search_owns_state():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def route(request):
        calls.append(request.url.path)
        if request.url.path == '/healthz':
            started.set()
            await release.wait()
            raise httpx.ConnectError('fabricated upstream text', request=request)
        return httpx.Response(200, json={'results': []})

    search = Search('https://search.example', transport=httpx.MockTransport(route))
    first = asyncio.create_task(search.probe())
    await started.wait()
    second = asyncio.create_task(search.probe())
    await search.search_checked('fictional')
    release.set()
    assert await asyncio.gather(first, second) == ['ok', 'ok']
    assert calls.count('/healthz') == 1


async def test_disabled_search_and_test_mode_never_probe(tmp_path):
    def forbidden(_request):
        pytest.fail('Offline search must not make HTTP calls')

    transport = httpx.MockTransport(forbidden)
    disabled = Search('', transport=transport)
    assert disabled.state == 'disabled'
    assert await disabled.probe() == 'disabled'
    assert await disabled.search_checked('fictional') == ([], False)
    engine = FakeEngine()
    engine.search = Search('https://search.example', transport=transport)
    with TestClient(create_app(settings(tmp_path, mode='test'), engine)) as client:
        assert client.get('/api/status').json()['search'] == 'disabled'
    assert engine.search.state == 'unchecked'


async def test_status_preserves_real_search_rejection(tmp_path):
    engine = FakeEngine()
    engine.search = Search('https://search.example', transport=httpx.MockTransport(
        lambda request: httpx.Response(403, text='fabricated private upstream text')))
    await engine.search.search('fictional')
    with TestClient(create_app(settings(tmp_path, search_provider='searxng'), engine)) as client:
        response = client.get('/api/status')
        assert response.json()['search'] == 'rejected'
        assert 'search.example' not in response.text
        assert 'fabricated private upstream text' not in response.text
