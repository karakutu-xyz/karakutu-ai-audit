"""Build one run case (the `cases.json` row `run_open_cases.py` / `runner.run_case` consume) from an
audit record — the body recovered from the archive.

Extracted from `scripts/open_audit/fetch_listed_cases.py::one`. Behaviour is identical:

  * url: the audit's embedded `article.link`, else the list row's `url`;
  * `get_article_with_body(url)`, retried ONCE after 3 s when it returns nothing;
  * body = `longest_body(record)` (the longest archived snapshot, falling back to the top level);
  * a body shorter than `MIN_BODY_CHARS` is skipped (`thin_or_missing_body`);
  * every field keeps the script's precedence (archive record → audit's article → list row).

`MIN_BODY_CHARS` is a constant, not configuration: it decides WHICH audits get results.
"""
import asyncio
import contextlib
from dataclasses import dataclass
from typing import Optional, Union

from src.clients.models import longest_body
from src.search.urls import host_of

MIN_BODY_CHARS = 400
FETCH_RETRY_SLEEP_S = 3
LIST_FIELDS = ("publisher", "list_state", "published", "detection_window")


@dataclass(frozen=True)
class Skip:
    """build_case declined the audit. `reason` is a stable code (the scripts' skip-count key)."""
    reason: str
    body_chars: int = 0


async def build_case(client, *, audit: Optional[dict], list_row: Optional[dict] = None,
                     discussions: Optional[int] = None, min_chars: int = MIN_BODY_CHARS,
                     limiter: Optional[asyncio.Semaphore] = None,
                     retry_sleep_s: float = FETCH_RETRY_SLEEP_S) -> Union[dict, Skip]:
    """One case dict, or a `Skip`.

    `audit` is the `GET /audits/{id}` record (or `{}` when only the list row is known — the
    script's "no probe" path); `list_row` the operator's list row (audit_id, url, title,
    descriptive columns); `discussions` the discussion count read with the audit, carried into
    `case["probe"]`. `limiter` bounds concurrent archive fetches (held across the retry, as in
    the script).
    """
    row = list_row or {}
    audit = audit or {}
    art = audit.get("article") or {}
    aid = row.get("audit_id", audit.get("id"))
    url = art.get("link") or row.get("url")
    if not url:
        return Skip("no_url")
    async with (limiter if limiter is not None else contextlib.nullcontext()):
        rec = await client.get_article_with_body(url)
        if rec is None:
            await asyncio.sleep(retry_sleep_s)
            rec = await client.get_article_with_body(url)
    body = longest_body(rec) if rec else ""
    if len(body) < min_chars:
        return Skip("thin_or_missing_body", len(body))
    return {
        "audit_id": aid, "article_id": (rec.id if rec else None) or art.get("id"), "url": url,
        "title": (rec.title if rec and rec.title else None) or art.get("title") or row.get("title") or "",
        "newspaper": (rec.newspaper.slug if rec and rec.newspaper and rec.newspaper.slug else None)
                     or art.get("newspaper") or host_of(url) or "",
        "newspaper_country": art.get("newspaper_country"),
        "publish_date": (rec.publish_date if rec and rec.publish_date else None) or art.get("publish_date"),
        "http_status": art.get("status"), "created_at": audit.get("created_at"),
        "list": {k: row.get(k) for k in LIST_FIELDS},
        "probe": {"status": audit.get("status"), "decision": audit.get("decision"),
                  "existing_tags": bool(audit.get("subject_tag_notations") or audit.get("content_type_notation")
                                        or audit.get("geographic_scope")),
                  "subject_tag_notations": audit.get("subject_tag_notations"),
                  "discussions": discussions},
        "content": body, "content_chars": len(body),
    }
