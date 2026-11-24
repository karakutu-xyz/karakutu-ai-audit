"""Same-site paragraph furniture, learned only from other live articles.

Callers select live pages on the removed article's publisher and exclude its
own URL; requested and final URLs must both stay on that publisher. The removed
article never votes. Independence uses whole-page shingles absent from both the
removed article and the other voter, so reflowed copies cannot vote each other's
body out. Both voters need 30 own shingles and half the distinct shingles within
their shared paragraph keys; shared volume is symmetric, including repeated lines.

Scoring drops every shingle that touches a learned paragraph's tokens, matched as a
token sequence anywhere in the text, so a different paragraph break cannot make
identical text differ. If that drops more than 50% of the removed article's shingles, measurement keeps BOTH raw scores and records the cap. This also protects against copies with
arbitrarily long distinct teasers voting a majority of the body into furniture.
Residual limitation: unrelated articles quoting a lede below the cap can still
vote it into furniture. In the invented regression a genuine partial copy falls
from 71/223 (31.84%) to 33/185 (17.84%) old-in-new; with rewritten material its
reverse score is 13.04%, so it is demoted below the 20% sibling bar. The cap cannot
protect every partial copy or prevent every score increase below its limit.
"""

from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from typing import Iterable

from audit.search.textmatch import SHINGLE_K, containment, directional_containments, shingles, tokens


# Historical name: this now counts own SHINGLES, rather than paragraph tokens.
BOILERPLATE_MIN_OWN_TOKENS = 30
MIN_PARAGRAPH_TOKENS = 4
# Each voter must also have half as many own shingles as shared paragraph shingles.
OWN_PER_SHARED_MIN = 0.5
MAX_REMOVED_SHARE = 0.5
Containments = tuple[float | None, float | None]


@dataclass(frozen=True)
class Measurement:
    """Scores, matched line counts and cap status; never paragraph text or keys.

    Counts are ordered (removed article, candidate), including repeated lines.
    When capped, counts describe proposed removals; neither side is stripped.
    """

    old_in_new: float | None
    new_in_old: float | None
    raw_old_in_new: float | None
    raw_new_in_old: float | None
    boilerplate_paragraphs: tuple[int, int]
    boilerplate_capped: bool = False

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

    Text handling is linear per page/pair; the small page family has O(n²) pairs.
    Whole-page shingles ignore paragraph reflow. Only lines of at least four
    tokens define furniture; repeated lines do not inflate shared volume.
    """
    removed_shingles = shingles(removed_prose)
    pages = []
    for text in other_page_texts:
        keys = {key for _, key, _ in _paragraphs(text) if key}
        page_shingles = shingles(text)
        pages.append((keys, page_shingles, page_shingles - removed_shingles))

    boilerplate: set[str] = set()
    for (a, a_shingles, a_independent), (b, b_shingles, b_independent) in combinations(pages, 2):
        shared = a & b
        if not shared:
            continue
        own = min(len(a_independent - b_shingles), len(b_independent - a_shingles))
        if own < BOILERPLATE_MIN_OWN_TOKENS:
            continue
        shared_shingles = set()
        for key in shared:
            shared_shingles.update(shingles(key))
        if own >= OWN_PER_SHARED_MIN * len(shared_shingles):
            boilerplate.update(shared)
    return frozenset(boilerplate)


def _matched(text: str, boilerplate: frozenset[str]) -> int:
    """How many lines of `text` are learned boilerplate (for review details only)."""
    return sum(1 for _, key, _ in _paragraphs(text) if key and key in boilerplate)


@lru_cache(maxsize=64)
def _key_index(boilerplate: frozenset[str]) -> dict[tuple[str, ...], list[tuple[str, ...]]]:
    """Boilerplate token sequences indexed by their first (up to SHINGLE_K) tokens."""
    index: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for key in boilerplate:
        seq = tuple(key.split(" "))
        index.setdefault(seq[:SHINGLE_K], []).append(seq)
    return index


def _clean_shingles(text: str, boilerplate: frozenset[str]) -> tuple[frozenset, int]:
    """Shingles of `text` that touch no token of a boilerplate paragraph, and the share
    of the text's shingles removed.

    Boilerplate is found as a token sequence anywhere in the token stream, not as whole
    lines, so identical text loses identical shingles whatever its paragraph breaks (a
    footer run into the last body paragraph on one page and set apart on another). Each
    position is checked against the few sequences that start with its tokens, so the scan
    stays linear in practice and uses no regular expression.
    """
    toks = tokens(text)
    index = _key_index(boilerplate)
    marked = [False] * len(toks)
    heads = {len(head) for head in index}
    i = 0
    while i < len(toks):
        matched = 0
        for size in heads:
            for seq in index.get(tuple(toks[i:i + size]), ()):
                if len(seq) > matched and tuple(toks[i:i + len(seq)]) == seq:
                    matched = len(seq)
        if matched:
            for j in range(i, i + matched):
                marked[j] = True
            i += matched
        else:
            i += 1
    if not toks:
        return frozenset(), 0
    k = SHINGLE_K
    if len(toks) < k:
        windows = [(0, len(toks))]
    else:
        windows = [(i, i + k) for i in range(len(toks) - k + 1)]
    kept = frozenset(tuple(toks[a:b]) for a, b in windows if not any(marked[a:b]))
    return kept, len(shingles(text)) - len(kept)


def measure(
    removed_prose: str, page_text: str, boilerplate: frozenset[str],
) -> Measurement:
    """Score both sides on the shingles that touch no boilerplate token.

    Boilerplate is matched as token sequences, not whole lines, so identical text stays
    identical even when the two pages break paragraphs differently. Strips nothing when the boilerplate would
    remove more than MAX_REMOVED_SHARE of the removed article's shingles (the cap). An
    unmeasurable side stays unmeasurable; an empty cleaned candidate measures zero while
    the removed article keeps text; an empty cleaned removed article falls back to raw.
    """
    raw = directional_containments(removed_prose, page_text)
    if not boilerplate:
        return Measurement(*raw, *raw, (0, 0))
    counts = (_matched(removed_prose, boilerplate), _matched(page_text, boilerplate))
    old_clean, old_removed = _clean_shingles(removed_prose, boilerplate)
    if old_removed > MAX_REMOVED_SHARE * len(shingles(removed_prose)):
        return Measurement(*raw, *raw, counts, boilerplate_capped=True)
    if raw[0] is None:
        # A side unmeasurable to begin with stays unmeasurable: no evidence, no call.
        return Measurement(*raw, *raw, counts)
    new_clean, _ = _clean_shingles(page_text, boilerplate)
    if not old_clean:
        cleaned = raw
    elif not new_clean:
        cleaned = (0.0, 0.0)
    else:
        cleaned = (containment(old_clean, new_clean), containment(new_clean, old_clean))
    return Measurement(*cleaned, *raw, counts)
