import pytest
from audit.domain import AuditRequest, Article
from audit.engine import AuditEngine, NAMES
from audit.workflow import classification, similarity, relevance, related_removals

@pytest.mark.asyncio
async def test_disabled_model_never_fabricates_findings():
    engine = AuditEngine({})
    await engine.start()
    seen = []
    async def on_step(step):
        seen.append(step)
    try:
        result = await engine.run(AuditRequest(removed_article=Article(url='https://example.com/a', title='Headline', content='Text')), on_step)
    finally:
        await engine.close()
    assert result['synthetic'] is True
    assert result['response']['verdict'] == 'AUDIT_FAILED'
    assert [s['name'] for s in seen] == NAMES
    assert [s['status'] for s in seen] == ['failed'] + ['skipped'] * 5

@pytest.mark.asyncio
async def test_low_interest_short_circuits_after_related_check(monkeypatch):
    async def cls(*a, **kw):
        return classification.ClassificationOutcome(explanation='no catalog', details={'skipped':True})
    async def sim(*a, **kw):
        return similarity.SimilarityEvidence(reason='no archive')
    async def rel(*a, **kw):
        return relevance.RelevanceResult(is_relevant=False, confidence=.8, reasoning='not in scope')
    async def related(*a, **kw):
        return related_removals.RelatedRemovalsEvidence(reason='unavailable')
    monkeypatch.setattr(classification, 'run', cls)
    monkeypatch.setattr(similarity, 'run', sim)
    monkeypatch.setattr(relevance, 'run', rel)
    monkeypatch.setattr(related_removals, 'run', related)
    engine = AuditEngine({'model_enabled':True})
    engine.agents = type('Agents', (), {'classification':None,'relevance':None,'related_removals':None})()
    try:
        result = await engine.run(AuditRequest(removed_article=Article(url='https://example.com/a', title='Headline', content='Text')))
    finally:
        await engine.close()
    assert result['response']['verdict'] == 'LIKELY_LOW_PUBLIC_INTEREST'
    assert [s['status'] for s in result['steps']] == ['skipped','completed','completed','completed','skipped','skipped']
    assert result['response']['short_circuited_at'] == 'relevance'
    # The relevance call succeeded; the later steps must not read as a failure.
    assert [s['output']['explanation'] for s in result['steps'][4:]] == ['Skipped: the article was judged of low public interest'] * 2

