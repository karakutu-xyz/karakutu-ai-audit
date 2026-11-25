"""Optional, GET-only archive integration; never writes to the archive."""
from typing import Literal

import httpx
from audit.clients.models import ArticleRecord, SimilarArticlesResult
from audit.clients.concepts import Concept
from audit.model import trace
from audit.evidence import archive_output
from audit.clients.auth import ArweaveWalletAuth

ArchiveState = Literal[
    "disabled", "unchecked", "ok", "wallet_missing", "wallet_unreadable",
    "auth_unavailable", "unreachable", "rejected",
]


class KarakutuClient:
    def __init__(self, base_url="", wallet_path="", wallet_json=""):
        self.base_url = base_url.rstrip("/")
        self.enabled = bool(self.base_url)
        self.http = httpx.AsyncClient(timeout=15)
        self.auth = ArweaveWalletAuth(wallet_path=wallet_path, wallet_json=wallet_json, base_url=self.base_url) if wallet_path or wallet_json else None

        self.state: ArchiveState = (
            (self.auth.wallet_state if self.auth else None) or "unchecked"
        ) if self.enabled else "disabled"

    def _record_state(self, event):
        """Project call outcomes to fixed public labels, never upstream messages."""
        if event['error_type'] == 'AuthenticationUnavailable':
            self.state = (getattr(self.auth, 'wallet_state', None)
                          or getattr(self.auth, 'failure_kind', None) or 'auth_unavailable')
        elif event['error_type'] == 'AuthenticationRejected':
            self.state = 'rejected'
        elif event['error_type']:
            status = event.get('retry_status') or event['status']
            self.state = ('rejected' if event['error_type'] == 'HTTPStatusError'
                          and status in (401, 403) else 'unreachable')
        else:
            self.state = 'ok'

    async def close(self):
        await self.http.aclose()

    async def _get(self, path, params=None):
        if not self.enabled:
            return None
        event = {'type':'archive_get', 'path':path, 'params':params, 'output':None, 'status':None, 'error_type':None}
        events = trace.get()
        if events is not None:
            events.append(event)
        token = await self.auth.get_token() if self.auth else None
        # Explicit wallet configuration must fail closed, rather than silently becoming public.
        if self.auth and not token:
            event['error_type'] = 'AuthenticationUnavailable'
            self._record_state(event)
            return None
        completed = True
        try:
            headers = {'Authorization': f'Bearer {token}'} if token else {}
            result = await self.http.get(self.base_url + path, params=params, headers=headers)
            event['status'] = result.status_code
            if result.status_code in (401, 403) and self.auth:
                token = await self.auth.get_token(force=True, rejected_token=token)
                if not token:
                    event['error_type'] = (
                        'AuthenticationRejected' if getattr(self.auth, 'refresh_throttled', False)
                        else 'AuthenticationUnavailable'
                    )
                    return None
                result = await self.http.get(self.base_url + path, params=params, headers={'Authorization': f'Bearer {token}'})
                event['retry_status'] = result.status_code
            result.raise_for_status()
            body = result.json()
            try:
                event['output'] = archive_output(path, body)
            except Exception as exc:  # noqa: BLE001 — recording never changes the read
                event['output'] = {'summary': {'record_error': type(exc).__name__}}
            return body
        except (httpx.HTTPError, ValueError) as exc:
            event['error_type'] = type(exc).__name__
            return None
        except BaseException:
            # A cancelled or interrupted read has no outcome: keep the previous state.
            completed = False
            raise
        finally:
            if completed:
                self._record_state(event)

    async def get_article_by_url(self, url):
        body = await self._get('/articles', {'url': url})
        try:
            return ArticleRecord.model_validate(body)
        except Exception:
            return None

    async def find_similar_by_url(self, url, limit=5):
        article = await self.get_article_by_url(url)
        if not article:
            return []
        body = await self._get(f'/articles/{article.id}/similar', {'limit': limit, 'date_range_days': 30})
        try:
            return SimilarArticlesResult.model_validate(body).articles
        except Exception:
            return []

    async def list_concepts(self, scheme, locale='en-GB'):
        body = await self._get('/concepts', {'scheme': scheme, 'locale': locale}) or {}
        try:
            return [Concept.model_validate(x) for x in body.get('concepts', [])]
        except Exception:
            return []

    async def first_dead_check(self, article_id):
        body = await self._get(f'/articles/{article_id}', {'expand': ['checks']}) or {}
        return first_dead_check_date(body.get('checks') or {})

    async def get_article(self, article_id):
        body = await self._get(f'/articles/{article_id}', {'expand': ['checks', 'versions']})
        try:
            return ArticleRecord.model_validate(body)
        except Exception:
            return None

    async def audits_created_between(self, date_from, date_to, statuses=(0, 1, 2, 3, 4)):
        rows, any_ok = [], False
        for status in statuses:
            for page in range(1, 31):
                body = await self._get('/audits', {'view':'public','status':status,'date_from':date_from,'date_to':date_to,'page':page,'page_size':100,'sort_by':'created_at','sort_order':'desc'})
                if body is None:
                    break
                any_ok = True
                items = body.get('items') or []
                for item in items:
                    art = item.get('article') or {}
                    rows.append({'audit_id':item.get('id'),'article_id':art.get('id'),'url':art.get('link') or '', 'title':art.get('title') or '', 'newspaper':art.get('newspaper') or '', 'status':item.get('status',status),'decision':item.get('decision'),'created_at':item.get('created_at'),'publish_date':art.get('publish_date')})
                if len(items) < 100:
                    break
        return rows if any_ok else None

def first_dead_check_date(checks):
    """First dead check after the last successful check (shared archive semantics)."""
    entries = sorted((e for e in (checks or {}).get('entries', []) if e.get('check_date')),
                     key=lambda e: e['check_date'])
    first = None
    for entry in entries:
        if entry.get('status') == 200:
            first = None
        elif entry.get('status') in (404, 410, 451) and first is None:
            first = entry['check_date']
    return first


def get_client():
    raise RuntimeError('Archive client must be injected')
