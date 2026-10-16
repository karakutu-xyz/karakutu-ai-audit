"""Step 1 — classification: IPTC media topics (≤5, ranked), one IPTC genre, geographic scope.

Two bounded model calls, both grounded by the ConceptCatalog pulled from the archive:
  1. COARSE — the 17 top-level media topics + the 58 genres + the scope menu → 1–3 top-level
     topics, a genre, a scope and a one-sentence English gist.
  2. FINE — the subtree(s) under the chosen top-level topic(s) → ≤5 specific topics, ranked.

Why two calls: LM Studio's loaded context is 8,192 tokens and the vocabulary is 1,393 concepts;
the whole tree does not fit, and a flat 1,393-way choice is beyond a local 12B anyway. Why
grounding in code: the model is told to copy notations, but a tag the archive's validator would
reject is worse than no tag — every notation is checked against the list SHOWN IN THAT CALL and
anything else is dropped (and logged).

Indexing rules applied in code after grounding (a model left to itself tends to list a topic
beside its own parent, to stack several siblings of one parent, and to pad every list to the
cap, where human indexers choose a few specific topics). They follow library
practice (LC Subject Headings Manual H 180; NLM MeSH "most specific heading"; NewsML-G2 marks a
co-listed ancestor as *derived*, never as a chosen topic):
  * rule of three   — more than MAX_SIBLINGS children of one parent → the parent instead;
  * most specific   — a tag is dropped when another chosen tag lies beneath it (consumers infer
                      broader terms from the tree; `details.collapsed` says what was folded);
  * no filler       — a top-level topic is dropped when anything more specific is present.
The prompt asks for 1–3 topics by default (5 hard cap): the main subject plus one per separate
section, primary first, no near-synonyms. Variable length is intended: an empty slot is not a
defect.

Reviewer feedback on posted tags showed three failure shapes: lists that missed real sections
appearing after the opening, vague umbrellas (people, values, social problem) and hook topics.
Half of the misses were unreachable — COARSE never chose the top-level the facet lives under —
and most of the rest sat beyond the short windows the two calls used to see. Hence the larger
text windows (COARSE, which chooses the subtrees, sees the most; FINE is bounded by its long
menu) below, the section rules and the filing notes in the prompts, and one more rule in code:
  * no umbrellas    — VAGUE_UMBRELLAS are dropped when a concrete (non-top-level) topic is present.
The larger windows improve recall on must-have tags and cut rejected or vague tags; prompt
wording alone recovers far less, so the windows carry most of the gain.

Classification is metadata. It never fails the audit: a total model failure yields
`classification=None` with an explanation, and the pipeline continues.
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent
from pydantic_ai.models import Model

from src.api.models import Article, Classification, ConceptTag, GeographicScope
from src.clients.concepts import Concept, ConceptCatalog
from src.search.fetch import prose_of
from src.workflow import prompts

logger = logging.getLogger(__name__)

MAX_TAGS = 5            # hard cap (LC H 180 §F allows up to six; the archive validates ≤ 20)
MAX_SIBLINGS = 3        # LC "rule of three": four or more children of one parent → the parent instead
MAX_TOP_LEVELS = 3
# Output caps per call (pydantic-ai model_settings, merged OVER the provider default). A correct
# answer is ~80–100 tokens; without a cap a runaway fine pass can run to the provider's
# token cap and take minutes before the retry answers. A runaway must fail in seconds, not
# minutes — the retry is what actually fixes it.
COARSE_MAX_TOKENS = 350
FINE_MAX_TOKENS = 300
# Article text each call sees (prose_of, then truncated; the retry halves it). Sized to the
# 8,192-token context: a long article truncated to these windows plus the largest
# three-top-level fine menu still leaves comfortable margin for the output cap and the tool
# schema (Turkish prose runs at roughly 3–4 chars per token). Much smaller windows hide most of
# a typical article's later sections.
COARSE_ARTICLE_CHARS = 10000
FINE_ARTICLE_CHARS = 5500
# Subtree budgeting for the FINE call (the loaded context is 8,192 tokens). A top-level whose
# depth-<=4 subtree exceeds FINE_DEEP_LIMIT nodes (economy: 253, sport: 334) is listed to
# depth 3 only; the whole menu is capped at FINE_MAX_NODES (later top-levels are trimmed to
# their direct children first, and a top-level itself always stays selectable).
FINE_DEEP_LIMIT = 120
FINE_MAX_NODES = 200
DEFINE_UP_TO_DEPTH = 2        # definitions shown for depth<=2 nodes; deeper nodes are label-only
MAX_DEFINITION_CHARS = 140

# Depth-2 umbrellas reviewers called vague filler. Each either only groups
# concrete subjects (values, social problem, social condition) or names a register rather than a
# subject (people). None was a must-have in any gold case. Kept OUT on purpose — a story can be
# about them: ethics, language, culture, demographic group, communities, fundamental rights.
VAGUE_UMBRELLAS = frozenset({
    "medtop:20000502",  # people (human interest)
    "medtop:20000808",  # values (society)
    "medtop:20000802",  # social problem (society)
    "medtop:20000799",  # social condition (society)
})

SCOPE_MEANINGS = {
    GeographicScope.LOCAL: "one town, district or province; matters mainly to the people there",
    GeographicScope.REGIONAL: "a region spanning several provinces, or a cross-border region",
    GeographicScope.NATIONAL: "the whole country — national government, nationwide institutions or effects",
    GeographicScope.GLOBAL_SCOPE: "international — several countries, foreign governments, global bodies or events",
}


def _list_from_json_string(value):
    """gemma sometimes sends a list field as a JSON-encoded string ('["medtop:…", …]') instead of
    an array; when the retry did the same, a correct answer was lost and the case got no
    classification. Decode it; anything else is left to
    validation as before."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, list):
            return decoded
    return value


