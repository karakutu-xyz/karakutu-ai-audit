"""Six-node LangGraph audit, with explicit evidence and model-call provenance."""
import re
import time
import asyncio
import logging
from types import SimpleNamespace
from typing import Any, TypedDict
from urllib.parse import urlsplit, urlunsplit

import httpx
from langgraph.graph import StateGraph, START, END
from langgraph.errors import NodeCancelledError
from langsmith import tracing_context
from openai import AsyncOpenAI

from audit.config import MODEL_STEPS
from audit.domain import AuditRequest, AuditResponse, StageResult, StageName, StageOutcome, AuditFlag
from audit.model import trace
from audit.jsonsafe import json_safe
from audit.clients.karakutu import KarakutuClient
from audit.clients.concepts import ConceptCatalog
from audit.search.base import BaseSearchProvider, SearchResult, SearchState
from audit.search._common import results_from_hits
from audit.workflow import classification, similarity, relevance, related_removals, source, web_search, pipeline
from audit.workflow.agents import build_stage_agents
from audit.search.fetch import normalize_url

# The clients log request URLs, headers and SDK exceptions, including at DEBUG. Keep a
# diagnostic event while stripping their potentially sensitive message and exception.
class _NetworkLogRedactor(logging.Filter):
    def filter(self, record):
        record.msg = "Network client diagnostic redacted"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


for _logger_name in ('httpx', 'httpcore.connection', 'httpcore.socks', 'httpcore.proxy',
                     'httpcore.http11', 'httpcore.http2', 'openai._base_client',
                     'openai._response', 'openai._legacy_response'):
    logging.getLogger(_logger_name).addFilter(_NetworkLogRedactor())

NAMES = ['classification', 'similarity', 'relevance', 'related_removals', 'source', 'web_search']

class State(TypedDict, total=False):
    request: Any
    callback: Any
    agents: Any
    steps: list
    stage_results: list
    flags: list
    classification: Any
    similarity: Any
    relevance: Any
    related: Any
    source: Any
    web: Any
    stopped: Any
    response: Any
    failure: Any
    short_circuit: Any
    low_interest: Any


def safe_endpoint(value):
    """Expose endpoint identity without credentials, query parameters or fragments."""
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, host + port, parts.path, "", ""))
    except ValueError:
        return "invalid endpoint"


def dump(value):
    if hasattr(value, 'model_dump'):
        return dump(value.model_dump(mode='json'))
    if isinstance(value, list):
        return [dump(x) for x in value]
    if isinstance(value, dict):
        return {k: dump(v) for k, v in value.items()}
    return json_safe(value)


# A SearXNG engine name ("brave", "google cse", "mymemory translated"): at most three
# lowercase words and 32 characters. Anything else is not kept.
ENGINE_NAME = re.compile(r'(?=.{1,32}$)[a-z0-9][a-z0-9_.-]*(?: [a-z0-9_.-]+){0,2}')


