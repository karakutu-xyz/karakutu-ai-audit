"""Re-export shim — the live-apply rules now live in `src/archive_flow/rules.py` (moved verbatim
on 2026-10-05 so the article job worker and these scripts share ONE implementation).

Kept so the operator scripts that import `apply_rules` by bare name (sys.path hack) keep working.
Edit the rules in `src/archive_flow/rules.py`, never here.
"""
import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:   # some scripts put only scripts/open_audit on sys.path
    sys.path.insert(0, _REPO)

from src.archive_flow.rules import *  # noqa: E402,F401,F403
from src.archive_flow.rules import (  # noqa: E402,F401 — underscored / explicit names star-import skips
    ELIGIBLE_STATUSES, MAX_COMMENT_CHARS, STATUS_NAMES, TAG_FIELDS, TAGS_PROPOSED, TAGS_PROPOSED_EXISTING,
    TAGS_SET, UNUSABLE_VERDICTS, Decision, _ineligible, comment_decision, has_any_tag, has_classification,
    is_eligible, result_usable, retag_decision, status_label, status_of, tag_decision, tags_state_for_comment,
)