class CoarseClassification(BaseModel):
    """Model-authored, call 1. Every field is re-validated against the catalog in code."""
    top_level_topics: List[str] = Field(default_factory=list)
    genre: str = ""
    geographic_scope: str = ""
    gist: str = ""

    _decode_lists = field_validator("top_level_topics", mode="before")(_list_from_json_string)


CONFIDENCE_MIN = 0.5    # a topic the model itself rates below this is not "well established" — drop it


class FineClassification(BaseModel):
    """Model-authored, call 2."""
    topics: List[str] = Field(default_factory=list)
    confidences: List[float] = Field(
        default_factory=list,
        description="One number from 0 to 1 per topic, in the same order: how well the text "
                    "establishes that topic. Leave a weak topic out rather than listing it low.")

    _decode_lists = field_validator("topics", "confidences", mode="before")(_list_from_json_string)


@dataclass
class ClassificationAgents:
    coarse: Agent
    fine: Agent


class ClassificationOutcome(BaseModel):
    """What the stage hands the pipeline: the grounded classification (or None) plus the facts
    a reviewer wants in `StageResult.details`."""
    classification: Optional[Classification] = None
    explanation: str = ""
    details: dict = Field(default_factory=dict)


def build_agents(model: Model) -> ClassificationAgents:
    return ClassificationAgents(
        coarse=Agent(model, result_type=CoarseClassification,
                     system_prompt=prompts.CLASSIFICATION_COARSE_PROMPT),
        fine=Agent(model, result_type=FineClassification,
                   system_prompt=prompts.CLASSIFICATION_FINE_PROMPT),
    )