@pytest.mark.asyncio
async def test_model_adapter_captures_complete_call_and_failure():
    from types import SimpleNamespace
    from pydantic import BaseModel
    from audit.model import Agent, trace
    class Answer(BaseModel):
        answer: str
    class Completion:
        model = 'served-model'
        usage = None
        choices = [SimpleNamespace(message=SimpleNamespace(content='{"answer":"yes"}'))]
        def model_dump(self, mode):
            return {'model': self.model, 'choices': [{'message': {'content': '{"answer":"yes"}'}}]}
    class Completions:
        async def create(self, **kwargs):
            return Completion()
    model = SimpleNamespace(name='configured', max_tokens=500, temperature=0, thinking_control='lmstudio', client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    events = []
    token = trace.set(events)
    try:
        answer = await Agent(model, result_type=Answer, system_prompt='rules').run('article', model_settings={'max_tokens':100})
    finally:
        trace.reset(token)
    assert answer.data.answer == 'yes'
    call = events[0]
    assert call['request']['messages'] == [{'role':'system','content':'rules'}, {'role':'user','content':'article'}]
    assert call['request']['max_tokens'] == 100
    assert call['request']['extra_body'] == {'chat_template_kwargs': {'enable_thinking':False}, 'reasoning_effort':'none'}
    assert call['parsed_output'] == {'answer':'yes'}
    assert call['response_model'] == 'served-model'
    assert call['raw_completion']['choices']

@pytest.mark.asyncio
async def test_measured_relocation_stops_graph(monkeypatch):
    from audit.search.base import SearchResult
    async def cls(*a, **kw):
        return classification.ClassificationOutcome(explanation='no catalog', details={'skipped':True})
    async def sim(*a, **kw):
        return similarity.SimilarityEvidence(reason='measured copy', relocated_to=SearchResult(url='https://example.com/new', title='Headline', snippet=''), relocation_old_in_new=1.0, relocation_new_in_old=1.0)
    monkeypatch.setattr(classification, 'run', cls)
    monkeypatch.setattr(similarity, 'run', sim)
    engine = AuditEngine({'model_enabled':True})
    engine.agents = type('Agents', (), {'classification':None})()
    try:
        result = await engine.run(AuditRequest(removed_article=Article(url='https://example.com/old', title='Headline', content='Text')))
    finally:
        await engine.close()
    assert result['response']['verdict'] == 'MOVED'
    assert result['response']['short_circuited_at'] == 'similarity'
    assert [s['status'] for s in result['steps']] == ['skipped','completed','skipped','skipped','skipped','skipped']

@pytest.mark.asyncio
async def test_invalid_model_output_keeps_raw_completion():
    from types import SimpleNamespace
    from pydantic import BaseModel, ValidationError
    from audit.model import Agent, trace
    class Answer(BaseModel):
        answer: bool
    class Completion:
        model = 'served'
        usage = None
        choices = [SimpleNamespace(message=SimpleNamespace(content='{"answer":"not a boolean"}'))]
        def model_dump(self, mode):
            return {'choices':[{'message':{'content':'{"answer":"not a boolean"}'}}]}
    class Completions:
        async def create(self, **kwargs):
            return Completion()
    model = SimpleNamespace(name='configured', max_tokens=500, temperature=0, thinking_control='none', client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    events = []
    token = trace.set(events)
    try:
        with pytest.raises(ValidationError):
            await Agent(model, result_type=Answer, system_prompt='rules').run('article')
    finally:
        trace.reset(token)
    assert events[0]['raw_completion']['choices'][0]['message']['content'] == '{"answer":"not a boolean"}'
    assert events[0]['parsed_output'] is None
    assert events[0]['error_type'] == 'ValidationError'

@pytest.mark.asyncio
@pytest.mark.parametrize('mode, verdict, statuses', [
    ('relevance_failure','AUDIT_FAILED',['skipped','completed','failed','skipped','skipped','skipped']),
    ('live_original','NOT_REMOVED',['skipped','completed','completed','completed','completed','skipped']),
    ('web_failure','AUDIT_FAILED',['skipped','completed','completed','completed','completed','failed']),
])
async def test_remaining_graph_paths(monkeypatch, mode, verdict, statuses):
    from audit.workflow import source, web_search
    async def cls(*a, **kw):
        return classification.ClassificationOutcome(explanation='no catalog', details={'skipped':True})
    async def sim(*a, **kw):
        return similarity.SimilarityEvidence(reason='no archive')
    async def rel(*a, **kw):
        if mode == 'relevance_failure':
            raise ValueError('sensitive payload must not appear')
        return relevance.RelevanceResult(is_relevant=True, confidence=.8, reasoning='in scope')
    async def related(*a, **kw):
        return related_removals.RelatedRemovalsEvidence(reason='unavailable')
    async def src(*a, **kw):
        if mode == 'live_original':
            return source.SourceEvidence(live_at_original_url=True, self_containment=.8, reasoning='live')
        return source.SourceEvidence(reasoning='unavailable')
    async def web(*a, **kw):
        raise web_search.WebSearchModelFailure('sensitive payload must not appear')
    for module, function in ((classification,cls),(similarity,sim),(relevance,rel),(related_removals,related),(source,src),(web_search,web)):
        monkeypatch.setattr(module, 'run', function)
    engine = AuditEngine({'model_enabled':True})
    engine.agents = type('Agents', (), {'classification':None,'relevance':None,'related_removals':None,'source':None,'web_search':None})()
    try:
        result = await engine.run(AuditRequest(removed_article=Article(url='https://example.com/a', title='Headline', content='Text')))
    finally:
        await engine.close()
    assert result['response']['verdict'] == verdict
    assert [s['status'] for s in result['steps']] == statuses
    assert 'sensitive payload' not in str(result)

@pytest.mark.asyncio
async def test_graph_disables_ambient_tracing(monkeypatch):
    from langsmith.run_helpers import get_tracing_context
    from audit.workflow import classification
    seen = []
    async def cls(*a, **kw):
        seen.append(get_tracing_context()['enabled'])
        return classification.ClassificationOutcome(explanation='no catalog', details={'skipped':True})
    async def sim(*a, **kw):
        raise RuntimeError('test stop')
    async def rel(*a, **kw):
        raise RuntimeError('test stop')
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    monkeypatch.setattr(classification, 'run', cls)
    monkeypatch.setattr(similarity, 'run', sim)
    monkeypatch.setattr(relevance, 'run', rel)
    engine = AuditEngine({'model_enabled':True})
    engine.agents = type('Agents', (), {'classification':None,'relevance':None})()
    try:
        await engine.run(AuditRequest(removed_article=Article(url='https://example.com/a', title='Headline', content='Text')))
    finally:
        await engine.close()
    assert seen == [False]

@pytest.mark.asyncio
async def test_related_removals_keep_low_interest_article_in_scope(monkeypatch):
    from audit.workflow import source
    async def cls(*a, **kw):
        return classification.ClassificationOutcome(explanation='no catalog', details={'skipped':True})
    async def sim(*a, **kw):
        return similarity.SimilarityEvidence(reason='no archive')
    async def rel(*a, **kw):
        return relevance.RelevanceResult(is_relevant=False, confidence=.8, reasoning='not in scope')
    async def related(*a, **kw):
        return related_removals.RelatedRemovalsEvidence(flag=True, available=True, reason='related removals')
    async def src(*a, **kw):
        return source.SourceEvidence(live_at_original_url=True, self_containment=.8, reasoning='live')
    for module, function in ((classification,cls),(similarity,sim),(relevance,rel),(related_removals,related),(source,src)):
        monkeypatch.setattr(module, 'run', function)
    engine = AuditEngine({'model_enabled':True})
    engine.agents = type('Agents', (), {'classification':None,'relevance':None,'related_removals':None,'source':None})()
    try:
        result = await engine.run(AuditRequest(removed_article=Article(url='https://example.com/a', title='Headline', content='Text')))
    finally:
        await engine.close()
    assert result['response']['verdict'] == 'NOT_REMOVED'
    assert result['response']['flags'] == ['RELATED_REMOVALS']
    assert result['steps'][3]['output']['details']['kept_in_scope'] is True

@pytest.mark.asyncio
async def test_auto_model_resolution_skips_embeddings(monkeypatch):
    from types import SimpleNamespace
    import audit.engine as module
    class Client:
        def __init__(self, **kwargs):
            self.models = self
        async def list(self):
            return SimpleNamespace(data=[SimpleNamespace(id='text-embedding-model'), SimpleNamespace(id='chat-model')])
        async def close(self):
            pass
    monkeypatch.setattr(module, 'AsyncOpenAI', Client)
    engine = AuditEngine({'model_enabled':True, 'model_name':'auto'})
    try:
        await engine.start()
        assert engine.public_config['model_name'] == 'chat-model'
    finally:
        await engine.close()

def test_public_config_does_not_expose_wallet_credentials():
    engine = AuditEngine({'archive_base_url':'https://user:password@archive.example/path?key=secret', 'archive_wallet_path':'/private/wallet-secret.json', 'archive_wallet_json':'private-key-json'})
    try:
        published = str(engine.public_config)
        assert engine.public_config['archive_authenticated'] is True
        for secret in ('password','key=secret','wallet-secret','private-key-json'):
            assert secret not in published
    finally:
        import asyncio
        asyncio.run(engine.close())

@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['NaN', 'Infinity', '-Infinity'])
async def test_nonfinite_model_result_is_rejected_but_raw_text_is_kept(invalid):
    import json
    from types import SimpleNamespace
    from pydantic import BaseModel
    from audit.model import Agent, trace
    class Answer(BaseModel):
        confidence: float
    raw = '{"confidence":"' + invalid + '"}'
    class Completion:
        model = 'served'
        usage = SimpleNamespace(model_dump=lambda mode: {'cost':float('nan')})
        choices = [SimpleNamespace(message=SimpleNamespace(content=raw))]
        def model_dump(self, mode):
            return {'choices':[{'message':{'content':raw}}], 'score':float('inf')}
    class Completions:
        async def create(self, **kwargs):
            return Completion()
    model = SimpleNamespace(name='configured', max_tokens=100, temperature=0, thinking_control='none', client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    events = []
    token = trace.set(events)
    try:
        with pytest.raises(ValueError):
            await Agent(model, result_type=Answer, system_prompt='rules').run('article')
    finally:
        trace.reset(token)
    assert events[0]['raw_completion']['choices'][0]['message']['content'] == raw
    assert events[0]['raw_completion']['score'] is None
    assert events[0]['usage']['cost'] is None
    assert events[0]['parsed_output'] is None
    json.dumps(events, allow_nan=False)

@pytest.mark.asyncio
async def test_unexpected_stage_failure_saves_trace_and_returns_failed_audit(monkeypatch):
    import json
    from audit.model import trace
    engine = AuditEngine({'model_enabled':True})
    original_execute = engine._execute
    async def explode(name, state):
        if state.get('stopped'):
            return await original_execute(name, state)
        trace.get().append({'type':'model', 'raw_completion':{'message':'actual raw text', 'score':float('nan')}})
        trace.get().append({'type':'fetch', 'output':{'score':float('inf')}})
        raise RuntimeError('secret exception text')
    monkeypatch.setattr(engine, '_execute', explode)
    seen = []
    async def on_step(step):
        json.dumps(step, allow_nan=False)
        seen.append(step)
    try:
        result = await engine.run(AuditRequest(removed_article=Article(url='https://example.com/a', title='Headline', content='Text')), on_step)
    finally:
        await engine.close()
    assert result['response']['verdict'] == 'AUDIT_FAILED'
    assert seen[0]['status'] == 'failed'
    assert seen[0]['output']['details'] == {'error_type':'RuntimeError'}
    # An unexpected error is not a model failure and must not be reported as one.
    assert seen[0]['output']['explanation'] == 'Stage did not complete: an unexpected error stopped this step.'
    assert seen[0]['model_calls'][0]['raw_completion'] == {'message':'actual raw text','score':None}
    assert seen[0]['evidence'][0]['output']['score'] is None
    assert [step['status'] for step in seen] == ['failed'] + ['skipped'] * 5
    assert 'secret exception text' not in str(result)
    json.dumps(result, allow_nan=False)

@pytest.mark.asyncio
async def test_cancellation_records_failed_step_before_propagation(monkeypatch):
    import asyncio
    from audit.model import trace
    engine = AuditEngine({'model_enabled':True})
    async def cancel(name, state):
        trace.get().append({'type':'model', 'raw_completion':{'message':'partial'}})
        raise asyncio.CancelledError()
    monkeypatch.setattr(engine, '_execute', cancel)
    seen = []
    async def on_step(step):
        seen.append(step)
    try:
        with pytest.raises(asyncio.CancelledError):
            await engine.run(AuditRequest(removed_article=Article(url='https://example.com/a', title='Headline', content='Text')), on_step)
    finally:
        await engine.close()
    assert seen[0]['status'] == 'failed'
    assert seen[0]['model_calls'][0]['raw_completion']['message'] == 'partial'
    assert seen[0]['output']['explanation'] == 'Stage did not complete: the audit was stopped.'

@pytest.mark.asyncio
async def test_external_task_cancellation_records_in_progress_stage(monkeypatch):
    import asyncio
    from audit.model import trace
    engine = AuditEngine({'model_enabled':True})
    active = asyncio.Event()
    async def wait_for_cancel(name, state):
        trace.get().append({'type':'model', 'raw_completion':{'message':'in progress'}})
        active.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(engine, '_execute', wait_for_cancel)
    seen = []
    async def on_step(step):
        seen.append(step)
    try:
        task = asyncio.create_task(engine.run(AuditRequest(removed_article=Article(url='https://example.com/a', title='Headline', content='Text')), on_step))
        await active.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await engine.close()
    assert seen[0]['status'] == 'failed'
    assert seen[0]['model_calls'][0]['raw_completion']['message'] == 'in progress'
    from audit.run_summary import summarize_run
    story = summarize_run({"status": "cancelled", "steps": seen})
    assert seen[0]["output"]["details"]["error_type"] == "CancelledError"
    assert story["steps"][0]["status"] == "stopped"
    # A person stopped the run; the stored step and the story must not blame the model.
    assert seen[0]["output"]["explanation"] == "Stage did not complete: the audit was stopped."
    assert story["steps"][0]["line"] == "Stage did not complete: the audit was stopped."
    assert story["steps"][0]["outcome"] is None
    assert story["failed_steps"] == []
    assert all(step["status"] == "pending" for step in story["steps"][1:])


@pytest.mark.asyncio
@pytest.mark.parametrize('override', [False, True])
async def test_every_stage_sends_byte_exact_system_prompt(override):
    from types import SimpleNamespace
    from audit.workflow.agents import build_stage_agents
    from audit.workflow.prompt_registry import DEFAULTS
    calls = []

    class Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                model='fabricated', usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(content='NONE'))],
                model_dump=lambda mode: {})

    model = SimpleNamespace(name='fabricated', max_tokens=100, temperature=0, thinking_control='none',
                            client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    texts = {name: text + '\nFabricated override' for name, text in DEFAULTS.items()} if override else DEFAULTS
    stages = build_stage_agents(model, texts if override else None)
    agents = {'classification_coarse': stages.classification.coarse,
              'classification_fine': stages.classification.fine,
              'relevance': stages.relevance, 'source': stages.source,
              'web_search': stages.web_search, 'related_removals': stages.related_removals}
    for name, agent in agents.items():
        # Parse errors are expected for JSON stages; the transport request is real.
        try:
            await agent.run('Fabricated stage input')
        except ValueError:
            pass
        assert calls[-1]['messages'][0] == {'role': 'system', 'content': texts[name]}
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_overlapping_runs_keep_private_agents_and_startup_defaults(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from audit.search.base import SearchResult
    from audit.workflow.agents import build_stage_agents
    from audit.workflow.prompt_registry import DEFAULTS
    texts = {**DEFAULTS, 'classification_coarse': DEFAULTS['classification_coarse'] + '\nFabricated override'}
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def cls(article, agents, catalog):
        before = agents.coarse.system_prompt
        if article.title == 'First fabricated input':
            entered.set()
            await release.wait()
        seen.append((article.title, before, agents.coarse.system_prompt))
        return classification.ClassificationOutcome(explanation='no catalog', details={'skipped': True})

    async def sim(*args, **kwargs):
        return similarity.SimilarityEvidence(relocated_to=SearchResult(url='https://fabricated.invalid/moved', title='Fabricated moved copy', snippet=''),
                                             relocation_old_in_new=1.0, relocation_new_in_old=1.0)

    monkeypatch.setattr(classification, 'run', cls)
    monkeypatch.setattr(similarity, 'run', sim)
    engine = AuditEngine({'model_enabled': True})
    engine.model = SimpleNamespace()
    engine.agents = build_stage_agents(engine.model)
    shared = engine.agents
    first = AuditRequest(removed_article=Article(url='https://fabricated.invalid/first', title='First fabricated input', content='Fabricated input'))
    second = AuditRequest(removed_article=Article(url='https://fabricated.invalid/second', title='Second fabricated input', content='Fabricated input'))
    try:
        task = asyncio.create_task(engine.run(first, prompts=texts))
        await asyncio.wait_for(entered.wait(), timeout=2)
        result = await engine.run(second)
        release.set()
        first_result = await task
        assert first_result['response']['verdict'] == result['response']['verdict'] == 'MOVED'
        assert seen == [
            ('Second fabricated input', DEFAULTS['classification_coarse'], DEFAULTS['classification_coarse']),
            ('First fabricated input', texts['classification_coarse'], texts['classification_coarse'])]
        assert engine.agents is shared
        assert shared.classification.coarse.system_prompt == DEFAULTS['classification_coarse']
    finally:
        release.set()
        await engine.close()
