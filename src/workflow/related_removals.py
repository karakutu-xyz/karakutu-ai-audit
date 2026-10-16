"""Step 4 — related removals: did OTHER articles on the same subject vanish from the archive
within ±WINDOW_DAYS of this one?

Why (operator's rule): an article can be individually out of scope — a sports note, a routine
business item — and still be part of a pattern. When several articles about one financial story
disappear from several publishers within two weeks, and one of them has already been
human-decided *censored*, the pattern is a reason to keep the audit in scope and to tell the
reviewer, even when the topic alone would not qualify. A deterministic-only matcher fires far
too often (single generic words link unrelated stories); the prefilter plus model confirmation
below keeps the flag to real clusters.

How (deterministic first, one bounded model judgement last, everything grounded in code):
  1. KEY TERMS from the title + head of the text — multi-word proper names, 3–6-letter acronyms,
     capitalised words that recur, content stems ≥ 3 chars — Turkish-folded (ı→i, ş→s …) so URL
     slugs match too. Generic tokens (cities, ministries, weekdays, section names) never count.
  2. CANDIDATES = every audit the archive created in [anchor − 14 d, anchor + 14 d]
     (`KarakutuClient.audits_created_between`, all statuses), minus this article's own URL
     variants and same-title duplicates. The anchor is the article's own DETECTION moment (first
     dead check after the last 200) when the archive knows it, else now.
  3. PREFILTER: a candidate's title + slug must share a multi-word name or an acronym, or a single
     entity PLUS a content stem, or ≥ 3 stems, or two RARE stems. A term that hits more than
     GENERIC_MAX candidates is dropped as generic for this window — which also defuses a
     publisher-wide pruning wave.
  4. MODEL CONFIRMATION: the local model sees the article (title, gist, text head, publish date)
     and the numbered prefiltered headlines with THEIR publish dates and answers with numbers or
     NONE — a PLAIN-TEXT answer, not a tool call (gemma-4-12b returns empty tool arguments for
     this list-selection question; the plain answer is exact).
     Only numbers that exist in the list shown are accepted. Nothing is confirmed without it.
  5. FLAG when ≥ MIN_MEMBERS confirmed members come from ≥ MIN_PUBLISHERS publishers, or when any
     confirmed member is already human-decided censored.

Never raises; degrades to `available=False`. The pipeline turns `flag` into
`AuditFlag.RELATED_REMOVALS` and, when step 3 said out of scope, keeps the audit going.
"""
import html
import logging
import re
import time
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from src.api.models import Article
from src.clients.karakutu import KarakutuClient, get_client as get_karakutu_client
from src.search.fetch import normalize_url, prose_of, same_article_variant
from src.workflow.prompts import RELATED_REMOVALS_PROMPT

logger = logging.getLogger(__name__)

WINDOW_DAYS = 14
MIN_MEMBERS = 3          # confirmed same-subject removals needed for the flag …
MIN_PUBLISHERS = 2       # … from at least this many distinct publishers
GENERIC_MAX = 6          # a term matching more candidates than this is generic in this window
RARE_MAX = 3             # a stem matching at most this many candidates is "rare"
MAX_TO_MODEL = 15        # headlines shown to the model at most
TEXT_HEAD_CHARS = 900    # of the article's prose used for key terms
MODEL_TEXT_CHARS = 500   # of the article's prose shown to the model
RELATED_MAX_TOKENS = 40

_FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇâÂîÎûÛ", "iisSgGuUoOcCaAiIuU")
STOP = set("""bir bu şu ve ile için gibi daha çok en de da ki mi mı mu mü ne neden nasıl olan olarak oldu olduğu
oldugu sonra önce once kadar göre gore ise ama ancak fakat veya ya hem her hiç hic tüm tum bazı bazi diğer diger yeni
son ilk büyük buyuk küçük kucuk yıl yil yıllık yillik ay gün gun hafta bugün bugun dün dun yarın yarin saat dakika
milyon milyar bin yüzde yuzde lira dolar euro açıkladı acikladi açıklama aciklama dedi söyledi soyledi belirtti
ifade etti oldu olacak edildi edilen yapılan yapilan yaptı yapti geldi gelen verdi veren aldı aldi alan çıktı cikti
ortaya haber haberi haberleri gündem gundem türkiye turkiye türkiyede turkiyede istanbul ankara izmir dünya dunya
başkanı baskani başkan baskan hakkında hakkinda ilgili üzerine uzerine arasında arasinda nedeniyle karşı karsi
tarafından tarafindan ardından ardindan sonrası sonrasi öncesi oncesi içinde icinde dışında disinda spor futbol
maç mac maçı maci takım takim""".split())
GENERIC_ENTITIES = {
    "turkiye", "istanbul", "ankara", "izmir", "abd", "avrupa", "cumhurbaskani", "erdogan", "akp", "chp", "mhp",
    "meclis", "bakan", "bakanligi", "bakani", "valilik", "belediye", "belediyesi", "universitesi", "son", "dakika",
    "tl", "usd", "euro", "pazar", "pazartesi", "sali", "carsamba", "persembe", "cuma", "cumartesi",
    "ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul", "ekim", "kasim", "aralik",
    "gazete", "gazetesi", "haberleri", "haber", "cumhuriyet", "bassavciligi", "savciligi", "mudurlugu", "baskanligi",
    "baskani", "yardimcisi", "milletvekili", "emniyet", "meydani", "limani", "sube", "genel", "kurulu", "idaresi",
    "resmi", "mahkemesi", "mahkeme", "hastanesi", "kaymakamligi", "il", "ilce", "kadin", "erkek", "cocuk", "polis",
    "video", "canli", "spor", "ekonomi", "dunya", "gundem", "yasam", "saglik", "magazin", "makro", "kultur", "sanat",
}
GENERIC_BIGRAM_PARTS = {"haberleri", "gazete", "gazetesi", "resmi", "spor", "ekonomi", "makro", "son", "dakika",
                        "genel", "kurulu"}


class RelatedRemoval(BaseModel):
    """One other removal in the window that shares this article's subject."""
    audit_id: int
    title: str = ""
    url: str = ""
    newspaper: str = ""
    created_at: Optional[int] = None
    publish_date: Optional[str] = None
    status: Optional[int] = None
    decision: Optional[str] = None
    shared_terms: List[str] = Field(default_factory=list)


class RelatedRemovalsEvidence(BaseModel):
    available: bool = False
    reason: str = ""
    anchor_ts: Optional[int] = None
    anchor_source: str = ""                  # first_dead_check | now
    window_days: int = WINDOW_DAYS
    window_size: int = 0                     # candidate audits in the window (after self-exclusion)
    entities: List[str] = Field(default_factory=list)
    generic_dropped: List[str] = Field(default_factory=list)
    prefiltered: List[RelatedRemoval] = Field(default_factory=list)
    members: List[RelatedRemoval] = Field(default_factory=list)   # model-confirmed
    publishers: Dict[str, int] = Field(default_factory=dict)
    human_censored: List[int] = Field(default_factory=list)       # member audit ids a human decided censored
    model_confirmed: bool = False
    model_failed: bool = False
    flag: bool = False
    kept_in_scope: bool = False              # set by the pipeline: step 3 said out of scope, the flag kept it


# --- key terms ------------------------------------------------------------------------------

def fold(s: str) -> str:
    return (s or "").translate(_FOLD).lower()


