"""Recording projections; never replace the values consumed by the workflow."""
from contextvars import ContextVar
import hashlib
import json

from audit.jsonsafe import json_safe


record_full_evidence = ContextVar('record_full_evidence', default=False)


def text_summary(text):
    return {'chars': len(text),
            'sha256': hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()}


def summary(value):
    """Hash compact, sorted UTF-8 JSON, with non-finite numbers normalized to null."""
    body = json.dumps(json_safe(value), sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8', 'surrogatepass')
    return {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}


SCALAR_MAX_CHARS = 300


def _scalar(value):
    """A short scalar as is; anything else (an object, a long string) only as size and hash."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and len(value) <= SCALAR_MAX_CHARS:
        return value
    return {'summary': text_summary(value) if isinstance(value, str) else summary(value)}


def archive_output(path, body):
    if record_full_evidence.get():
        return body
    result = summary(body)
    if path == '/audits' and isinstance(body, dict):
        result['items'] = len(body.get('items') or [])
    elif (path == '/articles' or path.startswith('/articles/')) and isinstance(body, dict):
        if 'id' in body:
            result.update({key: _scalar(body.get(key)) for key in ('id', 'title', 'publish_date')})
            result['url'] = _scalar(body.get('link') or body.get('url'))
        if isinstance(body.get('news_text'), str):
            result['news_text'] = text_summary(body['news_text'])
    return {'summary': result}


def page_summary(page):
    result = {key: page[key] for key in
              ('url', 'final_url', 'status', 'title', 'reason') if key in page}
    if 'text' in page:
        result['text'] = text_summary(page['text'])
    return result


def fetch_input(args, kwargs, kind):
    if record_full_evidence.get():
        return {'args': list(args), 'kwargs': kwargs}
    if kind == 'probe_removed_url':
        return {'urls': [args[0] if args else kwargs['url']]}
    hits = args[0] if args else kwargs.get('hits', [])
    return {'urls': [hit.url for hit in hits],
            'exclude_url': kwargs.get('exclude_url', args[1] if len(args) > 1 else None)}


def fetch_output(body):
    if record_full_evidence.get() or body is None:
        return body
    if 'url' in body:
        return page_summary(body)
    return {key: [page_summary(page) for page in value]
            if key in ('live', 'dead', 'unreadable') else value
            for key, value in body.items()}
