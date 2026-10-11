"""Re-export shim — the discussion-comment templates now live in `src/archive_flow/comments.py`
(moved verbatim on 2026-10-05; only its import of the rules changed).

Kept so the operator scripts that import `comment_templates` by bare name keep working. Edit the
templates in `src/archive_flow/comments.py`, never here. NOTE: patching a name on this shim (e.g.
`_stamp`) does not reach `render`; patch `src.archive_flow.comments` instead.
"""
import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:   # some scripts put only scripts/open_audit on sys.path
    sys.path.insert(0, _REPO)

from src.archive_flow.comments import *  # noqa: E402,F401,F403
from src.archive_flow.comments import (  # noqa: E402,F401 — underscored / explicit names star-import skips
    BOT, BOT_PREFIX, C_HEAD, DISCLAIMER, TAGS_PROPOSED, TAGS_PROPOSED_EXISTING, TAGS_SET, TAGS_WORDING, TEMPLATES,
    TITLES, VERDICT_WORDS, _flags, _mark, _pct, _stages, _stamp, _verdict, evidence_urls, iptc_body, iptc_line,
    related_line, render, render_A, render_B, render_C, render_D, scope_line, similar_line, source_line, web_line,
)
