"""Step 4 — find the source of the article, and check whether it is live.

Two halves, always both:

(a) ITSELF — the premise probe. Fetch the removed article's own URL and MEASURE the archived
    text against what answers (`old_in_new >= LIVE_SELF_MIN_CONTAINMENT`). Live and intact
    means the audit premise — a removed article — does not presently hold (pipeline verdict
    NOT_REMOVED: descriptive, never exculpatory). Moved here from stage 3 unchanged; measured
    2026-08-30, all 8 sampled "removed" URLs were 200 and batch 1 was 47% NOT_REMOVED.

(b) A CITED SOURCE. Turkish outlets routinely run agency copy ("(İHA)", "Anadolu Ajansı'na
    yaptığı açıklamada") or credit another outlet ("Sözcü'nün haberine göre"). The origin copy
    is the single best place to look for the story — and if THAT copy is gone too, a human
    should know (red flag `SOURCE_DEAD`). Extraction is deterministic first (outbound links in
    the archived HTML, attribution phrases), then ONE bounded model call names the source; every
    model claim is checked against the text in code — the name must occur in the text, the URL
    must be one of the links found and off the publisher's host. A claim that does not survive
    is `unknown`. A wrong source sends a human to the wrong page; no source is better.

Liveness of a cited URL goes through the same fetch gate as every lead: live => containment
measured and the page handed to step 5 as prefetched evidence; dead (404/410, article-shaped)
=> `dead_source` (the flag) — the article's own attribution establishes same-story, so the
pipeline files it under `dead_same_story`; unreadable (403, JS shell, PDF) => NOT a flag, the
existing rule. A cited outlet NAMED without a URL becomes ONE site-scoped search whose hits are
passed to step 5 as leads (`source="cited"`); nothing is fetched here for those.
"""
import html as html_lib
import logging
import re
import unicodedata
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from src.api.models import Article
from src.search.base import BaseSearchProvider, SearchResult
from src.search.fetch import (
    DeadCandidate, LivePage, fetch_candidates, guess_language, host_of, looks_like_an_article_url,
    normalize_url, probe_removed_url, prose_of, same_publisher,
)
from src.search.textmatch import directional_containments
from src.workflow import prompts

logger = logging.getLogger(__name__)

# The premise probe's floor: the removed URL counts as "live with its text intact" only when at
# least this share of the archived text measurably appears on the page (guards against soft-404s
# and parked domains, which answer 200 with readable-but-unrelated prose).
LIVE_SELF_MIN_CONTAINMENT = 0.5
HEAD_CHARS = 2000
TAIL_CHARS = 600
MAX_LINKS_IN_PROMPT = 8
MAX_PHRASES_IN_PROMPT = 6
CITED_SEARCH_RESULTS = 3
# Output cap per call (merged over the provider default): a source claim is ~40–120 tokens.
SOURCE_MAX_TOKENS = 400
SOURCE_KINDS = ("self", "agency", "other_outlet", "unknown")

# Folded name -> host. Used ONLY to turn a NAMED source without a URL into one site-scoped
# search. Deliberately small: an unknown name is simply "not_located".
KNOWN_SOURCES = {
    "aa": "aa.com.tr", "anadolu ajansi": "aa.com.tr",
    "dha": "dha.com.tr", "demiroren haber ajansi": "dha.com.tr",
    "iha": "iha.com.tr", "ihlas haber ajansi": "iha.com.tr",
    "anka": "ankahaber.net", "anka haber ajansi": "ankahaber.net",
    "mezopotamya ajansi": "mezopotamyaajansi43.com",
    "bianet": "bianet.org", "jinnews": "jinnews.net",
    "reuters": "reuters.com", "afp": "afp.com",
    "bbc turkce": "bbc.com", "bbc": "bbc.com", "dw turkce": "dw.com", "dw": "dw.com",
    "t24": "t24.com.tr", "cumhuriyet": "cumhuriyet.com.tr", "sozcu": "sozcu.com.tr",
    "birgun": "birgun.net", "evrensel": "evrensel.net", "hurriyet": "hurriyet.com.tr",
    "sabah": "sabah.com.tr", "milliyet": "milliyet.com.tr", "haberturk": "haberturk.com",
    "ntv": "ntv.com.tr", "karar": "karar.com", "diken": "diken.com.tr",
    "gazete duvar": "gazeteduvar.com.tr", "duvar": "gazeteduvar.com.tr",
    "medyascope": "medyascope.tv", "arti gercek": "artigercek.com", "yeni safak": "yenisafak.com",
    "halk tv": "halktv.com.tr", "bloomberg ht": "bloomberght.com",
    "apa": "apa.az", "report": "report.az", "trend": "trend.az", "azertac": "azertag.az",
    "turan": "turan.az", "meydan tv": "meydan.tv",
}
_NOISE_HOSTS = ("facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "youtu.be",
                "whatsapp.com", "t.me", "linkedin.com", "pinterest.com", "google.com", "apple.com",
                "play.google.com", "tiktok.com", "threads.net",
                # markup vocabularies that show up as itemtype/xmlns URLs inside article HTML
                "schema.org", "w3.org", "ogp.me", "purl.org")
