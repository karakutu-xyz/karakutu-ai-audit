"""Recording stays bounded while the real graph consumes full invented evidence."""
import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest
from openai import AsyncOpenAI

from audit.clients.concepts import Concept, ConceptCatalog
from audit.config import Settings
from audit.domain import Article, AuditRequest
from audit import step_inputs
from audit.engine import AuditEngine, NAMES
from audit.evidence import archive_output, summary, text_summary
from audit.search import fetch as fetch_module
from audit.workflow.agents import build_stage_agents


ORIGINAL = 'https://publisher.example/reports/7000/reservoir-budget'
CANDIDATE = 'https://publisher.example/reports/7001/pottery-workshop'
SENTINEL = 'The invented violet sundial stores a unique sentinel sentence.'
ANCHOR = 1900000000


def large_article():
    prose = ('The fictional council examined reservoir spending and public water '
             'contracts before approving new storage equipment. ')
    text = prose * 110 + SENTINEL + prose * 1600
    assert 200_000 < len(text) < 250_000
    assert text.index(SENTINEL) > 10_000
    return Article(url=ORIGINAL, title='Fictional reservoir budget decision',
                   content=text, published_at=datetime(2030, 3, 12, tzinfo=timezone.utc))


@pytest.fixture
def offline_recording(monkeypatch):
    # Use the real model adapter, archive pagination, guarded fetch parsing and graph.
    # The only replaced boundaries are HTTP transports and the clock.
    monkeypatch.setattr('audit.workflow.related_removals.time.time', lambda: ANCHOR)

    async def run(*, full=False, case='in_scope'):
        article = large_article()
        page_body = ('A fictional pottery workshop displayed glazed bowls and carved tiles. ' * 800)
        pages = {CANDIDATE: page_body}
        if case == 'original_live':
            pages[ORIGINAL] = article.content

        def fetch(request):
            text = pages.get(str(request.url))
            return httpx.Response(200 if text else 404,
                                  text=f'<html><body><p>{text}</p></body></html>' if text else '',
                                  headers={'content-type': 'text/html; charset=utf-8'})

        monkeypatch.setattr(fetch_module, '_guarded_transport',
                            lambda limits: httpx.MockTransport(fetch))

        def archive(request):
            path = request.url.path
            if path == '/articles/1/similar':
                body = {'articles': [{'id': 2, 'link': CANDIDATE,
                                      'title': 'Fictional pottery workshop'}]}
            elif path == '/articles/1':
                body = {'id': 1, 'url': ORIGINAL, 'title': article.title,
                        'news_text': article.content,
                        'checks': {'entries': [{'check_date': ANCHOR - 100, 'status': 404}]}}
            elif path == '/articles':
                body = {'id': 1, 'url': ORIGINAL, 'title': article.title,
                        'publish_date': '2030-03-11', 'news_text': article.content}
            elif path == '/audits':
                page = int(request.url.params['page'])
                status = int(request.url.params['status'])
                # Ten full listing pages, then an empty page for each status.
                body = {'items': [
                    {'id': status * 1000 + page * 100 + i, 'status': status,
                     'created_at': ANCHOR - 200,
                     'article': {'id': 5000 + i,
                                 'link': f'https://other.example/reports/{status}/{page}/{i}',
                                 'title': ('Fictional reservoir budget spending contracts'
                                           if status == 0 and page == 1 and i == 0
                                           else f'Imaginary pottery exhibition number {i}'),
                                 'newspaper': 'Fictional pottery journal',
                                 'news_text': page_body}}
                    for i in range(100)] if page <= 2 else []}
            else:
                raise AssertionError('Unexpected archive path')
            return httpx.Response(200, json=body)

        def complete(request):
            payload = json.loads(request.content)
            name = payload.get('response_format', {}).get('json_schema', {}).get('name')
            answers = {
                'CoarseClassification': {'top_level_topics': ['medtop:1000'],
                                         'genre': 'genre:Current', 'geographic_scope': 'local',
                                         'gist': 'A fictional reservoir budget decision.'},
                'FineClassification': {'topics': ['medtop:1000'], 'confidences': [0.9]},
                'RelevanceResult': {'is_relevant': case != 'low_interest',
                                    'confidence': 0.9, 'reasoning': 'Invented scope judgment.'},
                'SourceResult': {'source_kind': 'unknown'},
                'WebSearchResult': {'reasoning': 'No invented match selected.'},
            }
            answer = json.dumps(answers[name]) if name else 'NONE'
            return httpx.Response(200, json={
                'id': 'fake-completion', 'object': 'chat.completion', 'created': 1,
                'model': 'fake-model',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20},
            })

        client = AsyncOpenAI(base_url='https://model.example/v1', api_key='fake',
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(complete)))
        engine = AuditEngine({'model_enabled': True, 'model_name': 'fake-model',
                              'archive_base_url': 'https://archive.example',
                              'record_full_evidence': full}, client=client)
        await engine.archive.http.aclose()
        engine.archive.http = httpx.AsyncClient(transport=httpx.MockTransport(archive))
        engine.catalog = ConceptCatalog([
            Concept(notation=f'medtop:{1000 + i}', scheme='iptc_mediatopic',
                    pref_label=f'Invented topic {i}', definition='Fictional vocabulary. ' * 15)
            for i in range(1000)
        ], [Concept(notation='genre:Current', scheme='iptc_genre', pref_label='Current')])
        engine.agents = build_stage_agents(engine.model_descriptor('fake-model'))
        request = AuditRequest(removed_article=article)
        try:
            result = await engine.run(request)
        finally:
            await engine.close()
        assert all(step['status'] != 'failed' for step in result['steps'])
        return result, request.model_dump(mode='json')

    return run