async def run(article: Article, agents: ClassificationAgents,
              catalog: Optional[ConceptCatalog]) -> ClassificationOutcome:
    """Classify the article. Never raises for model reasons; see the module docstring."""
    if catalog is None:
        return ClassificationOutcome(
            explanation="Skipped: no IPTC concept catalog (the archive was unreachable and no "
                        "cache exists), so no tags could be assigned.",
            details={"skipped": True},
        )
    prose = prose_of(article.content)

    coarse = await _call(agents.coarse, lambda n: coarse_prompt(article, prose, catalog, n),
                         COARSE_ARTICLE_CHARS, "coarse", COARSE_MAX_TOKENS)
    if coarse is None:
        return ClassificationOutcome(
            explanation="The classification model failed on both attempts; no tags were "
                        "assigned. This does not affect the rest of the audit.",
            details={"model_failed": True},
        )

    top_levels, dropped = ground_top_levels(coarse.top_level_topics, catalog)
    genre_notation = notation_of(coarse.genre) or (coarse.genre or "").strip()
    genre = catalog.get(genre_notation) if catalog.is_genre(genre_notation) else None
    if genre_notation and genre is None:
        dropped.append(genre_notation)
    scope = ground_scope(coarse.geographic_scope)
    if (coarse.geographic_scope or "").strip() and scope is None:
        dropped.append(coarse.geographic_scope.strip())

    tags: List[Concept] = []
    menu: List[Concept] = []
    fine_failed = False
    collapsed: List[Tuple[str, str]] = []
    siblings_folded: List[Tuple[str, List[str]]] = []
    roots_dropped: List[str] = []
    low_confidence: List[Tuple[str, float]] = []
    vague: List[str] = []
    if top_levels:
        menu = fine_menu(top_levels, catalog)
        fine = await _call(agents.fine,
                           lambda n: fine_prompt(article, prose, coarse.gist, menu, n),
                           FINE_ARTICLE_CHARS, "fine", FINE_MAX_TOKENS)
        if fine is None:
            fine_failed = True
        else:
            tags, fine_dropped = ground_tags(fine.topics, menu, catalog)
            dropped.extend(fine_dropped)
            tags, low_confidence = drop_low_confidence(tags, fine.topics, fine.confidences)
            # Indexing rules, in code (see the module docstring):
            #   rule of three — four or more children of one parent become the parent;
            #   most specific level — a tag never sits next to its own ancestor;
            #   no filler — a top-level topic goes when anything more specific is present.
            # Umbrellas go first, so a parent the rule of three creates is never removed after.
            tags, vague = drop_vague_umbrellas(tags)
            tags, siblings_folded = collapse_siblings(tags, catalog)
            tags, collapsed = collapse_hierarchy(tags)
            tags, roots_dropped = drop_root_fillers(tags)
    if not tags:
        tags = top_levels[:MAX_TAGS]   # the broad filing stands when the fine pass gave nothing

    classification = Classification(
        subject_tags=[ConceptTag(notation=c.notation, label=c.pref_label) for c in tags],
        genre=ConceptTag(notation=genre.notation, label=genre.pref_label) if genre else None,
        geographic_scope=scope,
        gist=(coarse.gist or "").strip(),
    )
    if dropped:
        logger.info("classification: dropped %d model output(s) not in the vocabulary shown: %s",
                    len(dropped), ", ".join(dropped))
    if collapsed:
        logger.info("classification: collapsed %d broader tag(s) covered by a narrower pick: %s",
                    len(collapsed), ", ".join(f"{a} ⊂ {b}" for a, b in collapsed))
    labels = ", ".join(f"{t.label} ({t.notation})" for t in classification.subject_tags) or "none"
    explanation = (
        f"Topics: {labels}. Genre: {genre.pref_label if genre else 'not determined'}. "
        f"Scope: {scope.value if scope else 'not determined'}."
        + (" The fine-grained pass failed; the broad topic(s) stand." if fine_failed else "")
        + (f" {len(dropped)} model output(s) were not in the vocabulary and were dropped."
           if dropped else "")
        + (f" {len(collapsed)} broader topic(s) were folded into the narrower pick beneath them."
           if collapsed else "")
        + (f" {len(siblings_folded)} group(s) of four or more sibling topics were replaced by their parent."
           if siblings_folded else "")
        + (f" {len(roots_dropped)} top-level filler topic(s) were dropped." if roots_dropped else "")
        + (f" {len(vague)} vague umbrella topic(s) were dropped." if vague else "")
        + (f" {len(low_confidence)} topic(s) the model itself rated below {CONFIDENCE_MIN:.0%} were left out."
           if low_confidence else "")
    )
    return ClassificationOutcome(
        classification=classification if (tags or genre or scope) else None,
        explanation=explanation,
        details={
            "subject_tags": [t.model_dump() for t in classification.subject_tags],
            "genre": classification.genre.model_dump() if classification.genre else None,
            "geographic_scope": scope.value if scope else None,
            "gist": classification.gist,
            "coarse_top_levels": [c.notation for c in top_levels],
            "fine_menu_size": len(menu),
            "fine_failed": fine_failed,
            "dropped": dropped,
            "collapsed": [{"dropped": a, "covered_by": b} for a, b in collapsed],
            "siblings_folded": [{"parent": p_, "children": kids} for p_, kids in siblings_folded],
            "roots_dropped": roots_dropped,
            "vague_dropped": vague,
            "low_confidence_dropped": [{"notation": n, "confidence": c} for n, c in low_confidence],
        },
    )


