"""Live-apply policy for the archive — the operator's standing rules, in one place, as pure
functions so they can be unit-tested and cannot drift between runs.

Rules (operator, 2026-09-15 — GENERAL, not run-specific):

  * ELIGIBLE STATUSES — the bot works on audits in status 0 (screening), 2 (under
    investigation) and 3 (ready to publish) ONLY (operator, 2026-09-18; supersedes the 0/2/4
    wording of 2026-09-15). Every other status (1 claimed, 4 published, anything new) is left
    alone: no tags, no comment.
  * TAGS — the IPTC classification is written only when NONE of the audit's three tag fields
    (subject_tag_notations, content_type_notation, geographic_scope) is set. One set field is
    enough to stop the write, and there is no override flag. In that case the bot's proposed
    codes go into the comment instead, marked as proposed and not written.
  * COMMENT — one discussion comment per eligible audit, only when the audit has ZERO existing
    discussions (an unknown count never counts as zero) and the text fits the API's 10,000 chars.
  * IDEMPOTENCY — a write recorded as ok in the apply log is never repeated.
  * USABLE RESULTS ONLY — a record whose verdict is AUDIT_FAILED or ERROR (or that has no
    response) is not a result (AGENTS.md rule 3: the audit must be re-run); neither tags nor a comment
    are derived from it. Re-run with run_open_cases.py --retry-failed first.

The comment must never claim the codes were SET when they were not: `tags_state_for_comment`
decides the wording from the actual outcome (or, in a dry run, the projected one).
"""
from dataclasses import dataclass
from typing import Optional

ELIGIBLE_STATUSES = frozenset({0, 2, 3})
STATUS_NAMES = {0: "screening", 1: "claimed", 2: "under investigation", 3: "ready to publish", 4: "published"}
TAG_FIELDS = ("subject_tag_notations", "content_type_notation", "geographic_scope")
MAX_COMMENT_CHARS = 10_000

# wording states for the comment's IPTC line (see comment_templates.iptc_line)
TAGS_SET = "set"                            # written onto the audit by the bot (now or in an earlier run)
TAGS_PROPOSED_EXISTING = "proposed_existing"  # not written: the audit already carried tags
TAGS_PROPOSED = "proposed"                  # not written for any other reason (e.g. tags not enabled)


@dataclass(frozen=True)
class Decision:
    action: str   # "write" | "skip"
    reason: str

    @property
    def write(self) -> bool:
        return self.action == "write"


UNUSABLE_VERDICTS = frozenset({"AUDIT_FAILED", "ERROR", None, ""})


def result_usable(record: dict) -> Decision:
    """A run record may drive writes only if the audit actually completed."""
    v = (record.get("response") or {}).get("verdict") or record.get("verdict")
    if v in UNUSABLE_VERDICTS or not record.get("response"):
        return Decision("skip", f"verdict {v or 'missing'} is not a result — re-run the audit first")
    return Decision("write", f"verdict {v}")


def status_of(audit: dict) -> Optional[int]:
    return audit.get("status")


def status_label(status) -> str:
    return f"{status} ({STATUS_NAMES.get(status, 'unknown')})"


def is_eligible(audit: dict) -> bool:
    return status_of(audit) in ELIGIBLE_STATUSES


def has_any_tag(audit: dict) -> bool:
    """True when ANY of the three IPTC fields carries a value — enough to stop the tag write."""
    return any(audit.get(f) for f in TAG_FIELDS)


def has_classification(payload: dict) -> bool:
    return bool(payload.get("subject_tag_notations") or payload.get("content_type_notation")
                or payload.get("geographic_scope"))


def _ineligible(audit: dict) -> Decision:
    return Decision("skip", f"status {status_label(status_of(audit))} is outside the eligible set "
                            f"{sorted(ELIGIBLE_STATUSES)} — left alone")


def tag_decision(audit: dict, payload: dict, log_entry: dict) -> Decision:
    if (log_entry.get("tags") or {}).get("ok"):
        return Decision("skip", "already written (apply log)")
    if not is_eligible(audit):
        return _ineligible(audit)
    if not has_classification(payload):
        return Decision("skip", "nothing to write (no classification)")
    if has_any_tag(audit):
        return Decision("skip", f"audit already carries tags {audit.get('subject_tag_notations')} / "
                                f"{audit.get('content_type_notation')} / {audit.get('geographic_scope')} — "
                                "never overwritten; the bot's proposal goes into the comment instead")
    return Decision("write", "no IPTC field set on the audit")


def comment_decision(audit: dict, n_discussions: Optional[int], comment: str, log_entry: dict) -> Decision:
    if (log_entry.get("comment") or {}).get("ok"):
        return Decision("skip", "already posted (apply log)")
    if not is_eligible(audit):
        return _ineligible(audit)
    if n_discussions is None:
        return Decision("skip", "could not read the discussion count (unknown is not zero)")
    if n_discussions > 0:
        return Decision("skip", f"audit already has {n_discussions} discussion(s)")
    if len(comment) > MAX_COMMENT_CHARS:
        return Decision("skip", f"{len(comment)} chars exceeds the {MAX_COMMENT_CHARS:,} limit")
    return Decision("write", "eligible, no discussions yet")


def tags_state_for_comment(audit: dict, log_entry: dict, *, projected_write: bool, dry_run: bool) -> str:
    """How the comment may describe the IPTC codes.

    `set` only when the write actually happened (apply log) — or, in a DRY RUN, when it is
    projected to happen; otherwise `proposed_existing` (the audit already had tags) or `proposed`.
    """
    if (log_entry.get("tags") or {}).get("ok"):
        return TAGS_SET
    if dry_run and projected_write:
        return TAGS_SET
    return TAGS_PROPOSED_EXISTING if has_any_tag(audit) else TAGS_PROPOSED


def retag_decision(audit: dict, previous_payload: dict, new_payload: dict, log_entry: dict) -> Decision:
    """May the bot REPLACE tags it wrote earlier with a corrected list (2026-10-01 indexing rules)?

    Only when the archive still holds EXACTLY what the bot wrote last time — a human edit of any of
    the three fields means the audit is theirs now and is left alone — and only when the new list
    actually differs. The never-overwrite rule for human tags is untouched: this is the bot
    correcting itself.
    """
    if (log_entry.get("retag") or {}).get("ok"):
        return Decision("skip", "already re-tagged (apply log)")
    if not is_eligible(audit):
        return _ineligible(audit)
    if not has_classification(new_payload):
        return Decision("skip", "nothing to write (no new classification)")
    current = {f: audit.get(f) for f in TAG_FIELDS}
    before = {f: previous_payload.get(f) for f in TAG_FIELDS}
    if (current.get("subject_tag_notations") or []) != (before.get("subject_tag_notations") or []) \
            or current.get("content_type_notation") != before.get("content_type_notation") \
            or current.get("geographic_scope") != before.get("geographic_scope"):
        return Decision("skip", "the archive no longer holds what the bot wrote — a human changed the tags; left alone")
    after = {f: new_payload.get(f) for f in TAG_FIELDS}
    if (after.get("subject_tag_notations") or []) == (before.get("subject_tag_notations") or []) \
            and after.get("content_type_notation") == before.get("content_type_notation") \
            and after.get("geographic_scope") == before.get("geographic_scope"):
        return Decision("skip", "the corrected list is identical to what is already written")
    return Decision("write", "archive holds the bot's previous list; corrected list differs")

