"""Pure headline, distinctive-word and date checks for same-publisher rewrites.

These signals supplement sentence-level containment; they do not establish that
key facts survived. Callers must first verify a readable same-publisher page.
Every rewrite must stay at the requested page or a same-article URL variant.
"""

import re
from datetime import datetime
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel

from audit.search.textmatch import tokens
from audit.search.urls import same_article_variant


DISTINCTIVE_MIN_CHARS = 5
# Reworded reports retain distinctive vocabulary and part of their headline even
# when five-word phrases change. Requiring both shares within a short date window
# separates those reports from unrelated pages with a similar headline template.
# Short originals lack enough vocabulary for that comparison, so they require
# near-identical headlines in both directions and a tighter publication window.
# Short originals' live text must also contain the headline words. For BOTH rules,
# the requested page must not redirect to a different article or a landing page.
REWRITE_TITLE_MIN = 0.40
REWRITE_WORDS_MIN = 0.40
REWRITE_MAX_DAYS = 2
STUB_MAX_WORDS = 20
STUB_TITLE_MIN = 0.80
STUB_MAX_DAYS = 1


class RewriteSignal(BaseModel):
    """Numeric review evidence for a qualifying same-publisher rewrite."""

    title_overlap: float
    word_overlap: float | None
    own_words: int
    date_gap_days: int
    headline_in_text: float | None = None


def headline_words(title: str, url: str) -> frozenset[str]:
    """Use title words, falling back to the last non-empty URL path segment."""
    words = tokens(title)
    if words:
        return frozenset(words)
    segments = [part for part in urlsplit(url).path.split("/") if part]
    slug = re.sub(r"\W+", " ", unquote(segments[-1])) if segments else ""
    return frozenset(word for word in tokens(slug) if not word.isdigit())


def title_overlap(
    removed_title: str, removed_url: str, cand_title: str, cand_url: str,
) -> tuple[float | None, float | None]:
    """Shares of each headline's distinct words present in the other headline."""
    old = headline_words(removed_title, removed_url)
    new = headline_words(cand_title, cand_url)
    common = len(old & new)
    return (common / len(old) if old else None,
            common / len(new) if new else None)


def distinctive_words(text: str) -> frozenset[str]:
    return frozenset(word for word in tokens(text) if len(word) >= DISTINCTIVE_MIN_CHARS)


def word_overlap(own_words: frozenset[str], page_text: str) -> tuple[float | None, int]:
    """Look for the removed article's own words in the whole candidate page."""
    count = len(own_words)
    return (len(own_words & distinctive_words(page_text)) / count if count else None, count)


def date_gap_days(
    removed_published: datetime | None, cand_publish_date: str | None,
    archive_days: int | None,
) -> int | None:
    """Compare calendar dates, or use an explicitly supplied archive day gap."""
    if removed_published is not None and cand_publish_date:
        try:
            candidate = datetime.fromisoformat(cand_publish_date)
        except ValueError:
            pass
        else:
            return abs((removed_published.date() - candidate.date()).days)
    return abs(archive_days) if archive_days is not None else None


def is_rewrite(
    *, title_overlap: float | None, title_overlap_reverse: float | None,
    word_overlap: float | None, own_words: int, date_gap_days: int | None,
    headline_in_text: float | None, requested_url: str, final_url: str | None,
) -> bool:
    """Inclusive bars; an unknown publication gap never establishes a rewrite."""
    if date_gap_days is None or title_overlap is None:
        return False
    if final_url and not same_article_variant(requested_url, final_url):
        return False
    if own_words < STUB_MAX_WORDS:
        return (title_overlap >= STUB_TITLE_MIN
                and title_overlap_reverse is not None
                and title_overlap_reverse >= STUB_TITLE_MIN
                and headline_in_text is not None
                and headline_in_text >= STUB_TITLE_MIN
                and date_gap_days <= STUB_MAX_DAYS)
    return (word_overlap is not None and word_overlap >= REWRITE_WORDS_MIN
            and title_overlap >= REWRITE_TITLE_MIN
            and date_gap_days <= REWRITE_MAX_DAYS)