async def _call(agent, prompt_for: Callable[[int], str], chars: int, name: str,
                max_tokens: int):
    """Two attempts, the second with half the article text. None when both fail."""
    last: Optional[Exception] = None
    for attempt, n in enumerate((chars, chars // 2), start=1):
        try:
            result = await agent.run(prompt_for(n), model_settings={"max_tokens": max_tokens})
            return result.data
        except Exception as exc:  # noqa: BLE001 — empty response, schema refusal, timeout…
            last = exc
            logger.warning("classification: %s call attempt %d failed (%s)", name, attempt,
                           type(exc).__name__)
    logger.error("classification: %s call failed on all attempts (%s: %s)",
                 name, type(last).__name__, last)
    return None


# --- grounding (pure) --------------------------------------------------------

_NOTATION_RE = re.compile(r"(?:medtop|genre):[A-Za-z0-9_-]+")


def notation_of(item: str) -> Optional[str]:
    """The notation inside a model output item, or None.

    gemma can copy the menu LINES back verbatim —
    `"medtop:20001324 | sports management and ownership"` — five excellent picks that a strict
    equality check dropped, leaving only the broad top-level tag. The notation is right there,
    so take the first `medtop:…` / `genre:…` token wherever it sits in the string. A bare
    label with no notation is still None (and is dropped by the caller).
    """
    m = _NOTATION_RE.search(item or "")
    return m.group(0) if m else None


def ground_top_levels(raw: Sequence[str], catalog: ConceptCatalog) -> Tuple[List[Concept], List[str]]:
    """Known TOP-LEVEL topics only, model order kept, de-duplicated, <= MAX_TOP_LEVELS."""
    kept: List[Concept] = []
    dropped: List[str] = []
    for item in raw or []:
        n = notation_of(item) or (item or "").strip()
        c = catalog.get(n)
        if c is None or not catalog.is_topic(n) or c.parent_notation is not None:
            if n:
                dropped.append(n)
            continue
        if all(k.notation != n for k in kept):
            kept.append(c)
        if len(kept) >= MAX_TOP_LEVELS:
            break
    return kept, dropped


def ground_scope(raw: str) -> Optional[GeographicScope]:
    try:
        return GeographicScope((raw or "").strip())
    except ValueError:
        return None


def ground_tags(raw: Sequence[str], menu: Sequence[Concept],
                catalog: ConceptCatalog) -> Tuple[List[Concept], List[str]]:
    """Only notations that were SHOWN in the fine menu, model order kept, <= MAX_TAGS."""
    allowed: Set[str] = {c.notation for c in menu}
    kept: List[Concept] = []
    dropped: List[str] = []
    for item in raw or []:
        n = notation_of(item) or (item or "").strip()
        if n not in allowed:
            if n:
                dropped.append(n)
            continue
        if all(k.notation != n for k in kept):
            kept.append(catalog.get(n))
        if len(kept) >= MAX_TAGS:
            break
    return kept, dropped


def is_ancestor(a: Concept, b: Concept) -> bool:
    """True when `a` sits above `b` in the tree (a's hierarchy path is a proper prefix of b's)."""
    if not a.hierarchy_path or not b.hierarchy_path or a.notation == b.notation:
        return False
    return b.hierarchy_path.startswith(a.hierarchy_path + ".")


def collapse_hierarchy(tags: Sequence[Concept]) -> Tuple[List[Concept], List[Tuple[str, str]]]:
    """Drop every tag that is an ANCESTOR of another tag in the same list; order is kept.

    Thesaurus indexing rule (ISO 5963 / Z39.19: index at the most specific level; a consumer
    infers the broader terms from the hierarchy) and a common model defect (a tag listed
    together with its own parent — "fundamental rights" + "human
    rights", "civil unrest" + "protests and demonstrations"). Returns (kept, [(dropped, kept_for)]).
    """
    kept: List[Concept] = []
    collapsed: List[Tuple[str, str]] = []
    for t in tags:
        covered_by = next((o for o in tags if is_ancestor(t, o)), None)
        if covered_by is not None:
            collapsed.append((t.notation, covered_by.notation))
            continue
        if all(k.notation != t.notation for k in kept):
            kept.append(t)
    return kept, collapsed


def drop_low_confidence(tags: Sequence[Concept], raw_topics: Sequence[str], confidences: Sequence[float]
                        ) -> Tuple[List[Concept], List[Tuple[str, float]]]:
    """Cut the topics the model itself rated below CONFIDENCE_MIN (operator's rule: "if
    confidence drops, do not keep pushing tags"). Confidences are aligned with the RAW topic list by
    position; when the model returned none, or a list of another length, nothing is cut."""
    if not confidences or len(confidences) != len(raw_topics or []):
        return list(tags), []
    score = {}
    for item, c in zip(raw_topics, confidences, strict=False):
        n = notation_of(item) or (item or "").strip()
        if n and n not in score:
            try:
                score[n] = float(c)
            except (TypeError, ValueError):
                return list(tags), []
    kept, cut = [], []
    for t in tags:
        c = score.get(t.notation)
        if c is not None and c < CONFIDENCE_MIN:
            cut.append((t.notation, c))
        else:
            kept.append(t)
    return kept, cut


def collapse_siblings(tags: Sequence[Concept], catalog: ConceptCatalog
                      ) -> Tuple[List[Concept], List[Tuple[str, List[str]]]]:
    """LC Subject Headings Manual H 180 §J–L ("rule of three"): when MORE THAN MAX_SIBLINGS chosen
    tags share one parent, the parent describes the article better than the pile of children —
    replace them by the parent at the first child's rank. Returns (kept, [(parent, children)])."""
    groups: dict = {}
    for t in tags:
        groups.setdefault(t.parent_notation, []).append(t)
    folded: List[Tuple[str, List[str]]] = []
    replace: dict = {}
    for parent, kids in groups.items():
        if parent and len(kids) > MAX_SIBLINGS and catalog.get(parent) is not None:
            folded.append((parent, [k.notation for k in kids]))
            for k in kids:
                replace[k.notation] = parent
    if not folded:
        return list(tags), []
    kept: List[Concept] = []
    for t in tags:
        c = catalog.get(replace[t.notation]) if t.notation in replace else t
        if all(k.notation != c.notation for k in kept):
            kept.append(c)
    return kept, folded


def drop_vague_umbrellas(tags: Sequence[Concept]) -> Tuple[List[Concept], List[str]]:
    """Drop the VAGUE_UMBRELLAS when the list holds a concrete topic: one that is neither an
    umbrella nor a top-level filler (a top-level is broader still, and drop_root_fillers removes
    it next to an umbrella). Otherwise the list stays as it is. Order is kept.
    Returns (kept, dropped_notations)."""
    if not any(t.notation not in VAGUE_UMBRELLAS and t.depth > 1 for t in tags):
        return list(tags), []
    kept = [t for t in tags if t.notation not in VAGUE_UMBRELLAS]
    return kept, [t.notation for t in tags if t.notation in VAGUE_UMBRELLAS]


def drop_root_fillers(tags: Sequence[Concept]) -> Tuple[List[Concept], List[str]]:
    """A top-level topic ("human interest", "society") is a FILLER when any more specific topic is
    in the list — a broad heading is used only when nothing more precise exists (LC H 180 §G).
    Kept when the list holds nothing else. Returns (kept, dropped_roots)."""
    if not tags or all(t.depth <= 1 for t in tags):
        return list(tags), []
    kept = [t for t in tags if t.depth > 1]
    return kept, [t.notation for t in tags if t.depth <= 1]


def fine_menu(top_levels: Sequence[Concept], catalog: ConceptCatalog) -> List[Concept]:
    """The candidate list for the fine call: each chosen top-level followed by its subtree,
    depth-budgeted (FINE_DEEP_LIMIT / FINE_MAX_NODES)."""
    menu: List[Concept] = []
    for tl in top_levels:
        deep = catalog.subtree(tl.notation, max_depth=4)
        nodes = deep if len(deep) <= FINE_DEEP_LIMIT else catalog.subtree(tl.notation, max_depth=3)
        remaining = FINE_MAX_NODES - len(menu) - 1
        menu.append(tl)                      # the broad topic itself always stays selectable
        if remaining <= 0:
            continue
        if len(nodes) > remaining:
            nodes = catalog.children(tl.notation)[:remaining]
        menu.extend(nodes)
    return menu


# --- prompts -----------------------------------------------------------------

def render_concepts(concepts: Sequence[Concept]) -> str:
    lines = []
    for c in concepts:
        indent = "  " * (c.depth - 1)
        if c.depth <= DEFINE_UP_TO_DEPTH and c.definition:
            lines.append(f"{indent}{c.notation} | {c.pref_label} | {c.definition[:MAX_DEFINITION_CHARS]}")
        else:
            lines.append(f"{indent}{c.notation} | {c.pref_label}")
    return "\n".join(lines)


def coarse_prompt(article: Article, prose: str, catalog: ConceptCatalog, article_chars: int) -> str:
    topics = render_concepts(catalog.top_level())
    genres = "\n".join(
        f"{g.notation} | {g.pref_label} | {(g.definition or '')[:MAX_DEFINITION_CHARS]}"
        for g in catalog.genres()
    )
    scopes = "\n".join(f"{s.value} — {meaning}" for s, meaning in SCOPE_MEANINGS.items())
    return (
        f"ARTICLE\nTitle: {article.title}\nText: {prose[:article_chars]}\n\n"
        f"TOP-LEVEL MEDIA TOPICS (choose 1–3, most central first; copy the notation exactly):\n"
        f"{topics}\n\n"
        f"GENRES (choose exactly one; copy the notation exactly):\n{genres}\n\n"
        f"GEOGRAPHIC SCOPE (choose exactly one value):\n{scopes}"
    )


def fine_prompt(article: Article, prose: str, gist: str, menu: Sequence[Concept],
                article_chars: int) -> str:
    return (
        f"ARTICLE\nTitle: {article.title}\nGist: {gist}\nText: {prose[:article_chars]}\n\n"
        f"CANDIDATE TOPICS (indented = more specific; choose up to {MAX_TAGS}, most central "
        f"first; copy the notation exactly):\n{render_concepts(menu)}"
    )
