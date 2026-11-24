"""Same-site paragraph furniture, learned only from other live articles.

Callers select live pages on the removed article's publisher and exclude its
own URL. The removed prose never supplies a vote for boilerplate: a relocated
copy sharing that prose is evidence, not furniture.
"""

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

from audit.search.textmatch import directional_containments, tokens


BOILERPLATE_MIN_OWN_TOKENS = 30
MIN_PARAGRAPH_TOKENS = 4
# Each page of a distinct pair must also carry at least this many own tokens per shared
# token. Two copies of one article that each add a teaser line share far more than they
# add, so they never count as distinct and cannot vote the article body into
# boilerplate. Unrelated articles share only furniture (about 7-9% of a real page).
OWN_PER_SHARED_MIN = 0.5
Containments = tuple[float | None, float | None]


@dataclass(frozen=True)
class Measurement:
    """Scores and stripped line counts only; never paragraph text or keys.

    Counts are ordered (removed article, candidate), including repeated lines.
    Raw scores remain available even when stripping makes a side unmeasurable.
    """

    old_in_new: float | None
    new_in_old: float | None
    raw_old_in_new: float | None
    raw_new_in_old: float | None
    boilerplate_paragraphs: tuple[int, int]

    @property
    def cleaned(self) -> Containments:
        return self.old_in_new, self.new_in_old

    @property
    def raw(self) -> Containments:
        return self.raw_old_in_new, self.raw_new_in_old


def _paragraphs(text: str) -> Iterable[tuple[str, str, int]]:
    for line in text.split("\n"):
        words = tokens(line)
        key = " ".join(words) if len(words) >= MIN_PARAGRAPH_TOKENS else ""
        yield line, key, len(words)


def build_boilerplate(
    removed_prose: str, other_page_texts: Iterable[str],
) -> frozenset[str]:
    """Union shared keys only across pairs with enough independent body text.

    Tokenization is linear in text size. Each page's keys and token counts are
    built once; pairwise set operations cost O(n²) over the small page family.
    Short lines cannot define furniture or contribute independent body tokens.
    """
    removed_keys = {key for _, key, _ in _paragraphs(removed_prose) if key}
    pages = []
    for text in other_page_texts:
        counts: dict[str, int] = {}
        for _, key, count in _paragraphs(text):
            if key:
                counts[key] = counts.get(key, 0) + count
        pages.append((set(counts), counts))

    boilerplate: set[str] = set()
    for (a, a_counts), (b, b_counts) in combinations(pages, 2):
        shared = a & b
        if not shared:
            continue
        a_own = sum(a_counts[key] for key in a - removed_keys - b)
        b_own = sum(b_counts[key] for key in b - removed_keys - a)
        shared_tokens = sum(a_counts[key] for key in shared)
        own = min(a_own, b_own)
        if own >= BOILERPLATE_MIN_OWN_TOKENS and own >= OWN_PER_SHARED_MIN * shared_tokens:
            boilerplate.update(shared)
    return frozenset(boilerplate)


def _strip(text: str, boilerplate: frozenset[str]) -> tuple[str, int]:
    kept = []
    count = 0
    for line, key, _ in _paragraphs(text):
        if key and key in boilerplate:
            count += 1
        else:
            kept.append(line)
    return "\n".join(kept), count


def measure(
    removed_prose: str, page_text: str, boilerplate: frozenset[str],
) -> Measurement:
    """Measure after stripping; fall back only when the removed text is empty.

    An empty cleaned candidate measures zero when the removed article still
    has tokens. An empty boilerplate set preserves raw no-evidence behaviour.
    """
    raw = directional_containments(removed_prose, page_text)
    if not boilerplate or raw[0] is None:
        # No boilerplate learned, or a side was unmeasurable to begin with: no evidence,
        # no call.
        return Measurement(*raw, *raw, (0, 0))
    old, old_count = _strip(removed_prose, boilerplate)
    new, new_count = _strip(page_text, boilerplate)
    if not tokens(old):
        cleaned = raw
    elif not tokens(new):
        cleaned = (0.0, 0.0)
    else:
        cleaned = directional_containments(old, new)
    return Measurement(*cleaned, *raw, (old_count, new_count))