_HREF_RE = re.compile(r"""href\s*=\s*["'](https?://[^"'\s>]+)""", re.I)
_BARE_URL_RE = re.compile(r"""https?://[^\s<>"'\])]+""")
_ATTRIBUTION_RES = [
    re.compile(r"[^.\n]{0,80}haberine göre[^.\n]{0,60}", re.I),
    re.compile(r"[^.\n]{0,80}(?:ulaştığı bilgilere göre|bildirdiğine göre|aktardığına göre|"
               r"yazdığına göre|haberleştirdi)[^.\n]{0,40}", re.I),
    re.compile(r"Kaynak\s*:\s*[^\n.]{2,80}", re.I),
    re.compile(r"[^.\n]{0,60}\((?:AA|DHA|İHA|IHA|ANKA|MA|AFP|AP|DPA|Reuters)\)[^.\n]{0,40}"),
    re.compile(r"[^.\n]{0,60}(?:Anadolu Ajansı|Demirören Haber Ajansı|İhlas Haber Ajansı|"
               r"Mezopotamya Ajansı|ANKA Haber Ajansı|Bianet|Reuters|AFP)[^.\n]{0,60}", re.I),
]


class SourceResult(BaseModel):
    """Model-authored. Every field is re-checked against the text by `ground_claim`."""
    source_kind: str = "unknown"
    source_name: str = ""
    source_url: str = ""
    attribution_quote: str = ""


class SourceEvidence(BaseModel):
    # (a) itself
    self_status: str = "not_probed"        # live_intact | live_other | dead_or_unreachable
    self_containment: Optional[float] = None
    live_at_original_url: bool = False
    # (b) cited source
    source_kind: str = "unknown"
    source_name: str = ""
    source_url: str = ""
    source_host: str = ""
    source_status: str = "n/a"             # live | dead | unreadable | leads_only | not_located | n/a
    source_containment: Optional[float] = None
    attribution_quote: str = ""
    extracted_links: List[str] = Field(default_factory=list)
    extracted_phrases: List[str] = Field(default_factory=list)
    dropped_claims: List[str] = Field(default_factory=list)
    dead_source: Optional[DeadCandidate] = None
    live_source: Optional[LivePage] = None
    extra_hits: List[SearchResult] = Field(default_factory=list)
    model_failed: bool = False
    reasoning: str = ""

    @property
    def source_is_dead(self) -> bool:
        return self.dead_source is not None


def build_agent(model: Model) -> Agent[None, SourceResult]:
    return Agent(model, result_type=SourceResult, system_prompt=prompts.SOURCE_PROMPT)


