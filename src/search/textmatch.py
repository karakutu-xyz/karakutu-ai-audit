"""Pure token/shingle text-containment helpers — the arithmetic behind relocation verdicts.

Stage 3 used to let the MODEL decide whether a same-publisher live page was a
republication of the removed article. A model reading two same-publisher pages from the
same news cycle readily mistakes a SIBLING (different article, same story) for a copy.
Where measurement is possible it replaces judgement: two directional shingle containments
say arithmetically whether the original's text survives in the candidate.

  old_in_new = |shingles(old) ∩ shingles(new)| / |shingles(old)|
      "how much of the removed article's text appears in the candidate"
  new_in_old — symmetric: "how much of the candidate's text came from the removed article"

No I/O and no models here: pure string functions. HTML must be reduced to prose BEFORE
calling in (see `fetch.prose_of`) — the Karakutu archive stores raw HTML, and measuring
markup+CTA boilerplate against extracted prose depresses even a verbatim republication's
old_in_new far below the MOVED bar.

Calibration: compare prose-extracted text on BOTH sides (the repo's own `html_to_text`).
The shapes the thresholds separate:

    verbatim republication   old_in_new at (or a hair below) 1.0; new_in_old high but
                             below 1.0 (the copy adds its own furniture)
    shortened / rewritten    old_in_new well above the sibling band but clearly short
    republish (same event)   of verbatim — a partial fit
    sibling story            both directions near zero; only shared quotes or stock
                             phrases overlap
"""

import re
import unicodedata
from typing import FrozenSet, List, Optional, Tuple

# Tokens: word characters, length >= MIN_TOKEN_CHARS, casefolded (NFKC applied first).
# Shorter tokens (da, ve, bir digits' remnants…) are stopword-like in Turkish/Azeri news
# prose and only add chance overlap.
MIN_TOKEN_CHARS = 3
# k=5 shingles: long enough that shared quotes and boilerplate rarely align by chance,
# short enough that light re-editing does not zero the overlap.
SHINGLE_K = 5

# --- verdict-tier thresholds (from the calibration shapes above) -------------
#
# MOVED_THRESHOLD = 0.99 — a verbatim republication contains the whole original, so
#   old_in_new sits at 1.0; a rewritten same-event story falls clearly short of it.
#   0.99 keeps MOVED verbatim-grade with a wide margin below it.
# SIBLING_MAX = 0.20 — a sibling overlaps only through shared quotes or stock phrases in
#   a same-cycle article, well under this ceiling; 0.20 leaves margin and stays below
#   PARTIAL_MIN. Only a fit measurably BELOW this may demote a candidate.
# PARTIAL_MIN = 0.30 — the floor above which a partial fit is called "substantial" in
#   findings. A rewritten same-event story sits well inside the band.
MOVED_THRESHOLD = 0.99
PARTIAL_MIN = 0.30
SIBLING_MAX = 0.20

Shingles = FrozenSet[Tuple[str, ...]]

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> List[str]:
    """Casefolded word tokens of length >= MIN_TOKEN_CHARS.

    Turkish/Azeri letters survive (ş, ğ, ç, ə, …) — this is deliberately NOT an ASCII
    fold. The one repair is dotted/dotless I: Python's casefold turns İ into "i" + U+0307
    (combining dot) and leaves ı as-is, so "İSTANBUL" would never match "istanbul" and
    "BAŞLATTI" would never match "başlattı". Stripping U+0307 and folding ı→i makes the
    match case-insensitive for Turkish at the cost of the (rare, shingle-buffered)
    ı-vs-i distinction.
    """
    folded = (
        unicodedata.normalize("NFKC", text)
        .casefold()
        .replace("̇", "")
        .replace("ı", "i")
    )
    return [t for t in _WORD_RE.findall(folded) if len(t) >= MIN_TOKEN_CHARS]


def shingles(text: str, k: int = SHINGLE_K) -> Shingles:
    """The set of k-grams over `tokens(text)`.

    A text with fewer than k tokens (but at least one) yields its whole token tuple as a
    single shingle, so short texts stay comparable; a tokenless text yields the empty set,
    which downstream means "unmeasurable", never "measured zero".
    """
    toks = tokens(text)
    if not toks:
        return frozenset()
    if len(toks) < k:
        return frozenset([tuple(toks)])
    return frozenset(tuple(toks[i:i + k]) for i in range(len(toks) - k + 1))


def containment(of: Shingles, within: Shingles) -> float:
    """|of ∩ within| / |of| — the fraction of `of` found in `within`; 0.0 when `of` is empty."""
    if not of:
        return 0.0
    return len(of & within) / len(of)


def directional_containments(
    old_text: str, new_text: str,
) -> Tuple[Optional[float], Optional[float]]:
    """(old_in_new, new_in_old) between two prose texts, or (None, None) when either side
    yields no shingles.

    "Unmeasurable" is deliberately distinct from "measured 0.0": disjoint texts measure
    (0.0, 0.0) and may demote a candidate; a textless side measures NOTHING and must never
    demote on no evidence (the caller keeps judgement-based behaviour instead).
    """
    old_sh, new_sh = shingles(old_text), shingles(new_text)
    if not old_sh or not new_sh:
        return None, None
    return containment(old_sh, new_sh), containment(new_sh, old_sh)


# --- distinctive phrase (stage 3's quoted search pass) -----------------------
#
# A bare-title general search often misses a copy that verifiably exists on major
# national outlets — near-identical sibling headlines drown the story. One distinctive sentence fragment, searched in quotes, cuts
# through the title collision.

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
# Only the article's opening carries the lede facts; a long quote deep in the piece is
# often another speaker's boilerplate.
EARLY_SENTENCES = 5
# Window size sits inside the 6–10-token band that search engines phrase-match well.
PHRASE_WINDOW = 8
PHRASE_MIN_TOKENS = 6


def distinctive_phrase(prose: str) -> str:
    """ONE deterministic quoted-search phrase from an article's prose, or "" when the
    text is too short to yield one.

    Takes PROSE — reduce HTML with `fetch.prose_of` first, like every function here.
    Picks a PHRASE_WINDOW-token window from the longest of the first EARLY_SENTENCES
    sentences, preferring the first window that carries an anchor: a token with a digit,
    or an uppercase-initial token that is not merely the sentence's first word — names
    and figures are what make a phrase distinctive. Returns the sentence's raw text for
    that window (original casing and punctuation) with double quotes stripped, so it
    embeds cleanly in a quoted query.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(prose) if s.strip()]
    if not sentences:
        return ""
    sentence = max(sentences[:EARLY_SENTENCES], key=lambda s: len(_WORD_RE.findall(s)))
    words = list(_WORD_RE.finditer(sentence))
    if len(words) < PHRASE_MIN_TOKENS:
        return ""

    def _is_anchor(index: int) -> bool:
        token = words[index].group()
        if any(ch.isdigit() for ch in token):
            return True
        # A sentence-initial capital is orthography, not a proper-noun signal.
        return index > 0 and token[:1].isupper()

    size = min(PHRASE_WINDOW, len(words))
    starts = range(len(words) - size + 1)
    start = next((i for i in starts if any(_is_anchor(j) for j in range(i, i + size))), 0)
    phrase = sentence[words[start].start():words[start + size - 1].end()]
    return phrase.replace('"', "").strip()