class Search(BaseSearchProvider):
    def __init__(self, base, *, transport=None):
        self.base = base.rstrip('/')
        self.enabled = bool(self.base)
        self.state: SearchState = "unchecked" if self.enabled else "disabled"
        self._transport = transport
        self._probe_at = None
        self._probe_lock = asyncio.Lock()
        # Probes and transport failures cannot clear remembered query failures.
        # Valid search answers clear rejection; successful queries clear degradation.
        self._rejected = False
        self._degraded = False
        self._search_generation = 0

    async def probe(self) -> SearchState:
        """Bounded reachability only; HTTP answers cannot clear search failures.

        Coalesce concurrent status polls and cache for 30 seconds. A real search
        completed during a probe owns the newer state. No upstream text escapes.
        """
        if not self.enabled:
            return "disabled"
        async with self._probe_lock:
            if self._probe_at is not None and time.monotonic() - self._probe_at < 30:
                return self.state
            generation = self._search_generation
            try:
                async with asyncio.timeout(2):
                    async with httpx.AsyncClient(timeout=2, transport=self._transport) as client:
                        async with client.stream('GET', self.base + '/healthz'):
                            pass  # Headers suffice; never download an upstream body.
                state = ("rejected" if self._rejected else
                         "degraded" if self._degraded else "ok")
            except Exception:
                state = "unreachable"
            if generation == self._search_generation:
                self.state = state
            self._probe_at = time.monotonic()
            return self.state

    async def search(self, query, max_results=5, *, language=None):
        results, _failed = await self.search_checked(query, max_results, language=language)
        return results

    async def search_checked(self, query, max_results=5, *, language=None):
        event = {'type':'search', 'query':query, 'max_results':max_results, 'language':language, 'results':[], 'error_type':None}
        events = trace.get()
        if events is not None:
            events.append(event)
        if not self.base:
            event['disabled'] = True
            return [], False
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.get(self.base + '/search', params={'q':query,'format':'json','categories':'general','language':language or 'auto'})
                response.raise_for_status()
                payload = response.json()
                hits = payload.get('results') if isinstance(payload, dict) else None
                if not isinstance(hits, list):
                    # A 200 without a results list (an error object, a proxy page) is a failed
                    # query, not an empty search: it must not clear a rejection or hide an outage.
                    raise ValueError('search response has no results list')
                # SearXNG lists blocked engines as [name, reason] pairs. Only that shape counts;
                # the reason is upstream text and is never kept, and a name is kept only when it
                # looks like an engine name, so no upstream sentence can reach the trace.
                unresponsive = payload.get('unresponsive_engines')
                pairs = [entry for entry in
                         (unresponsive if isinstance(unresponsive, list) else [])
                         if isinstance(entry, (list, tuple)) and len(entry) == 2]
                names = [name.strip() for name, _reason in pairs if isinstance(name, str)
                         and ENGINE_NAME.fullmatch(name.strip())][:20]
                if names:
                    event['unresponsive_engines'] = names
            results = results_from_hits(hits, lambda h:(h.get('url'), h.get('title') or '', h.get('content') or ''), query=query, provider='SearXNG', max_results=max_results)
            event['results'] = dump(results)
            if not results and pairs:
                event['error_type'] = 'EnginesUnresponsive'
                self.state = 'degraded'
                self._degraded = True
                self._rejected = False
                self._search_generation += 1
                return [], True
            self.state = 'ok'
            self._rejected = False
            self._degraded = False
            self._search_generation += 1
            return results, False
        except Exception as exc:
            event['error_type'] = type(exc).__name__
            if isinstance(exc, httpx.TransportError):
                self.state = 'unreachable'
            else:
                self.state = 'rejected'
                self._rejected = True
            self._search_generation += 1
            return [], True


def capture_fetch(fn, kind):
    async def wrapped(*args, **kwargs):
        event = {'type':kind, 'input':dump({'args':list(args), 'kwargs':kwargs}), 'output':None, 'error_type':None}
        events = trace.get()
        if events is not None:
            events.append(event)
        try:
            result = await fn(*args, **kwargs)
            event['output'] = dump(result)
            return result
        except Exception as exc:
            event['error_type'] = type(exc).__name__
            raise
    return wrapped

# Each module imported these functions directly; wrap those references once, preserving the SSRF gate.
for _module in (similarity, source, web_search):
    _module.fetch_candidates = capture_fetch(_module.fetch_candidates, 'fetch_candidates')
source.probe_removed_url = capture_fetch(source.probe_removed_url, 'probe_removed_url')