async def run(removed: Article, agent, search_provider: BaseSearchProvider) -> SourceEvidence:
    ev = SourceEvidence()
    prose = prose_of(removed.content)

    # (a) itself — the premise probe
    page = await probe_removed_url(removed.url, removed.title)
    if page is None:
        ev.self_status = "dead_or_unreachable"
    else:
        o, _ = directional_containments(prose, page.text)
        ev.self_containment = o
        if o is not None and o >= LIVE_SELF_MIN_CONTAINMENT:
            ev.self_status, ev.live_at_original_url = "live_intact", True
            logger.info("source: removed URL %s is LIVE with %.0f%% of the archived text present",
                        removed.url, o * 100)
        else:
            ev.self_status = "live_other"   # 200 but a soft-404 / parked page / unmeasurable

    # (b) a cited source — deterministic extraction, the model, then grounding
    ev.extracted_links = extract_links(removed.content, removed.url)
    ev.extracted_phrases = extract_attribution_phrases(prose)
    try:
        result = await agent.run(build_prompt(removed, prose, ev.extracted_links, ev.extracted_phrases),
                                 model_settings={"max_tokens": SOURCE_MAX_TOKENS})
        raw = result.data
    except Exception as exc:  # noqa: BLE001 — empty response, schema refusal, timeout…
        logger.warning("source: model call failed (%s) — cited source unknown", type(exc).__name__)
        ev.model_failed = True
        raw = SourceResult()
    (ev.source_kind, ev.source_name, ev.source_url,
     ev.attribution_quote, ev.dropped_claims) = ground_claim(raw, prose, ev.extracted_links, removed.url)

    if ev.source_url:
        ev.source_host = host_of(ev.source_url)
        fetched = await fetch_candidates(
            [SearchResult(url=ev.source_url, title=ev.source_name or "cited source",
                          snippet=ev.attribution_quote, source="cited")],
            exclude_url=removed.url,
        )
        if fetched.live:
            ev.live_source = fetched.live[0]
            ev.source_status = "live"
            ev.source_containment, _ = directional_containments(prose, ev.live_source.text)
        elif fetched.dead:
            d = fetched.dead[0]
            ev.dead_source = d.model_copy(update={"reason": f"cited source — {d.reason}"})
            ev.source_status = "dead"
            logger.info("source: cited source %s is DEAD (%s) — red flag", d.url, d.reason)
        else:
            ev.source_status = "unreadable"
    elif ev.source_kind in ("agency", "other_outlet") and ev.source_name:
        host = host_for(ev.source_name)
        if host and host != host_of(removed.url):
            ev.source_host = host
            hits = await search_provider.search(f"site:{host} {removed.title}",
                                                max_results=CITED_SEARCH_RESULTS,
                                                language=guess_language(removed.url))
            ev.extra_hits = [h.model_copy(update={"source": "cited"}) for h in hits]
            ev.source_status = "leads_only" if ev.extra_hits else "not_located"
        else:
            ev.source_status = "not_located"

    ev.reasoning = describe(ev)
    return ev


# --- extraction (pure) -------------------------------------------------------

def extract_links(content: str, removed_url: str) -> List[str]:
    """Outbound, article-shaped URLs present in the archived content (hrefs or bare URLs), off
    the publisher's own host and off social/share hosts; de-duplicated, document order."""
    out: List[str] = []
    seen = set()
    for u in _HREF_RE.findall(content) + _BARE_URL_RE.findall(content):
        u = html_lib.unescape(u).rstrip(".,;:")
        host = host_of(u)
        if not host or any(host == h or host.endswith("." + h) for h in _NOISE_HOSTS):
            continue
        if same_publisher(u, removed_url) or not looks_like_an_article_url(u):
            continue
        key = normalize_url(u)
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def extract_attribution_phrases(prose: str) -> List[str]:
    """Sentences (head + tail of the article) that look like a credit to another outlet."""
    window = prose[:HEAD_CHARS] + "\n" + (prose[-TAIL_CHARS:] if len(prose) > HEAD_CHARS else "")
    out: List[str] = []
    seen = set()
    for rx in _ATTRIBUTION_RES:
        for m in rx.finditer(window):
            s = " ".join(m.group(0).split())
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return out[:MAX_PHRASES_IN_PROMPT]


