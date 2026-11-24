"""Same-site paragraph furniture, learned only from other live articles.

Callers select live pages on the removed article's publisher and exclude its
own URL; requested and final URLs must both stay on that publisher. The removed
article never votes. Independence uses whole-page shingles absent from both the
removed article and the other voter, so reflowed copies cannot vote each other's
body out. Both voters need 30 own shingles and half the distinct shingles within
their shared paragraph keys; shared volume is symmetric, including repeated lines.

If learned keys cover more than 50% of the removed article's tokens, measurement
keeps BOTH raw scores and records the cap. This also protects against copies with
arbitrarily long distinct teasers voting a majority of the body into furniture.
Residual limitation: unrelated articles quoting a lede below the cap can still
vote it into furniture. In the invented regression a genuine partial copy falls
from 71/223 (31.84%) to 33/185 (17.84%) old-in-new; with rewritten material its
reverse score is 13.04%, so it is demoted below the 20% sibling bar. The cap cannot
protect every partial copy or prevent every score increase below its limit.
"""

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

from audit.search.textmatch import directional_containments, shingles, tokens


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


def _strip(text: str, boilerplate: frozenset[str]) -> tuple[str, int, int]:
    kept = []
    count = 0
    removed_tokens = 0
    for line, key, token_count in _paragraphs(text):
        if key and key in boilerplate:
            count += 1
            removed_tokens += token_count
        else:
            kept.append(line)
    return "\n".join(kept), count, removed_tokens


def measure(
    removed_prose: str, page_text: str, boilerplate: frozenset[str],
) -> Measurement:
    """Strip only when learned keys cover at most half the removed tokens.

    An empty cleaned candidate measures zero when the removed article still
    has tokens. An empty boilerplate set preserves raw no-evidence behaviour.
    """
    raw = directional_containments(removed_prose, page_text)
    if not boilerplate:
        return Measurement(*raw, *raw, (0, 0))
    old, old_count, removed_tokens = _strip(removed_prose, boilerplate)
    new, new_count, _ = _strip(page_text, boilerplate)
    total_tokens = removed_tokens + len(tokens(old))
    if removed_tokens > MAX_REMOVED_SHARE * total_tokens:
        return Measurement(*raw, *raw, (old_count, new_count), boilerplate_capped=True)
    if raw[0] is None:
        # An unmeasurable side stays unmeasurable: no evidence, no call.
        return Measurement(*raw, *raw, (0, 0))
    if not tokens(new):
        cleaned = (0.0, 0.0)
    else:
        cleaned = directional_containments(old, new)
    return Measurement(*cleaned, *raw, (old_count, new_count))
