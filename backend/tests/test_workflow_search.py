"""Query failures change descriptions, never evidence verdicts or lead standing."""
from types import SimpleNamespace

import httpx
import pytest

from audit.domain import Article, Verdict
from audit.engine import Search
from audit.search.base import BaseSearchProvider, SearchResult
from audit.search.fetch import FetchOutcome, LivePage
from audit.workflow import pipeline, web_search


def article():
    return Article(url='https://paper.example/fictional', title='Imagined report',
                   content=' '.join(f'fictionalword{i}' for i in range(100)))


def hit(source, name):
    return SearchResult(url=f'https://{name}.example/copy', title='Fictional copy',
                        snippet='Invented evidence', source=source)


@pytest.mark.parametrize('failures', [0, 1, 3])
async def test_query_failure_counts_and_explanations(monkeypatch, failures):
    calls = []
    web_hit = hit('web', 'web')
    leads = [hit('archive', 'archive'), hit('cited', 'cited')]

    def route(request):
        calls.append(request)
        if len(calls) <= failures:
            raise httpx.ConnectError('fabricated upstream text', request=request)
        return httpx.Response(200, json={'results': [
            {'url': web_hit.url, 'title': web_hit.title, 'content': web_hit.snippet}]})

    async def fetch(hits, **_kwargs):
        return FetchOutcome(live=[LivePage(url=h.url, final_url=h.url, status=200,
                                          title=h.title, text='Invented unrelated page')
                                  for h in hits])

    async def judge(*_args, **_kwargs):
        return SimpleNamespace(data=web_search.WebSearchResult(reasoning='No same-story match.'))

    monkeypatch.setattr(web_search, 'fetch_candidates', fetch)
    search = Search('https://search.example', transport=httpx.MockTransport(route))
    evidence = await web_search.run(article(), search, SimpleNamespace(run=judge), extra_hits=leads)
    assert len(calls) == evidence.search_queries == 3
    assert evidence.search_queries_failed == failures
    assert evidence.archive_candidates == evidence.cited_source_leads == 1
    assert evidence.web_hits == (0 if failures == 3 else 1)
    assert evidence.hits_searched == evidence.web_hits + 2
    verdict, findings = pipeline._decide(evidence)
    assert verdict == Verdict.INCONCLUSIVE
    assert pipeline._decide(evidence.model_copy(update={'search_queries_failed': 0}))[0] == verdict
    counts = (f'Read {evidence.live_pages_read} live page(s) from 1 archive candidate(s), '
              f'1 cited-source lead(s) and {evidence.web_hits} search hit(s)')
    assert counts in evidence.reasoning
    assert counts in findings
    note = web_search.search_failure_note(evidence)
    assert evidence.reasoning.startswith(note) and findings.startswith(note)
    if failures == 3:
        assert note == ('Web search failed: all 3 search queries failed, so the web was not '
                        'searched. Only archive candidates (1) and cited-source leads (1) '
                        'were available. ')
    elif failures:
        assert note == '1 of 3 search queries failed. '
    else:
        assert not note
    details = pipeline._web_details(evidence)
    for key in ('search_queries', 'search_queries_failed', 'web_hits',
                'archive_candidates', 'cited_source_leads', 'hits_searched'):
        assert details[key] == getattr(evidence, key)


async def test_failure_without_candidates_does_not_call_model():
    def route(request):
        return httpx.Response(403, text='fabricated upstream text')

    async def forbidden(*_args, **_kwargs):
        pytest.fail('No pages to judge')

    evidence = await web_search.run(article(), Search('https://search.example',
        transport=httpx.MockTransport(route)), SimpleNamespace(run=forbidden))
    assert evidence.search_queries == evidence.search_queries_failed == 3
    assert evidence.hits_searched == evidence.web_hits == 0
    assert 'so the web was not searched' in evidence.reasoning
    verdict, findings = pipeline._decide(evidence)
    assert verdict == Verdict.INCONCLUSIVE
    assert findings.startswith(web_search.search_failure_note(evidence))


async def test_unresponsive_engines_use_existing_search_failure_note():
    calls = []

    def route(request):
        calls.append(request)
        return httpx.Response(200, json={
            'results': [], 'unresponsive_engines': [
                ['brave', 'fabricated private rate-limit reason'],
                ['duckduckgo', 'fabricated private CAPTCHA reason'],
            ],
        })

    async def forbidden(*_args, **_kwargs):
        pytest.fail('No pages to judge')

    search = Search('https://search.example', transport=httpx.MockTransport(route))
    evidence = await web_search.run(article(), search, SimpleNamespace(run=forbidden))
    assert len(calls) == evidence.search_queries == evidence.search_queries_failed == 3
    assert search.state == 'degraded'
    assert evidence.web_hits == evidence.hits_searched == 0
    assert evidence.reasoning.startswith(
        'Web search failed: all 3 search queries failed, so the web was not searched. ')
    assert evidence.reasoning.startswith(web_search.search_failure_note(evidence))
    assert 'fabricated private' not in evidence.reasoning


async def test_default_checked_provider_and_independent_archive_upgrade():
    lead = hit('archive', 'archive')

    class Provider(BaseSearchProvider):
        async def search(self, query, max_results=5, *, language=None):
            return [lead.model_copy(update={'source': 'web'})]

    provider = Provider()
    assert await provider.search_checked('fictional') == (
        [lead.model_copy(update={'source': 'web'})], False)
    hits, queries, failed = await web_search._gather_hits(article(), provider, [lead])
    assert queries == 3 and failed == 0
    assert len(hits) == 1 and hits[0].source == 'web'


async def test_disabled_provider_does_not_count_queries():
    hits, queries, failed = await web_search._gather_hits(article(), Search(''), [])
    assert (hits, queries, failed) == ([], 0, 0)


@pytest.mark.parametrize('evidence, verdict', [
    ({'matches': [hit('web', 'web')]}, Verdict.FOUND_ONLINE),
    ({'relocated_to': hit('web', 'paper')}, Verdict.LIKELY_MOVED),
])
def test_failure_prefix_also_precedes_positive_verdicts(evidence, verdict):
    web = web_search.WebSearchEvidence(search_queries=3, search_queries_failed=3, **evidence)
    actual, findings = pipeline._decide(web)
    assert actual == verdict
    assert findings.startswith(web_search.search_failure_note(web))


async def test_model_failure_keeps_search_diagnostics(monkeypatch):
    def route(request):
        raise httpx.ConnectError('fabricated upstream text', request=request)

    async def fetch(hits, **_kwargs):
        return FetchOutcome(live=[LivePage(url=h.url, final_url=h.url, status=200,
                                          title=h.title, text='Invented unrelated page')
                                  for h in hits])

    async def broken(*_args, **_kwargs):
        raise RuntimeError('fabricated model reply')

    monkeypatch.setattr(web_search, 'fetch_candidates', fetch)
    search = Search('https://search.example', transport=httpx.MockTransport(route))
    with pytest.raises(web_search.WebSearchModelFailure) as caught:
        await web_search.run(article(), search, SimpleNamespace(run=broken),
                             extra_hits=[hit('archive', 'archive')])
    evidence = caught.value.evidence
    assert evidence.search_queries == evidence.search_queries_failed == 3
    assert evidence.archive_candidates == 1 and evidence.web_hits == 0
    assert web_search.search_failure_note(evidence).startswith('Web search failed: all 3')
    assert 'fabricated' not in str(caught.value) + evidence.reasoning
