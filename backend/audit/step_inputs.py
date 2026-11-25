"""Named step dependencies for review, without copying pipeline state or bodies."""
import html
import re

from audit.evidence import bounded, summary, text_summary
from audit.search.fetch import prose_of
from audit.workflow import relevance as relevance_step, related_removals as related_step


def article_ref(article):
    return bounded({'ref': 'run.input.removed_article', 'url': article.url,
            'title': article.title,
            'published_at': article.published_at.isoformat() if article.published_at else None,
            'content_chars': len(article.content or ''),
            'content_sha256': text_summary(article.content or '')['sha256']})


def catalog_summary(catalog):
    # fetched_at is runtime metadata, not part of the vocabulary the step reads.
    data = {key: value for key, value in catalog.to_json().items() if key != 'fetched_at'}
    return {'topics': catalog.topic_count, 'genres': catalog.genre_count, **summary(data)}


def classification(state, engine):
    if engine.catalog is None:
        return {'skipped_because': 'no IPTC concept catalog'}
    return {'article': article_ref(state['request'].removed_article),
            'catalog': catalog_summary(engine.catalog)}


def similarity(state, engine):
    return {'article': article_ref(state['request'].removed_article),
            'archive_enabled': engine.archive.enabled}


def relevance(state, engine):
    article = state['request'].removed_article
    prose = prose_of(article.content or '') or article.content or ''
    return {'article': article_ref(article),
            'prose_head_chars': len(prose[:relevance_step.MAX_RELEVANCE_CHARS])}


def related_removals(state, engine):
    article = state['request'].removed_article
    prose = prose_of(article.content or '') or article.content or ''
    head = re.sub(r'\s+', ' ', html.unescape(prose))
    cls = state.get('classification')
    rel = state.get('relevance')
    return {'article': article_ref(article), 'archive_enabled': engine.archive.enabled,
            'classification_gist': cls.gist if cls else '',
            'is_relevant': rel.is_relevant if rel else None,
            'key_terms_head_chars': len(head[:related_step.TEXT_HEAD_CHARS]),
            'model_head_chars': len(head[:related_step.MODEL_TEXT_CHARS])}


def source(state, engine):
    return {'article': article_ref(state['request'].removed_article)}


def web_search(state, engine):
    sim, src = state['similarity'], state['source']
    leads = [*sim.hits]
    if src.live_source:
        leads.append(src.live_source)
    leads.extend(src.extra_hits)
    return {'article': article_ref(state['request'].removed_article),
            'leads': [{'url': hit.url, 'title': hit.title,
                       'source': 'cited' if hit is src.live_source else hit.source}
                      for hit in leads],
            'prefetched': {'live': len(sim.fetched.live) + bool(src.live_source),
                           'dead': len(sim.fetched.dead),
                           'unreadable': len(sim.fetched.unreadable),
                           'excluded_self': len(sim.fetched.excluded_self),
                           'excluded_variant': len(sim.fetched.excluded_variant),
                           'excluded_redirect_self': len(sim.fetched.excluded_redirect_self)},
            'rewrite_url': sim.rewrite.url if sim.rewrite else None,
            'dead_source_url': src.dead_source.url if src.dead_source else None}


BUILDERS = {fn.__name__: fn for fn in
            (classification, similarity, relevance, related_removals, source, web_search)}


def build(name, state, engine):
    if state.get('stopped'):
        return {'skipped_because': state['stopped']}
    if engine.synthetic:
        return {'skipped_because': 'model is disabled; audit did not run'}
    return bounded(BUILDERS[name](state, engine))


def observed_related(inp, updates, events):
    """Record archive-derived inputs after the read, using its actual clock/window."""
    related = updates.get('related')
    if related is None or related.anchor_ts is None:
        return
    inp.update(anchor_ts=related.anchor_ts, anchor_source=related.anchor_source)
    if related.prompt_publish_date:
        inp['publish_date_in_prompt'] = related.prompt_publish_date
    for event in events:
        if event['type'] != 'archive_get':
            continue
        if event.get('path') == '/audits':
            params = event.get('params') or {}
            inp['window'] = {key: params.get(key) for key in ('date_from', 'date_to')}
            break
