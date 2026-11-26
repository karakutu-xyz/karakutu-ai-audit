"""Same-site paragraph furniture, learned only from other live articles.

Callers select live pages on the removed article's publisher and exclude its
own URL; requested and final URLs must both stay on that publisher. The removed
article never votes. Independence uses whole-page shingles absent from both the
removed article and the other voter, so reflowed copies cannot vote each other's
body out. Both voters need 30 own shingles and half the distinct shingles within
their shared paragraph keys; shared volume is symmetric, including repeated lines.

Learned paragraphs are matched as token sequences anywhere in a page (overlapping
occurrences included). Each side's own shingles, those touching no learned token, are
looked for in the WHOLE other side, so a different paragraph break or a revised site
footer cannot hide evidence of a copy. The removed article loses only learned paragraphs
the candidate also carries: follow-ups repeating its background paragraph must not let a
copy missing that paragraph score as complete. If more than 50% of the removed article's
shingles touch learned text, measurement keeps BOTH raw scores and records the cap. This also protects against copies with
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

from audit.search.rewrite import distinctive_words
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

    Counts are ordered (removed article, candidate), including repeated lines; the removed
    article's count covers only learned lines the candidate carries. When capped, counts
    describe proposed removals; neither side is stripped.
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


def _spans(toks: list[str], keys: frozenset[str]) -> list[tuple[int, int]]:
    """Every occurrence of a learned key in `toks`, as (start, end) token positions.

    Each position is checked against the few sequences that start with its tokens, and
    the longest key starting there is taken; a match that starts inside the previous one
    is its own occurrence, so overlapping ones are all found. No regular expression; the
    cost is bounded by the page size times the length of the keys that match there.
    """
    index = _key_index(keys)
    heads = {len(head) for head in index}
    found = []
    for i in range(len(toks)):
        longest = 0
        for size in heads:
            for seq in index.get(tuple(toks[i:i + size]), ()):
                if len(seq) > longest and tuple(toks[i:i + len(seq)]) == seq:
                    longest = len(seq)
        if longest:
            found.append((i, i + longest))
    return found


def _covered(size: int, spans: list[tuple[int, int]]) -> list[bool]:
    """Which of `size` token positions lie inside any span (difference array, linear)."""
    depth = [0] * (size + 1)
    for start, end in spans:
        depth[start] += 1
        depth[end] -= 1
    marked, running = [], 0
    for i in range(size):
        running += depth[i]
        marked.append(running > 0)
    return marked


def _kept(text: str, toks: list[str], spans: list[tuple[int, int]]) -> tuple[frozenset, int]:
    """Shingles of `text` that touch no spanned token, and how many shingles were dropped."""
    if not toks:
        return frozenset(), 0
    marked = _covered(len(toks), spans)
    k = SHINGLE_K
    if len(toks) < k:
        windows = [(0, len(toks))]
    else:
        windows = [(i, i + k) for i in range(len(toks) - k + 1)]
    kept = frozenset(tuple(toks[a:b]) for a, b in windows if not any(marked[a:b]))
    return kept, len(shingles(text)) - len(kept)


def _clean_shingles(text: str, boilerplate: frozenset[str]) -> tuple[frozenset, int]:
    """Shingles of `text` that touch no token of a boilerplate paragraph, and the share
    of the text's shingles removed.

    Boilerplate is found as a token sequence anywhere in the token stream, not as whole
    lines, so identical text loses identical shingles whatever its paragraph breaks (a
    footer run into the last body paragraph on one page and set apart on another).
    """
    toks = tokens(text)
    return _kept(text, toks, _spans(toks, boilerplate))


def _clean_removed(
    removed_prose: str, boilerplate: frozenset[str], carried: frozenset[str],
) -> frozenset:
    """The removed article's shingles after dropping only paragraphs the candidate carries.

    An occurrence of a carried paragraph is kept when it overlaps, or lies within one
    shingle's reach of, an occurrence of a learned paragraph the candidate lacks. Dropping
    it would also drop the shingles that span into the missing paragraph, so a short footer
    variant inside a longer one, or a carried line right after a missing one, would take
    the missing words' evidence out of the score with it.

    Residual: any other omission shorter than a shingle right next to carried furniture
    loses the shingles that span into the furniture, as it did before this rule.
    """
    toks = tokens(removed_prose)
    reach = SHINGLE_K - 1
    missing = _covered(len(toks), [
        (max(0, a - reach), min(len(toks), b + reach))
        for a, b in _spans(toks, boilerplate - carried)
    ])
    spans = [(a, b) for a, b in _spans(toks, carried) if not any(missing[a:b])]
    return _kept(removed_prose, toks, spans)[0]


def _carried(text: str, boilerplate: frozenset[str]) -> frozenset[str]:
    """Learned paragraphs that occur in `text` as token sequences, whatever its paragraph
    breaks; keys only, never text spans."""
    toks = tokens(text)
    index = _key_index(boilerplate)
    heads = {len(head) for head in index}
    found = set()
    for i in range(len(toks)):
        for size in heads:
            for seq in index.get(tuple(toks[i:i + size]), ()):
                if tuple(toks[i:i + len(seq)]) == seq:
                    found.add(" ".join(seq))
    return frozenset(found)


def own_text_words(removed_prose: str, boilerplate: frozenset[str]) -> frozenset[str]:
    """Distinctive words covered by kept shingles, with measurement's cap fallback."""
    kept, removed = _clean_shingles(removed_prose, boilerplate)
    if not kept or removed > MAX_REMOVED_SHARE * len(shingles(removed_prose)):
        return distinctive_words(removed_prose)
    return distinctive_words(" ".join(word for shingle in kept for word in shingle))


