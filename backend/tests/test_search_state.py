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


@pytest.mark.parametrize('hits', [[], [
    {'url': '/fictional/relative'}, {'url': 'ftp://files.example/fictional'},
]])
async def test_unresponsive_engines_without_usable_results_fail(hits):
    search = Search('https://search.example', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            'results': hits, 'unresponsive_engines': [
                [' brave ', 'fabricated private rate-limit reason'],
                ['duckduckgo', 'fabricated private CAPTCHA reason'],
            ],
        })))
    events = []
    context = trace.set(events)
    try:
        assert await search.search_checked('fictional') == ([], True)
    finally:
        trace.reset(context)
    assert search.state == 'degraded'
    assert events[0]['error_type'] == 'EnginesUnresponsive'
    assert events[0]['unresponsive_engines'] == ['brave', 'duckduckgo']
    assert events[0]['results'] == []
    assert 'fabricated private' not in str(events)


@pytest.mark.parametrize('engines', [
    None, [], 'fabricated private reason', {'brave': 'fabricated private reason'},
    [1, None, {}, '', '  ', 'fabricated private reason', [], ['brave'],
     ['brave', 'reason', 'extra']],
])
async def test_empty_search_ignores_absent_or_malformed_unresponsive_engines(engines):
    payload = {'results': []}
    if engines is not None:
        payload['unresponsive_engines'] = engines
    search = Search('https://search.example', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    events = []
    context = trace.set(events)
    try:
        assert await search.search_checked('fictional') == ([], False)
    finally:
        trace.reset(context)
    assert search.state == 'ok'
    assert events[0]['error_type'] is None
    assert 'unresponsive_engines' not in events[0]
    assert 'fabricated private' not in str(events)


async def test_usable_results_record_only_bounded_engine_names():
    engines = [[' brave ', 'fabricated private reason'], ('duckduckgo', 'fabricated private reason'),
               'fabricated private bare string', ['x' * 40, 'fabricated private reason'],
               ['Fabricated private reason: blocked!', 'r'],
               ['fabricated private reason text here', 'r']]
    engines.extend([f'engine{i}', 'fabricated private reason'] for i in range(25))
    search = Search('https://search.example', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            'results': [{'url': 'https://paper.example/fictional'}],
            'unresponsive_engines': engines,
        })))
    events = []
    context = trace.set(events)
    try:
        results, failed = await search.search_checked('fictional')
    finally:
        trace.reset(context)
    assert not failed and search.state == 'ok'
    assert [hit.url for hit in results] == ['https://paper.example/fictional']
    assert events[0]['error_type'] is None
    assert events[0]['unresponsive_engines'] == (
        ['brave', 'duckduckgo'] + [f'engine{i}' for i in range(18)])
    assert 'fabricated private' not in str(events)


@pytest.mark.parametrize('name', [
    'Fabricated private reason: blocked!', 'fabricated private reason text here', '', '  ', None,
])
async def test_unrecognised_engine_names_still_fail_but_are_not_kept(name):
    search = Search('https://search.example', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            'results': [], 'unresponsive_engines': [[name, 'fabricated private reason']],
        })))
    events = []
    context = trace.set(events)
    try:
        assert await search.search_checked('fictional') == ([], True)
    finally:
        trace.reset(context)
    assert search.state == 'degraded'
    assert events[0]['error_type'] == 'EnginesUnresponsive'
    assert 'unresponsive_engines' not in events[0]
    assert 'fabricated private' not in str(events)


@pytest.mark.parametrize('hits', [[], [{'url': 'https://paper.example/fictional'}]])
async def test_degraded_search_recovers_on_a_successful_query(hits):
    payload = {'results': [], 'unresponsive_engines': [['brave', 'fabricated reason']]}
    search = Search('https://search.example', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    assert await search.search_checked('fictional') == ([], True)
    assert search.state == 'degraded'
    payload = {'results': hits}
    assert not (await search.search_checked('fictional'))[1]
    assert search.state == 'ok'
    assert await search.probe() == 'ok'


async def test_probe_and_transport_failure_preserve_degraded_search():
    available = True

    def route(request):
        if not available:
            raise httpx.ConnectError('fabricated private reason', request=request)
        return httpx.Response(200, json={
            'results': [], 'unresponsive_engines': [['brave', 'fabricated private reason']],
        })

    search = Search('https://search.example', transport=httpx.MockTransport(route))
    assert await search.probe() == 'ok'
    assert await search.search_checked('fictional') == ([], True)
    search._probe_at -= 31
    assert await search.probe() == 'degraded'
    available = False
    assert await search.search_checked('fictional') == ([], True)
    assert search.state == 'unreachable'
    search._probe_at -= 31
    assert await search.probe() == 'unreachable'
    available = True
    search._probe_at -= 31
    assert await search.probe() == 'degraded'


async def test_degraded_answer_clears_remembered_rejection():
    status = 403
    search = Search('https://search.example', transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={
            'results': [], 'unresponsive_engines': [['brave', 'fabricated private reason']],
        })))
    assert await search.search_checked('fictional') == ([], True)
    assert search.state == 'rejected'
    status = 200
    assert await search.search_checked('fictional') == ([], True)
    assert search.state == 'degraded'
    assert await search.probe() == 'degraded'


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


@pytest.mark.parametrize('degraded', [False, True])
async def test_concurrent_probes_coalesce_and_new_search_owns_state(degraded):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def route(request):
        calls.append(request.url.path)
        if request.url.path == '/healthz':
            started.set()
            await release.wait()
            raise httpx.ConnectError('fabricated upstream text', request=request)
        return httpx.Response(200, json={
            'results': [],
            'unresponsive_engines': [['brave', 'fabricated reason']] if degraded else [],
        })

    search = Search('https://search.example', transport=httpx.MockTransport(route))
    first = asyncio.create_task(search.probe())
    await started.wait()
    second = asyncio.create_task(search.probe())
    await search.search_checked('fictional')
    release.set()
    expected = 'degraded' if degraded else 'ok'
    assert await asyncio.gather(first, second) == [expected, expected]
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


async def test_status_preserves_real_search_degradation(tmp_path):
    engine = FakeEngine()
    engine.search = Search('https://search.example', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            'results': [], 'unresponsive_engines': [['brave', 'fabricated private reason']],
        })))
    assert await engine.search.search_checked('fictional') == ([], True)
    with TestClient(create_app(settings(tmp_path, search_provider='searxng'), engine)) as client:
        response = client.get('/api/status')
        assert response.status_code == 200
        assert response.json()['search'] == 'degraded'
        assert 'search.example' not in response.text
        assert 'fabricated private reason' not in response.text
