"""Apply ONE run record to the archive: IPTC tags onto the audit, one discussion comment.

Moved from the per-record body of `scripts/open_audit/apply_results.py` (2026-10-05). Same guard
order, same pacing, same ledger entry shape (`{"tags": {ok, at, payload}, "comment": {ok, at,
template, tags_state, discussion_id, view_url}}`), same printed wording (through `say`):

  usable result → live `GET /audits/{id}` (missing → skip) → eligible status (else skip)
  → tags: `rules.tag_decision` → PATCH → ledger write → pacing
  → comment: `rules.tags_state_for_comment` → `comments.render` → `count_discussions`
    → `rules.comment_decision` → POST → ledger write → pacing

Three additions, all about the comment never claiming the wrong thing, neither
touching the normal path's wording rules:

  * WRITE-AHEAD MARK. Before the PATCH the ledger gets `tags = {ok: False, pending: True, at,
    payload}` and is persisted. A crash between the PATCH and the ledger write used to leave no
    trace, so the re-run saw tags on the audit, skipped, and worded the comment PROPOSED ONLY
    although the bot had set them. On the next apply a pending mark is RECONCILED first: when the
    live audit holds exactly the pending payload the write landed (`ok: True`, `reconciled:
    True`); otherwise the mark is dropped and the entry reads as a write that did not land.
  * READ-AFTER-WRITE. `update_audit_classification` returns False on ANY transport failure, even
    one after the server committed. On a False the audit is re-read; when it now holds exactly
    the payload the write is recorded as ok (`confirmed_by_read: True`).
  * HELD COMMENT (`hold_comment_on_tag_failure`, job worker only). When the tag write was
    attempted and did not land, the comment step is skipped (`CommentOutcome.held`) so the caller
    can retry the tags first; a comment posted now would word the codes as PROPOSED ONLY and be
    contradicted by a later successful retry. The CLI never passes it (its behaviour is unchanged).
"""
import asyncio
import copy
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.archive_flow.comments import render
from src.archive_flow.rules import (
    ELIGIBLE_STATUSES, TAG_FIELDS, TAGS_SET, Decision, comment_decision, has_any_tag, is_eligible,
    result_usable, status_label, tag_decision, tags_state_for_comment,
)

DEFAULT_FRONTEND_URL = "https://karakutu.xyz"
WRITE_PACING_S = 0.5


def classification_payload(r):
    c = r.get("classification") or {}
    return {
        "subject_tag_notations": [t["notation"] for t in (c.get("subject_tags") or [])],
        "content_type_notation": (c.get("genre") or {}).get("notation"),
        "geographic_scope": c.get("geographic_scope"),
    }


def view_url(audit_id, article_url, frontend_url: str = DEFAULT_FRONTEND_URL) -> str:
    return f"{frontend_url.rstrip('/')}/audits/{audit_id}/discussions#/news/{article_url}"


def live_tags_match(audit: Optional[dict], payload: dict) -> bool:
    """True when the audit holds exactly `payload` in its three IPTC fields — the equality test
    of `rules.retag_decision` (a missing tag list equals an empty one)."""
    if not audit:
        return False
    return ((audit.get("subject_tag_notations") or []) == (payload.get("subject_tag_notations") or [])
            and audit.get("content_type_notation") == payload.get("content_type_notation")
            and audit.get("geographic_scope") == payload.get("geographic_scope"))


@dataclass
class TagsOutcome:
    decision: Optional[Decision] = None   # None when the record never reached the tag step
    requested: bool = False               # --tags / the job worker (always)
    payload: Optional[dict] = None
    attempted: bool = False               # a PATCH was sent
    written: bool = False


@dataclass
class CommentOutcome:
    decision: Optional[Decision] = None
    requested: bool = False
    tags_state: Optional[str] = None
    text: Optional[str] = None
    chars: int = 0
    discussions: Optional[int] = None
    attempted: bool = False               # a POST was sent
    posted: bool = False
    discussion_id: Optional[int] = None
    view_url: Optional[str] = None
    held: bool = False                    # skipped because the tag write did not land (see module doc)


@dataclass
class ApplyOutcome:
    audit_id: int
    stage: str                     # "unusable" | "missing" | "ineligible" | "processed"
    reason: str
    dry_run: bool
    audit: Optional[dict] = None
    reconciled_pending: Optional[bool] = None   # None: no pending mark; True/False: it landed / did not
    tags: TagsOutcome = field(default_factory=TagsOutcome)
    comment: CommentOutcome = field(default_factory=CommentOutcome)

    @property
    def proposed_wording(self) -> bool:
        """The comment (rendered) words the IPTC codes as PROPOSED ONLY."""
        return self.comment.tags_state is not None and self.comment.tags_state != TAGS_SET


async def _call(persist):
    res = persist()
    if inspect.isawaitable(res):
        await res


def _quiet(_msg: str) -> None:
    return None


def _no_persist() -> None:
    return None


async def reconcile_pending_tags(client, audit_id, ledger_entry: dict, persist, *,
                                 audit: Optional[dict] = None) -> Optional[bool]:
    """Resolve a write-ahead tag mark left by an interrupted apply. None when there is none."""
    t = ledger_entry.get("tags") or {}
    if not t.get("pending") or t.get("ok"):
        return None
    if audit is None:
        audit = await client.get_audit(audit_id)
    landed = live_tags_match(audit, t.get("payload") or {})
    entry = {k: v for k, v in t.items() if k != "pending"}
    if landed:
        entry.update(ok=True, reconciled=True)
    ledger_entry["tags"] = entry
    await _call(persist)
    return landed