def fold(s: str) -> str:
    """Lower-case, diacritics stripped, Turkish dotless/dotted i unified, whitespace collapsed."""
    s = " ".join(s.split()).replace("ı", "i").replace("İ", "i")
    s = unicodedata.normalize("NFKD", s).casefold()
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def host_for(name: str) -> Optional[str]:
    """Host for a source NAME as written in the text ("Sözcü'nün", "İHA", "Anadolu Ajansı")."""
    f = " ".join(fold(name).replace("'", " ").replace("’", " ").split())
    stripped = " ".join(re.sub(r"\b(gazetesi|gazete|haber ajansi|ajansi|nin|nun|un|in)\b", " ", f).split())
    for cand in (f, stripped):
        if cand in KNOWN_SOURCES:
            return KNOWN_SOURCES[cand]
    for key, host in sorted(KNOWN_SOURCES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", f):
            return host
    return None


# --- grounding (pure) --------------------------------------------------------

def ground_claim(raw: SourceResult, prose: str, links: List[str],
                 removed_url: str) -> Tuple[str, str, str, str, List[str]]:
    """Keep only what the text supports. Returns (kind, name, url, quote, dropped_notes)."""
    dropped: List[str] = []
    kind_raw = (raw.source_kind or "").strip()
    kind = kind_raw if kind_raw in SOURCE_KINDS else "unknown"
    if kind_raw and kind != kind_raw:
        dropped.append(f"kind={kind_raw!r} is not a known kind")
    text_f = fold(prose)

    name = (raw.source_name or "").strip()
    if name and fold(name) not in text_f:
        dropped.append(f"name={name!r} not found in the text")
        name = ""

    url = (raw.source_url or "").strip()
    if url:
        allowed = {normalize_url(link): link for link in links}
        if normalize_url(url) not in allowed:
            dropped.append(f"url={url!r} not among the links found in the text")
            url = ""
        elif same_publisher(url, removed_url):
            dropped.append(f"url={url!r} is the publisher's own page, not a source")
            url = ""
        else:
            url = allowed[normalize_url(url)]

    quote = " ".join((raw.attribution_quote or "").split())
    if quote and fold(quote)[:50] not in text_f:
        dropped.append("attribution quote not found in the text")
        quote = ""

    if kind in ("agency", "other_outlet") and not name and not url:
        kind = "unknown"
    if kind in ("self", "unknown"):
        name = url = ""
        if kind == "self":
            quote = ""
    return kind, name, url, quote, dropped


# --- prompt & description ----------------------------------------------------

def build_prompt(removed: Article, prose: str, links: List[str], phrases: List[str]) -> str:
    head = prose[:HEAD_CHARS]
    tail = prose[-TAIL_CHARS:] if len(prose) > HEAD_CHARS + TAIL_CHARS else ""
    links_text = "\n".join(f"- {u}" for u in links[:MAX_LINKS_IN_PROMPT]) or "(none)"
    phrases_text = "\n".join(f"- {p}" for p in phrases) or "(none)"
    return (
        f"REMOVED ARTICLE\nPublisher host: {host_of(removed.url)}\nTitle: {removed.title}\n"
        f"Opening text: {head}\n"
        + (f"Closing text: {tail}\n" if tail else "")
        + f"\nLINKS FOUND IN THE TEXT (off the publisher's own site):\n{links_text}\n"
        f"\nATTRIBUTION-LIKE PHRASES FOUND IN THE TEXT:\n{phrases_text}"
    )


def describe(ev: SourceEvidence) -> str:
    itself = {
        "live_intact": f"The article itself is LIVE at its original URL with "
                       f"{(ev.self_containment or 0):.0%} of the archived text present.",
        "live_other": "The original URL answers 200 but does not carry the archived text "
                      "(a soft-404, a replacement page, or unmeasurable).",
        "dead_or_unreachable": "The original URL does not answer with a readable page.",
    }.get(ev.self_status, "The original URL was not probed.")
    if ev.model_failed:
        cited = "The source-finding model failed, so the cited source is unknown."
    elif ev.source_kind == "self":
        cited = "The text credits no agency or other outlet: the reporting is the publisher's own."
    elif ev.source_kind == "unknown":
        cited = "The text does not identify where the reporting originated."
    else:
        who = f"{ev.source_kind.replace('_', ' ')} {ev.source_name}".strip()
        status = {
            "live": f"its copy at {ev.source_url} is LIVE"
                    + (f" ({ev.source_containment:.0%} of the archived text present)"
                       if ev.source_containment is not None else ""),
            "dead": f"its copy at {ev.source_url} is GONE ({ev.dead_source.reason if ev.dead_source else 'dead'}) — RED FLAG",
            "unreadable": f"its copy at {ev.source_url} could not be read (blocked or non-article) — not evidence",
            "leads_only": f"no URL was cited; {len(ev.extra_hits)} lead(s) from site:{ev.source_host} were passed to the web-search step",
            "not_located": "no URL was cited and the outlet could not be searched",
        }.get(ev.source_status, "")
        cited = f"The text credits {who}; {status}."
    return f"{itself} {cited}"