def _tokens(s: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", fold(s))


def key_terms(title: str, text: str) -> Tuple[Dict[str, str], Set[str], Dict[str, str]]:
    """(entities folded→surface, content stems, entity kinds) from the title and the text head."""
    head = re.sub(r"\s+", " ", html.unescape(text or ""))[:TEXT_HEAD_CHARS]
    ents: Dict[str, str] = {}
    kinds: Dict[str, str] = {}
    stems: Counter = Counter()
    single_counts: Counter = Counter()
    single_surface: Dict[str, str] = {}
    for chunk in (title or "", head):
        for sent in re.split(r"(?<=[.!?:;])\s+|\n", chunk):
            toks = sent.split()
            for i, w in enumerate(toks):
                w_clean = w.strip("“”\"'’‘(),.:;!?")
                base = re.sub(r"[’'‘].*$", "", w_clean)                 # strip the Turkish apostrophe suffix
                is_acr = len(w_clean) >= 2 and w_clean.isupper() and w_clean.isalpha()
                is_cap = w_clean[:1].isupper() and not w_clean.isupper() and len(w_clean) >= 3
                if is_acr or (is_cap and i > 0):
                    f = fold(base)
                    if f and f not in GENERIC_ENTITIES and f not in STOP and len(f) >= 3:
                        if is_acr and 3 <= len(f) <= 6:
                            ents.setdefault(f, base)
                            kinds[f] = "acronym"
                        elif is_cap:
                            single_counts[f] += 1
                            single_surface.setdefault(f, base)
                    nxt = toks[i + 1].strip("“”\"'’‘(),.:;!?") if i + 1 < len(toks) else ""
                    nxt_base = re.sub(r"[’'‘].*$", "", nxt)
                    if is_cap and nxt_base[:1].isupper() and not nxt_base.isupper() and len(nxt_base) >= 3:
                        a, b = fold(base), fold(nxt_base)
                        if (a not in GENERIC_BIGRAM_PARTS and b not in GENERIC_BIGRAM_PARTS
                                and not (a in GENERIC_ENTITIES and b in GENERIC_ENTITIES)):
                            ents.setdefault(a + " " + b, f"{base} {nxt_base}")
                            kinds[a + " " + b] = "bigram"
        for t in _tokens(chunk):
            # 3-letter words matter in Turkish finance/politics ("fon", "kur"); the rarity filter and
            # the bounded-suffix match keep them from becoming wildcards
            if len(t) >= 3 and t not in STOP and not t.isdigit():
                stems[t[:6] if len(t) > 6 else t] += 1
    for f, n in single_counts.items():                       # a lone capitalised word counts only when it recurs
        if n >= 2:
            ents.setdefault(f, single_surface[f])
            kinds.setdefault(f, "single")
    return ents, set(stems), kinds


def candidate_text(row: dict) -> str:
    slug = re.sub(r"^https?://[^/]+/", " ", row.get("url") or "").replace("-", " ").replace("/", " ")
    return fold((row.get("title") or "") + " " + slug)


def term_matches(term: str, ctext: str) -> bool:
    toks = set(re.findall(r"[a-z0-9]+", ctext))
    if " " in term:
        return term in ctext
    if term in toks:
        return True
    if len(term) >= 4:
        return any(w.startswith(term) for w in toks)
    return any(w.startswith(term) and len(w) <= len(term) + 5 for w in toks)   # short stems: bounded suffix


# --- prefilter ------------------------------------------------------------------------------

def window_candidates(removed: Article, rows: List[dict], anchor_ts: int, *, window_days: int = WINDOW_DAYS) -> List[dict]:
    """Rows created within ±window_days of the anchor, minus this article's own URL variants and
    same-title duplicates, de-duplicated by article id / URL / title (oldest first)."""
    lo, hi = anchor_ts - window_days * 86400, anchor_ts + window_days * 86400
    own_title = fold(removed.title).strip()
    out, seen = [], set()
    for c in sorted(rows, key=lambda c: c.get("created_at") or 0):
        if not (lo <= (c.get("created_at") or 0) <= hi):
            continue
        url = c.get("url") or ""
        if not url or normalize_url(url) == normalize_url(removed.url) or same_article_variant(removed.url, url):
            continue
        title_key = fold(c.get("title") or "").strip()
        if title_key and title_key == own_title:
            continue
        key = c.get("article_id") or normalize_url(url)
        if key in seen or (title_key and title_key in seen):
            continue
        seen.add(key)
        seen.add(title_key)
        out.append(c)
    return out


def prefilter(removed: Article, cands: List[dict], *, text: Optional[str] = None
              ) -> Tuple[List[RelatedRemoval], Dict[str, str], List[str]]:
    """(prefiltered candidates with their shared terms, entities, generic terms dropped)."""
    prose = text if text is not None else (prose_of(removed.content or "") or removed.content or "")
    ents, stems, kinds = key_terms(removed.title, prose)
    ctexts = {c["audit_id"]: candidate_text(c) for c in cands}

    def hits(term):
        return [c for c in cands if term_matches(term, ctexts[c["audit_id"]])]

    ent_hits = {e: hits(e) for e in ents}
    stem_hits = {s: hits(s) for s in stems}
    generic = {t for t, h in {**ent_hits, **stem_hits}.items() if len(h) > GENERIC_MAX}
    picked: List[RelatedRemoval] = []
    for c in cands:
        ct = ctexts[c["audit_id"]]
        shared_e = [ents[e] for e in ents if e not in generic and term_matches(e, ct)]
        strong = [e for e in ents if e not in generic and kinds.get(e) in ("bigram", "acronym") and term_matches(e, ct)]
        shared_s = [s for s in stems if s not in generic and term_matches(s, ct)]
        rare_pair = sum(1 for s in shared_s if len(stem_hits[s]) <= RARE_MAX) >= 2
        if strong or (shared_e and shared_s) or len(shared_s) >= 3 or rare_pair:
            picked.append(RelatedRemoval(
                audit_id=int(c["audit_id"]), title=c.get("title") or "", url=c.get("url") or "",
                newspaper=c.get("newspaper") or "", created_at=c.get("created_at"),
                publish_date=c.get("publish_date"), status=c.get("status"), decision=c.get("decision"),
                shared_terms=(shared_e or shared_s)[:6]))
    return picked, ents, sorted(generic)


# --- model confirmation ------------------------------------------------------------------

def build_agent(model: Model) -> Agent[None, str]:
    """A plain-TEXT agent (result_type=str): the model answers with numbers or NONE; parsing and
    grounding happen in code. Not a tool call — see the module docstring."""
    return Agent(model, result_type=str, system_prompt=RELATED_REMOVALS_PROMPT)


def build_prompt(removed: Article, members: List[RelatedRemoval], *, gist: str = "",
                 publish_date: Optional[str] = None, text: Optional[str] = None) -> str:
    prose = text if text is not None else (prose_of(removed.content or "") or removed.content or "")
    head = re.sub(r"\s+", " ", html.unescape(prose))[:MODEL_TEXT_CHARS]
    listing = "\n".join(f"{i}. [{m.newspaper}, published {(m.publish_date or '?')[:10]}] {m.title[:120]}"
                        for i, m in enumerate(members, 1))
    pub = publish_date or (removed.published_at.date().isoformat() if removed.published_at else "?")
    return (f"ARTICLE title: {removed.title}\nARTICLE published: {str(pub)[:10]}\n"
            f"ARTICLE summary: {gist or ''}\nARTICLE text (start): {head}\n\n"
            f"OTHER REMOVED HEADLINES:\n{listing}\n\n"
            f"Which numbers are about the same subject or event as the article?")


def parse_selection(answer: str, n: int) -> Set[int]:
    """Grounded parse: only numbers 1..n; NONE → empty."""
    text = (answer or "").strip()
    if not text or "NONE" in text.upper():
        return set()
    return {int(x) for x in re.findall(r"\d+", text) if 1 <= int(x) <= n}


def decide_flag(members: List[RelatedRemoval]) -> Tuple[bool, Dict[str, int], List[int]]:
    pubs = Counter(m.newspaper for m in members)
    censored = [m.audit_id for m in members if m.decision == "censored"]
    flag = (len(members) >= MIN_MEMBERS and len(pubs) >= MIN_PUBLISHERS) or bool(censored)
    return flag, dict(pubs), censored


def describe(ev: RelatedRemovalsEvidence) -> str:
    """One paragraph for `findings` / the comment. Always says it is a pointer, not a finding."""
    if not ev.available:
        return f"Related removals: not checked ({ev.reason})."
    if not ev.flag:
        n = len(ev.members)
        return (f"Related removals: no pattern — {n} other removal(s) in the archive within ±{ev.window_days} days "
                f"were confirmed to be about the same subject (needed ≥ {MIN_MEMBERS} from ≥ {MIN_PUBLISHERS} publishers"
                f"{', or one human-decided censored' if n else ''}).")
    def when(m):
        return time.strftime("%d %b", time.gmtime(m.created_at)) if m.created_at else "?"
    items = "; ".join(f"{when(m)} {m.newspaper}: {m.title[:90]}"
                      + (" [human decision: censored]" if m.decision == "censored" else "") for m in ev.members)
    return (f"RELATED REMOVALS (pattern flag): {len(ev.members)} other article(s) on the same subject disappeared "
            f"within ±{ev.window_days} days of this one, from {len(ev.publishers)} publisher(s): {items}. "
            f"This is a pointer for the reviewer, not a finding — related removals can also be a publisher "
            f"pruning a section.")


async def run(removed: Article, agent: Optional[Agent], *, client: Optional[KarakutuClient] = None,
              gist: str = "", now: Optional[int] = None) -> RelatedRemovalsEvidence:
    """Anchor → window → prefilter → model confirmation → flag. Never raises."""
    ev = RelatedRemovalsEvidence()
    client = client or get_karakutu_client()
    if client is None or not client.enabled:
        ev.reason = "archive client disabled (no wallet keyfile)"
        return ev
    now = int(now or time.time())
    try:
        record = await client.get_article_by_url(removed.url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("related_removals: article lookup failed for %s: %s", removed.url, type(exc).__name__)
        record = None
    anchor = None
    if record is not None:
        try:
            anchor = await client.first_dead_check(record.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("related_removals: check history failed for %s: %s", removed.url, type(exc).__name__)
    ev.anchor_ts, ev.anchor_source = (int(anchor), "first_dead_check") if anchor else (now, "now")
    lo, hi = ev.anchor_ts - WINDOW_DAYS * 86400, min(ev.anchor_ts + WINDOW_DAYS * 86400, now)
    try:
        rows = await client.audits_created_between(lo, hi)
    except Exception as exc:  # noqa: BLE001
        logger.warning("related_removals: window listing failed: %s", type(exc).__name__)
        rows = None
    if rows is None:
        ev.reason = "the archive's audit list could not be read"
        return ev
    ev.available = True
    cands = window_candidates(removed, rows, ev.anchor_ts)
    ev.window_size = len(cands)
    prose = prose_of(removed.content or "") or removed.content or ""
    picked, ents, generic = prefilter(removed, cands, text=prose)
    ev.entities, ev.generic_dropped, ev.prefiltered = list(ents.values()), generic, picked[:MAX_TO_MODEL]
    if not ev.prefiltered:
        ev.reason = (f"{len(cands)} other removal(s) in the archive within ±{WINDOW_DAYS} days; none shares this "
                     "article's key terms")
        return ev
    if agent is None:
        ev.model_failed = True
        ev.reason = f"{len(ev.prefiltered)} candidate(s) shared key terms but no model was available to confirm them"
        return ev
    publish_date = (record.publish_date if record is not None and record.publish_date else None)
    prompt = build_prompt(removed, ev.prefiltered, gist=gist, publish_date=publish_date, text=prose)
    try:
        # temperature 0: the same list must give the same answer run to run (at the default
        # temperature a member can drop in and out between otherwise identical calls)
        result = await agent.run(prompt, model_settings={"max_tokens": RELATED_MAX_TOKENS, "temperature": 0})
        picked_idx = parse_selection(str(result.data), len(ev.prefiltered))
        ev.model_confirmed = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("related_removals: model confirmation failed for %s: %s: %s", removed.url, type(exc).__name__, exc)
        ev.model_failed = True
        ev.reason = (f"{len(ev.prefiltered)} candidate(s) shared key terms but the model could not confirm them "
                     f"({type(exc).__name__}); nothing flagged")
        return ev
    ev.members = [ev.prefiltered[i - 1] for i in sorted(picked_idx)]
    ev.flag, ev.publishers, ev.human_censored = decide_flag(ev.members)
    ev.reason = (f"{len(cands)} removal(s) in the window; {len(ev.prefiltered)} shared key terms; the model confirmed "
                 f"{len(ev.members)} as the same subject from {len(ev.publishers)} publisher(s)"
                 + (f"; {len(ev.human_censored)} already human-decided censored" if ev.human_censored else "")
                 + (" → PATTERN FLAG" if ev.flag else " → no flag"))
    return ev