async def apply_record(client, record: dict, *, ledger_entry: dict, persist: Callable,
                       template: str = "C", do_tags: bool = True, do_comments: bool = True,
                       dry_run: bool = True, pacing_s: float = WRITE_PACING_S,
                       frontend_url: str = DEFAULT_FRONTEND_URL,
                       say: Optional[Callable[[str], None]] = None,
                       hold_comment_on_tag_failure: bool = False) -> ApplyOutcome:
    """Apply one record. Mutates `ledger_entry` in place and calls `persist()` (sync or async)
    right after each write, as the script saved its log after each write.

    `say` receives the exact lines `apply_results.py` printed (the CLI passes `print`).
    """
    say = say or _quiet
    aid = record["audit_id"]
    usable = result_usable(record)
    if not usable.write:
        say(f"\n  {aid}: SKIP — {usable.reason}")
        return ApplyOutcome(aid, "unusable", usable.reason, dry_run)
    entry = ledger_entry
    audit = await client.get_audit(aid)
    if audit is None:
        say(f"  {aid}: SKIP — audit not found (deleted/merged) or archive unreachable")
        return ApplyOutcome(aid, "missing", "audit not found (deleted/merged) or archive unreachable", dry_run)
    out = ApplyOutcome(aid, "processed", "eligible", dry_run, audit=audit)
    status, decision = audit.get("status"), audit.get("decision")
    say(f"\n  {aid}: status={status_label(status)} decision={decision} existing_tags={has_any_tag(audit)} "
        f"verdict={record.get('verdict')}")
    if not is_eligible(audit):
        say(f"    SKIP — status outside the eligible set {sorted(ELIGIBLE_STATUSES)} (standing rule); left alone")
        out.stage, out.reason = "ineligible", f"status {status_label(status)} outside the eligible set"
        return out
    if (entry.get("tags") or {}).get("pending"):
        if dry_run:   # decide on a reconciled COPY; a dry run never writes the ledger
            entry = copy.deepcopy(entry)
            out.reconciled_pending = await reconcile_pending_tags(client, aid, entry, _no_persist, audit=audit)
        else:
            out.reconciled_pending = await reconcile_pending_tags(client, aid, entry, persist, audit=audit)
    payload = classification_payload(record)

    # ---- tags (policy: rules.tag_decision) ----------------------------------------------
    td = tag_decision(audit, payload, entry)
    out.tags = TagsOutcome(decision=td, requested=do_tags, payload=payload)
    if do_tags:
        if td.write:
            say(f"    tags: PATCH /audits/{aid} {json.dumps(payload, ensure_ascii=False)}")
            if not dry_run:
                entry["tags"] = {"ok": False, "pending": True, "at": time.time(), "payload": payload}
                await _call(persist)                                   # write-ahead mark
                out.tags.attempted = True
                ok = await client.update_audit_classification(aid, **payload)
                confirmed = False
                if not ok:   # ambiguous: a transport failure can follow a committed PATCH
                    confirmed = live_tags_match(await client.get_audit(aid), payload)
                    ok = confirmed
                entry["tags"] = {"ok": ok, "at": time.time(), "payload": payload}
                if confirmed:
                    entry["tags"]["confirmed_by_read"] = True
                await _call(persist)
                say(f"    tags: {'written' if ok else 'FAILED'}")
                out.tags.written = bool(ok)
                await asyncio.sleep(pacing_s)
        else:
            say(f"    tags: SKIP — {td.reason}")

    # ---- comment (policy: rules.comment_decision) -----------------------------------------
    if do_comments and hold_comment_on_tag_failure and out.tags.attempted and not out.tags.written:
        say("    comment: HELD — the tag write did not land; retried before any comment")
        out.comment = CommentOutcome(requested=True, held=True)
    elif do_comments:
        # Wording follows the ACTUAL tag outcome (the ledger), or the projected one in a dry run.
        state = tags_state_for_comment(audit, entry, projected_write=(do_tags and td.write), dry_run=dry_run)
        comment = render(template, record, tags_state=state)
        n = await client.count_discussions(aid)
        cd = comment_decision(audit, n, comment, entry)
        out.comment = CommentOutcome(decision=cd, requested=True, tags_state=state, text=comment,
                                     chars=len(comment), discussions=n)
        if cd.write:
            say(f"    comment: POST /audits/{aid}/discussions ({len(comment)} chars, template {template}, IPTC wording: {state})")
            say("    " + comment.replace("\n", "\n    "))
            if not dry_run:
                out.comment.attempted = True
                posted = await client.post_discussion(aid, comment)
                url = view_url(aid, record["url"], frontend_url)
                entry["comment"] = {"ok": posted is not None, "at": time.time(), "template": template,
                                    "tags_state": state, "discussion_id": (posted or {}).get("id"),
                                    "view_url": url}
                await _call(persist)
                say(f"    comment: {'posted id ' + str(posted.get('id')) if posted else 'FAILED'}")
                out.comment.posted = posted is not None
                out.comment.discussion_id = (posted or {}).get("id")
                out.comment.view_url = url
                await asyncio.sleep(pacing_s)
        else:
            say(f"    comment: SKIP — {cd.reason}")
    return out


__all__ = ["ApplyOutcome", "CommentOutcome", "TagsOutcome", "DEFAULT_FRONTEND_URL", "TAG_FIELDS",
           "WRITE_PACING_S", "apply_record", "classification_payload", "live_tags_match",
           "reconcile_pending_tags", "view_url"]