class AuditEngine:
    def __init__(self, config: dict, *, client: AsyncOpenAI | None = None):
        self.config = dict(config)
        self.synthetic = not bool(config.get('model_enabled', False))
        self.model = None
        self.client = client
        self.fallback_models = set()
        self.archive = KarakutuClient(config.get('archive_base_url', ''), wallet_path=config.get('archive_wallet_path', ''), wallet_json=config.get('archive_wallet_json', ''))
        self.catalog = None
        provider = config.get('search_provider', 'disabled')
        if provider not in ('disabled', 'searxng'):
            raise ValueError('Unsupported search provider')
        self.search = Search(config.get('searxng_url', '') if provider == 'searxng' else '')
        self.public_config = {'model_enabled':not self.synthetic, 'model_base_url':safe_endpoint(config.get('model_base_url', '')),
                              'model_provider':config.get('model_provider', 'lmstudio'),
                              'response_format':config.get('response_format', 'json_schema'),
                              'model_steps':{step: config.get('model_name_' + step) or None for step in MODEL_STEPS},
                              'model_name':config.get('model_name', 'auto'), 'model_timeout':config.get('model_timeout', 120),
                              'max_tokens':config.get('max_tokens', 1024), 'temperature':config.get('temperature', 0),
                              'thinking_control':config.get('thinking_control', 'none'), 'search_provider':provider,
                              'searxng_url':safe_endpoint(config.get('searxng_url', '')), 'archive_base_url':safe_endpoint(config.get('archive_base_url', '')),
                              'archive_authenticated':bool(config.get('archive_wallet_path') or config.get('archive_wallet_json'))}
        graph = StateGraph(State)
        for name in NAMES:
            graph.add_node(name, self._node(name))
        graph.add_node('skip_remaining', self._skip_remaining)
        graph.add_edge(START, NAMES[0])
        for a, b in zip(NAMES, NAMES[1:]):
            graph.add_conditional_edges(a, lambda state, next_node=b: 'skip_remaining' if state.get('stopped') else next_node, ['skip_remaining', b])
        graph.add_edge('skip_remaining', END)
        graph.add_edge(NAMES[-1], END)
        self.graph = graph.compile()

    async def start(self):
        if not self.synthetic:
            self.client = self.client or AsyncOpenAI(base_url=self.config.get('model_base_url') or 'http://localhost:1234/v1', api_key=self.config.get('model_api_key') or 'not-needed', timeout=self.config.get('model_timeout', 120), max_retries=0)
            name = self.config.get('model_name', 'auto')
            if name == 'auto':
                candidates, error = await self.available_models()
                if error:
                    raise RuntimeError('Model discovery failed')
                if not candidates:
                    raise RuntimeError('No chat model is loaded')
                name = candidates[0]
            self.model = self.model_descriptor(name)
            self.public_config['model_name'] = name
            self.models = {step: self.model_descriptor(self.public_config['model_steps'][step] or name) for step in MODEL_STEPS}
            self.agents = build_stage_agents(self.models)
        if self.archive.enabled:
            topics = await self.archive.list_concepts('iptc_mediatopic')
            genres = await self.archive.list_concepts('iptc_genre') if topics else []
            if topics and genres:
                self.catalog = ConceptCatalog(topics, genres)

    def model_descriptor(self, name, *, provider=None, response_format=None):
        return SimpleNamespace(client=self.client, name=name,
            max_tokens=self.config.get('max_tokens', 1024),
            temperature=self.config.get('temperature', 0),
            thinking_control=self.config.get('thinking_control', 'none'),
            provider=provider or self.public_config['model_provider'],
            response_format=response_format or self.public_config['response_format'],
            fallback_models=self.fallback_models)

    async def available_models(self):
        if self.synthetic or self.client is None:
            return [], None
        try:
            models = await self.client.models.list()
            return [item.id for item in models.data if not any(
                term in item.id.lower() for term in ('embed', 'rerank'))], None
        except Exception:
            return [], 'models_unavailable'

    async def close(self):
        if self.client:
            await self.client.close()
        await self.archive.close()

    async def _skip_remaining(self, state):
        # Emit a durable record for each unvisited stage, in contract order.
        current = dict(state)
        for name in NAMES[len(state['steps']):]:
            current.update(await self._node(name)(current))
        return {'steps':current['steps'], 'stage_results':current['stage_results']}

    def _node(self, name):
        async def node(state):
            started = time.monotonic()
            events = []
            token = trace.set(events)
            inp = {'article':dump(state['request'].removed_article), 'prior_stages':dump(state['stage_results'])}
            if name == 'classification' and self.catalog is not None:
                inp['catalog'] = self.catalog.to_json()
            for key in ('classification', 'similarity', 'relevance', 'related', 'source'):
                if key in state:
                    inp[key] = dump(state[key])
            cancelled = None
            try:
                result, updates = await self._execute(name, state)
            except asyncio.CancelledError as exc:
                cancelled = exc
                # A person stopped the run (or the service shut down); the model did not fail.
                result = pipeline._stage_failed(StageName(name), pipeline.STOPPED)
                result.details = {'error_type': 'CancelledError'}
                updates = {'stopped':'stage was cancelled', 'failure':'The audit was cancelled.'}
            except Exception as exc:
                result = pipeline._stage_failed(StageName(name), pipeline.UNEXPECTED_ERROR)
                result.details = {'error_type':type(exc).__name__}
                updates = {'stopped':'stage did not complete', 'failure':'An audit stage failed unexpectedly.'}
            finally:
                trace.reset(token)
            status = 'skipped' if result.skipped else ('failed' if result.explanation.startswith('Stage did not complete') else 'completed')
            step = dump({'name':name, 'status':status,
                    'model':next((e.get('request', {}).get('model') for e in events if e['type'] == 'model'), None), 'input':inp, 'output':dump(result),
                    'model_calls':[e for e in events if e['type'] == 'model'],
                    'evidence':[e for e in events if e['type'] != 'model'],
                    'duration_ms':round((time.monotonic()-started)*1000)})
            if state.get('callback'):
                await state['callback'](step)
            if cancelled is not None:
                raise cancelled
            return {**updates, 'steps':state['steps'] + [step], 'stage_results':state['stage_results'] + [result]}
        return node

    async def _execute(self, name, s):
        article = s['request'].removed_article
        stage = StageName(name)
        if s.get('stopped'):
            return pipeline._skipped(stage, s['stopped']), {}
        if self.synthetic:
            if name == 'classification':
                return pipeline._stage_failed(stage), {'stopped':'model is disabled; audit did not run', 'failure':'Model is disabled; the audit did not run.'}
        if name == 'classification':
            try:
                out = await classification.run(article, s['agents'].classification, self.catalog)
            except Exception:
                out = classification.ClassificationOutcome(explanation='Classification did not complete; no tags assigned.', details={'crashed':True})
            result = StageResult(stage=stage, outcome=StageOutcome.PASS if out.classification is not None else StageOutcome.INCONCLUSIVE, explanation=out.explanation, skipped=bool(out.details.get('skipped')), details=out.details)
            return result, {'classification':out.classification}
        if name == 'similarity':
            try:
                out = await similarity.run(article, client=self.archive)
            except Exception:
                out = similarity.SimilarityEvidence(reason='Similar-link lookup failed; continuing without archive candidates')
            result = StageResult(stage=stage, outcome=StageOutcome.PASS if out.relocated_to else StageOutcome.INCONCLUSIVE, explanation=out.reason, details=pipeline._similarity_details(out))
            updates = {'similarity':out}
            if out.relocated_to:
                updates.update({
                    'stopped': 'similarity found a measured relocation',
                    'web': web_search.WebSearchEvidence(
                        relocated_to=out.relocated_to,
                        relocation_old_in_new=out.relocation_old_in_new,
                        relocation_new_in_old=out.relocation_new_in_old,
                        matches=[out.relocated_to], hits_searched=len(out.hits),
                        archive_candidates=len(out.hits),
                        live_pages_read=len(out.fetched.live),
                        unreadable_count=len(out.fetched.unreadable),
                        reasoning=out.reason,
                    ),
                    'short_circuit': stage,
                })
            return result, updates
        if name == 'relevance':
            try:
                out = await relevance.run(article, s['agents'].relevance)
            except Exception:
                return pipeline._stage_failed(stage), {'stopped':'relevance did not complete', 'failure':'The relevance model failed.'}
            return StageResult(stage=stage, outcome=StageOutcome.PASS if out.is_relevant else StageOutcome.FAIL, confidence=out.confidence, explanation=out.reasoning), {'relevance':out}
        if name == 'related_removals':
            try:
                out = await related_removals.run(article, s['agents'].related_removals, client=self.archive, gist=s['classification'].gist if s.get('classification') else '')
            except Exception:
                out = related_removals.RelatedRemovalsEvidence(reason='Related-removals lookup failed')
            flags = list(s['flags'])
            if out.flag:
                flags.append(AuditFlag.RELATED_REMOVALS)
                out.kept_in_scope = not s['relevance'].is_relevant
            result = StageResult(stage=stage, outcome=StageOutcome.PASS if out.flag else StageOutcome.INCONCLUSIVE if not out.available or out.model_failed else StageOutcome.FAIL, explanation=out.reason, details=pipeline._related_details(out))
            updates = {'related':out, 'flags':flags}
            if not s['relevance'].is_relevant and not out.flag:
                updates.update({'stopped':'the article was judged of low public interest', 'low_interest':True, 'short_circuit':StageName.RELEVANCE})
            return result, updates
        if name == 'source':
            try:
                out = await source.run(article, s['agents'].source, self.search)
            except Exception:
                out = source.SourceEvidence(reasoning='Source stage failed')
            flags = list(s['flags'])
            if out.dead_source:
                flags.append(AuditFlag.SOURCE_DEAD)
            result = StageResult(stage=stage, outcome=StageOutcome.PASS if out.live_at_original_url or out.source_status in ('live','dead') else StageOutcome.INCONCLUSIVE, explanation=out.reasoning, details=pipeline._source_details(out))
            updates = {'source':out, 'flags':flags}
            if out.live_at_original_url:
                updates.update({'stopped':'the removed URL is live with its text substantially intact', 'web':web_search.WebSearchEvidence(live_at_original_url=True, original_url=article.url, original_url_containment=out.self_containment, reasoning='Web search skipped: the removed URL answers with the article text.')})
            return result, updates
        sim, src = s['similarity'], s['source']
        leads = list(sim.hits)
        prefetched = sim.fetched.model_copy(deep=True)
        if src.live_source:
            prefetched.live.append(src.live_source)
            leads.append(SearchResult(url=src.live_source.url, title=src.live_source.title, snippet='', source='cited'))
        leads.extend(src.extra_hits)
        try:
            out = await web_search.run(
                article, self.search, s['agents'].web_search,
                prefetched=prefetched, extra_hits=leads,
                rewrites=({normalize_url(sim.rewrite.url): sim.rewrite} if sim.rewrite else {}),
            )
        except web_search.WebSearchModelFailure as exc:
            failed = pipeline._stage_failed(stage)
            note = web_search.search_failure_note(exc.evidence) if exc.evidence else ''
            if exc.evidence is not None:
                # The note follows: a failed step is recognised by its leading sentence.
                failed = failed.model_copy(update={
                    'explanation': (failed.explanation + ' ' + note).strip(),
                    'details': (pipeline._web_details(exc.evidence)
                                | web_search.overlap_details(exc.evidence)),
                })
            return failed, {'failure': note + 'The web-search model failed on every attempt.'}
        if src.dead_source and not any(normalize_url(d.url) == normalize_url(src.dead_source.url) for d in out.dead_same_story):
            out.dead_same_story.insert(0, src.dead_source)
        return StageResult(
            stage=stage,
            outcome=StageOutcome.PASS if out.matches else StageOutcome.FAIL,
            explanation=out.reasoning,
            details=pipeline._web_details(out) | web_search.overlap_details(out),
        ), {'web':out}

    async def run(self, request: AuditRequest, on_step=None, prompts: dict[str, str] | None = None, models: dict | None = None) -> dict:
        agents = getattr(self, 'agents', None)
        if not self.synthetic:
            if models is not None:
                stage_models = {step: self.model_descriptor(name,
                    provider=models['provider'], response_format=models['response_format'])
                    for step, name in models['steps'].items()}
            else:
                stage_models = getattr(self, 'models', self.model if prompts is not None else None)
            if stage_models is not None:
                agents = build_stage_agents(stage_models, prompts)
        # Audit text and evidence must never be sent to ambient LangSmith tracing.
        with tracing_context(enabled=False):
            try:
                state = await self.graph.ainvoke({'request':request, 'callback':on_step, 'agents':agents, 'steps':[], 'stage_results':[], 'flags':[]})
            except NodeCancelledError:
                raise asyncio.CancelledError() from None
        stages = state['stage_results']
        cls = state.get('classification')
        if state.get('failure'):
            response = pipeline._audit_failed(state['failure'], stages=stages, classification=cls)
        elif state.get('low_interest'):
            response = pipeline._low_interest_response(
                state['similarity'], state['relevance'], state['related'],
                stages=stages, classification=cls,
            )
        else:
            web = state['web']
            src = state.get('source')
            if src and src.dead_source and not any(normalize_url(d.url) == normalize_url(src.dead_source.url) for d in web.dead_same_story):
                web.dead_same_story.insert(0, src.dead_source)
            verdict, findings = pipeline._decide(web)
            flags = state['flags']
            if AuditFlag.SOURCE_DEAD in flags:
                who = f' ({src.source_name})' if src.source_name else ''
                findings += f" RED FLAG (SOURCE_DEAD): the source this article itself credits{who}, {src.dead_source.url}, answers {src.dead_source.reason.replace('cited source — ', '')} — the origin copy of the story is gone too. This is a pointer for a human reviewer, not a finding of censorship."
            if AuditFlag.RELATED_REMOVALS in flags:
                findings += ' ' + related_removals.describe(state['related'])
                if state['related'].kept_in_scope:
                    findings += f" On its own the public-interest screen judged this article out of scope ({state['relevance'].reasoning}); the pattern of related removals kept it in the audit."
            response = AuditResponse(verdict=verdict, stages=stages, short_circuited_at=state.get('short_circuit'), findings=pipeline._with_classification(findings, cls), classification=cls, flags=flags)
        return {'response':dump(response), 'steps':state['steps'], 'model':{'name':self.public_config['model_name'], 'enabled':not self.synthetic}, 'synthetic':self.synthetic}