def measure(
    removed_prose: str, page_text: str, boilerplate: frozenset[str],
) -> Measurement:
    """Score each side's own shingles (touching no boilerplate token) against the whole
    other side.

    Boilerplate is matched as token sequences, not whole lines, so identical text stays
    identical even when the two pages break paragraphs differently. The removed article
    loses only learned paragraphs the candidate also carries, so a copy missing one keeps
    that gap in its score; the candidate loses every learned paragraph it has. Strips
    nothing when the learned boilerplate would remove more than MAX_REMOVED_SHARE of the
    removed article's shingles (the cap). Counts are ordered as in `Measurement`. An
    unmeasurable side stays unmeasurable; an empty cleaned candidate measures zero while
    the removed article keeps text; an empty cleaned removed article falls back to raw.
    """
    raw = directional_containments(removed_prose, page_text)
    if not boilerplate:
        return Measurement(*raw, *raw, (0, 0))
    # The removed article loses only learned paragraphs the candidate also carries. Other
    # live pages repeating a paragraph (follow-ups quoting the same background) do not make
    # it furniture for a candidate that lacks it: dropping it there would hide exactly the
    # text the candidate is missing and lift a partial copy to the move bar.
    carried = _carried(page_text, boilerplate)
    counts = (_matched(removed_prose, carried), _matched(page_text, boilerplate))
    # The cap still weighs everything learned, so its meaning does not depend on the page.
    _, learned_removed = _clean_shingles(removed_prose, boilerplate)
    if learned_removed > MAX_REMOVED_SHARE * len(shingles(removed_prose)):
        return Measurement(*raw, *raw, counts, boilerplate_capped=True)
    old_clean = _clean_removed(removed_prose, boilerplate, carried)
    if raw[0] is None:
        # A side unmeasurable to begin with stays unmeasurable: no evidence, no call.
        return Measurement(*raw, *raw, counts)
    new_clean, _ = _clean_shingles(page_text, boilerplate)
    # Each side's OWN text (boilerplate dropped) is looked for in the WHOLE other side. A
    # footer then cannot inflate either score, and cleaning can never hide evidence: text of
    # the removed article that the candidate carries counts even if the candidate's copy of
    # it was matched as boilerplate (for example after the site revised its footer).
    if not old_clean:
        cleaned = raw
    elif not new_clean:
        cleaned = (0.0, 0.0)
    else:
        cleaned = (containment(old_clean, shingles(page_text)),
                   containment(new_clean, shingles(removed_prose)))
    return Measurement(*cleaned, *raw, counts)