async def test_large_step_records_are_bounded(offline_recording, record_property):
    result, request = await offline_recording()
    assert SENTINEL in request['removed_article']['content']
    assert [step['name'] for step in result['steps']] == NAMES
    sizes = {}
    for step in result['steps']:
        sizes[step['name']] = len(json.dumps(step['input']))
        assert sizes[step['name']] < 4096
        for value in (step['input'], step['evidence'], step['output']['details']):
            assert SENTINEL not in json.dumps(value)
        for event in step['evidence']:
            assert len(json.dumps(event)) < 4096
        assert step['input']['article']['content_sha256'] == hashlib.sha256(
            request['removed_article']['content'].encode()).hexdigest()
    record_property('step_input_sizes', sizes)
    print('Step input sizes:', json.dumps(sizes, sort_keys=True))
    classification = result['steps'][0]
    assert classification['input']['catalog']['topics'] == 1000
    assert classification['input']['catalog']['bytes'] > 400_000
    assert classification['model_calls'][0]['request']['messages'][1]['content']
    related = result['steps'][3]
    assert related['model_calls']
    assert len(related['output']['details']['prefiltered']) == 1
    assert related['input']['archive_publish_date'] == '2030-03-11'
    assert related['input']['anchor_ts'] == ANCHOR - 100
    assert related['input']['window'] == {
        'date_from': ANCHOR - 100 - 14 * 86400, 'date_to': ANCHOR}
    reads = [event for event in related['evidence'] if event.get('path') == '/audits']
    assert len(reads) == 15
    assert sum(event['output']['summary']['items'] for event in reads) == 1000
    assert all(event['output']['summary']['bytes'] > 5_000_000
               for event in reads if event['output']['summary']['items'])
    article_read = next(event for event in related['evidence'] if event.get('path') == '/articles')
    assert article_read['output']['summary']['news_text'] == text_summary(
        request['removed_article']['content'])


async def test_debug_restores_full_bodies_but_inputs_stay_lean(offline_recording):
    result, request = await offline_recording(full=True)
    related = result['steps'][3]
    article_read = next(event for event in related['evidence'] if event.get('path') == '/articles')
    assert article_read['output']['news_text'] == request['removed_article']['content']
    fetched = result['steps'][1]['evidence'][-1]
    assert len(fetched['output']['live'][0]['text']) > 40_000
    assert fetched['input']['args'][0][0]['url'] == CANDIDATE
    for step in result['steps']:
        assert len(json.dumps(step['input'])) < 4096
        assert SENTINEL not in json.dumps(step['input'])


@pytest.mark.parametrize('case', ['in_scope', 'low_interest', 'original_live'])
async def test_recording_switch_preserves_results_and_model_calls(offline_recording, case):
    lean, _ = await offline_recording(case=case)
    full, _ = await offline_recording(full=True, case=case)
    assert lean['response'] == full['response']
    assert [s['output'] for s in lean['steps']] == [s['output'] for s in full['steps']]
    assert [s['model_calls'] for s in lean['steps']] == [s['model_calls'] for s in full['steps']]
    assert [s['input'] for s in lean['steps']] == [s['input'] for s in full['steps']]
    expected = {'in_scope': 'INCONCLUSIVE', 'low_interest': 'LIKELY_LOW_PUBLIC_INTEREST',
                'original_live': 'NOT_REMOVED'}
    assert lean['response']['verdict'] == expected[case]


async def test_a_failing_input_record_never_changes_the_audit(offline_recording, monkeypatch):
    baseline, _ = await offline_recording()

    def broken(name, state, engine):
        raise KeyError('invented recording fault')

    monkeypatch.setattr(step_inputs, 'build', broken)
    result, _ = await offline_recording()
    assert result['response'] == baseline['response']
    assert [s['output'] for s in result['steps']] == [s['output'] for s in baseline['steps']]
    assert all(s['input'].get('record_error') == 'KeyError' for s in result['steps'])


@pytest.mark.parametrize('value,expected', [
    ('true', True), ('1', True), ('false', False), ('0', False),
])
def test_full_evidence_environment(monkeypatch, value, expected):
    monkeypatch.setattr('audit.config.load_dotenv', lambda **kwargs: False)
    monkeypatch.setenv('RECORD_FULL_EVIDENCE', value)
    monkeypatch.setenv('RUN_MODE', 'live')
    assert Settings.from_env().engine_config()['record_full_evidence'] is expected


def test_archive_summary_is_stable_and_json_safe():
    body = {'unrecognized': ['invented text' * 500], 'score': float('nan')}
    assert archive_output('/other', body) == {'summary': summary(body)}
    json.dumps(archive_output('/other', body), allow_nan=False)
