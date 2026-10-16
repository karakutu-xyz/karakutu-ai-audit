"""Fetch candidate search hits and keep only the ones that are actually alive.

Stage 3 used to judge "does this story survive elsewhere?" from DuckDuckGo's title and
snippet alone. A search index is stale: it happily returns a URL long after the page
behind it is gone, so the model was asked to guess liveness from a one-line description
and could not. Worse, the removed article's own (now-dead) URL is usually still indexed,
so the pipeline could cite the corpse as proof the story was alive.

This module closes that gap: it drops the removed article's own URL, actually fetches
each remaining candidate, keeps only those that return 2xx, and hands the model the real
page text instead of a snippet.

The network-free URL helpers live in `urls.py`; they are re-exported below so callers can
keep importing them (and `normalize_url`, `same_publisher`, …) from `src.search.fetch`.
"""
import asyncio
import html as html_lib
import logging
import ipaddress
import re
import socket
import time
from typing import List, Optional

import anyio
import httpcore
import httpx
from pydantic import BaseModel

from src.search.base import SearchResult
from src.search.urls import (  # re-exported — see module docstring
    NON_ARTICLE_SUFFIXES,
    guess_language,
    host_of,
    is_junk_suffix_variant,
    is_public_ip,
    is_public_web_url,
    is_same_url,
    looks_like_an_article_url,
    normalize_url,
    same_article_variant,
    same_publisher,
)

logger = logging.getLogger(__name__)

__all__ = [
    "fetch_candidates",
    "probe_removed_url",
    "html_to_text",
    "markdown_to_text",
    "is_same_url",
    "normalize_url",
    "same_article_variant",
    "is_junk_suffix_variant",
    "same_publisher",
    "host_of",
    "guess_language",
    "looks_like_an_article_url",
    "MIN_TEXT_CHARS",
    "MAX_PAGE_CHARS",
    "MAX_STORED_PAGE_CHARS",
    "prose_of",
    "MAX_PAGES_IN_PROMPT",
    "MAX_REMOVED_CHARS",
    "GONE_STATUSES",
    "is_public_web_url",
    "NON_ARTICLE_SUFFIXES",
    "DeadCandidate",
    "FetchOutcome",
    "LivePage",
    "UnreadableCandidate",
]

# --- tuning constants --------------------------------------------------------

FETCH_TIMEOUT_S = 10.0
MAX_CONCURRENCY = 5
# Prompt budget. Stage 3 sends the removed article plus these pages. This is sized for a
# small local context (LM Studio default 8192 tokens): an over-long prompt makes the server
# return an empty response and then fail with "request exceeds the available context size".
# Characters are a poor proxy for tokens — Turkish tokenizes densely — so keep a wide margin.
MAX_PAGE_CHARS = 800
MAX_PAGES_IN_PROMPT = 3
# Cap the HTML body we read. A news page is tens of KB; this bounds a mislabelled giant.
MAX_HTML_BYTES = 2_000_000
# The removed article's own text is also capped for the same reason (stage 1 sees it in full).
MAX_REMOVED_CHARS = 1200
# LivePage stores the FULL extracted prose (capped only to bound a pathological page):
# stage 3 MEASURES text containment on it (relocation vs sibling, src/search/textmatch.py),
# and measuring against a prompt-sized excerpt would make every long republication look
# like a partial fit. Prompt truncation happens in web_search._build_prompt, NEVER here.
MAX_STORED_PAGE_CHARS = 120_000
# A <p> shorter than this is a caption, a byline, a menu item or a "share this" — not prose.
MIN_PARAGRAPH_CHARS = 40
# Some newsrooms serve a 200 shell for a dead article. A page whose extracted text is
# shorter than this is treated as a soft-404 rather than a surviving copy.
MIN_TEXT_CHARS = 200
# A self-identifying bot UA gets 403'd by Turkish news sites: one outlet answered 403 to
# "karakutu-audit/1.0" and 200 to a browser UA for the very same article — so the pipeline
# was silently discarding a live relocated copy it should have found.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
# Readable prose comes as HTML — or markdown: some publishers serve article pages as
# `content-type: text/markdown` (measured; dropping those as "not HTML" was
# pure recall loss). Everything else is rejected: a PDF answers 200 and has no <p>, so it
# once fell through to the flattened-bytes path, cleared the text floor, and was handed to
# the model as an article — which is how an unrelated municipal budget PDF once became
# "the only retrieved page" for a news story.
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_MARKDOWN_TYPES = ("text/markdown", "text/x-markdown")
_READABLE_TYPES = _HTML_TYPES + _MARKDOWN_TYPES
# The ONLY statuses that count as evidence a copy is gone. Deliberately narrow.
#   404 Not Found, 410 Gone — the server states the resource is not there.
# Everything else that isn't a success is "we couldn't read it", NOT "it was deleted":
#   403 — bot-blocked. Several publishers 403 our fetcher while serving the
#         article fine to a browser. Counting this as gone would let any bot-hostile
#         publisher manufacture a censorship flag.
#   401/429/5xx — auth walls, rate limits, server trouble. Say nothing about deletion.
GONE_STATUSES = frozenset({404, 410})


# --- candidate models --------------------------------------------------------


class LivePage(BaseModel):
    """A candidate that was fetched, answered 2xx, and yielded readable prose."""
    url: str          # the URL we requested (as it appeared in the search hit)
    final_url: str    # after redirects — may differ, e.g. a publisher moved the piece
    status: int
    title: str        # from the search hit; the index's title is good enough to label it
    text: str         # extracted body prose — FULL (see MAX_STORED_PAGE_CHARS), not prompt-truncated


class DeadCandidate(BaseModel):
    """A candidate that answered an HTTP error, or did not answer at all.

    This is the ONLY class of drop that counts as evidence a copy existed and is now gone.
    Carries the search hit's title/snippet, since that is all we will ever know about it.
    """
    url: str
    title: str
    snippet: str
    status: Optional[int] = None   # None when the host never answered
    reason: str
    # Copied from the search hit. "archive"-sourced dead candidates are NOT evidence a copy
    # is gone: the archive catalogues removed articles, so its candidates being dead is
    # near-tautological (see SearchResult.source in base.py).
    source: str = "web"


class UnreadableCandidate(BaseModel):
    """Fetched fine, but we could not read an article out of it (a PDF, a JS shell).

    Explicitly NOT evidence of anything. Keeping this separate from DeadCandidate is what
    stops a JavaScript-rendered page from being mistaken for a deleted one.
    """
    url: str
    title: str
    reason: str


class FetchOutcome(BaseModel):
    live: List[LivePage] = []
    dead: List[DeadCandidate] = []
    unreadable: List[UnreadableCandidate] = []
    excluded_self: List[str] = []   # hits that were the removed article's own URL
    # Hits that were the removed article's own URL in disguise — a junk-suffix slug or the
    # same numeric article id under another slug — and did not turn out to be a LIVE page.
    # A false POSSIBLE_CENSORSHIP flag typically comes from such a variant (a slug with a junk
    # suffix glued on; `/12345/` re-slugged) counted as a "dead same-story copy". A live variant
    # is NOT listed here: it is the relocation (some publishers keep the id across sections,
    # e.g. /gundem/ -> /haber/).
    excluded_variant: List[str] = []
    # Candidates that fetched fine but whose FINAL URL (after redirects) is the removed
    # article's own URL, or a same-article variant of it. A classic false MOVED
    # verdict is exactly this — a publisher that 301s ANY slug
    # spelled under an article id to the canonical URL, so the pipeline fetched a variant,
    # followed the redirect, and measured containment 1.0 against the removed article
    # ITSELF. A page that IS the removed article can never be a match, a relocation, or
    # dead evidence.
    excluded_redirect_self: List[str] = []


# --- HTML text extraction ----------------------------------------------------


# --- linear tag scanning ------------------------------------------------------------------
#
# Every helper below reproduces a regex EXACTLY (same matches, same replacements) while staying
# linear in the input size. The regexes they replace — `<[^>]+>`, `<p\\b[^>]*>`,
# `<(script|style|noscript)[^>]*>.*?</\\1>` — rescan to the end of the input from every `<`
# when the closing `>` never comes, which is quadratic: a page of unclosed tags within the
# MAX_HTML_BYTES cap pinned the event loop for minutes. A `[^>]*` class can never cross a `>`,
# so the only candidate close is the FIRST `>` after the open — `str.find` gives it directly,
# and once no `>` remains nothing further can match, so the scan stops. Close tags are searched
# case-insensitively on the ORIGINAL string, never on a lower-cased copy: `str.lower()` changes
# the length of some characters (Turkish İ becomes two code points) and would shift indices.

_HIDDEN_NAME_RE = re.compile(r"<(script|style|noscript)", re.I)
_HIDDEN_CLOSE_RE = {name: re.compile(rf"</{name}>", re.I) for name in ("script", "style", "noscript")}
_P_NAME_RE = re.compile(r"<p\b", re.I)
_P_CLOSE_RE = re.compile(r"</p>", re.I)


def _strip_all_tags(text: str) -> str:
    """`re.sub(r"<[^>]+>", " ", text)`, linear."""
    out: List[str] = []
    pos = 0
    while True:
        i = text.find("<", pos)
        if i < 0:
            break
        j = text.find(">", i + 1)
        if j < 0:
            break
        if j == i + 1:            # "<>" has no body: not a tag, the regex moves on
            out.append(text[pos:i + 1])
            pos = i + 1
            continue
        out.append(text[pos:i])
        out.append(" ")
        pos = j + 1
    out.append(text[pos:])
    return "".join(out)


def _has_markup(text: str) -> bool:
    """`re.search(r"<[a-zA-Z][^>]*>", text) is not None`, linear."""
    last_gt = text.rfind(">")
    if last_gt < 0:
        return False
    pos = 0
    while True:
        i = text.find("<", pos)
        if i < 0 or i >= last_gt:
            return False
        if i + 1 < len(text) and text[i + 1].isascii() and text[i + 1].isalpha():
            return True             # a `>` exists after it (last_gt > i), so the class closes
        pos = i + 1


def _remove_hidden_blocks(markup: str) -> str:
    """`re.sub(r"<(script|style|noscript)[^>]*>.*?</\\1>", " ", markup, flags=re.S | re.I)`, linear.

    An open tag whose close never comes is kept, and the scan continues after it.
    """
    out: List[str] = []
    pos = 0
    no_close_after: dict = {}  # name -> position after which no close tag exists
    while True:
        m = _HIDDEN_NAME_RE.search(markup, pos)
        if m is None:
            break
        gt = markup.find(">", m.end())
        if gt < 0:
            break                   # no `>` left anywhere: no open tag can complete
        name = m.group(1).lower()
        open_end = gt + 1
        if name in no_close_after and open_end >= no_close_after[name]:
            out.append(markup[pos:open_end])
            pos = open_end
            continue
        close = _HIDDEN_CLOSE_RE[name].search(markup, open_end)
        if close is None:
            no_close_after[name] = open_end
            out.append(markup[pos:open_end])
            pos = open_end
            continue
        out.append(markup[pos:m.start()])
        out.append(" ")
        pos = close.end()
    out.append(markup[pos:])
    return "".join(out)


def _paragraph_bodies(html: str) -> List[str]:
    """`re.findall(r"<p\\b[^>]*>(.*?)</p>", html, flags=re.S | re.I)`, linear: each open pairs with
    the next close; an open without a close ends the scan (no later open can close either)."""
    bodies: List[str] = []
    pos = 0
    while True:
        m = _P_NAME_RE.search(html, pos)
        if m is None:
            break
        gt = html.find(">", m.end())
        if gt < 0:
            break
        close = _P_CLOSE_RE.search(html, gt + 1)
        if close is None:
            break
        bodies.append(html[gt + 1:close.start()])
        pos = close.end()
    return bodies


def _strip_tags(markup: str) -> str:
    """Flatten markup to bare text, decoding both named and numeric entities."""
    text = _remove_hidden_blocks(markup)
    text = _strip_all_tags(text)
    text = html_lib.unescape(text)  # &amp; &#x27; &#39; &nbsp; …
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(html: str) -> str:
    """Extract the ARTICLE PROSE, not the whole page.

    Flattening a news page and taking the first N characters yields the nav menu, the
    cookie banner and a sidebar of unrelated headlines — verified against real pages,
    where the first few hundred chars were only masthead and social-media links. A
    model handed that correctly reports it sees "only website navigation elements".

    So: prefer the text of <p> paragraphs, which is where body copy lives and where nav
    links do not. Keep only paragraphs long enough to be prose. Fall back to the flattened
    page when a site uses no <p> tags at all.
    """
    paragraphs = _paragraph_bodies(html)
    prose = [t for t in (_strip_tags(p) for p in paragraphs) if len(t) >= MIN_PARAGRAPH_CHARS]
    if prose:
        return "\n".join(prose)
    return _strip_tags(html)


def prose_of(text: str) -> str:
    """Article prose of a string that MAY be raw HTML; plain text passes through unchanged.

    The Karakutu archive hands the removed article's `content` as RAW HTML — subscription
    CTAs, SVG path data and all. Measuring that against a live page's extracted prose
    can depress a verbatim republication's containment far below the match threshold, so both sides
    of any text measurement must be reduced by the same extraction first.
    """
    return html_to_text(text) if _has_markup(text) else text


# --- markdown text extraction -------------------------------------------------
#
# Some publishers serve article pages as `content-type: text/markdown`. The prose is the
# text itself once the markdown syntax and every link TARGET are stripped (link text and
# image alt text stay — they are words the author wrote).

# The bracket patterns are scanned with `str.find` for the same reason as the HTML tags above:
# `[^\\]]*` / `[^)]*` / `[^>]+` rescan to the end from every `[` or `<` when the closer never
# comes. Each helper reproduces its regex exactly (first closer wins, since the class cannot
# cross it; once no closer remains nothing further can match).
_MD_REF_DEF_RE = re.compile(r"^[ \t]*\[[^\]\n]+\]:[ \t]*\S+.*$", re.M)   # no `\s*`, no newline in `[…]`:
# a class crossing newlines made every line starting with `[` (or every blank line) rescan the
# rest of the document. The prose output is the same: a reference definition sits on one line,
# and a swallowed blank line and an empty paragraph both fall below MIN_PARAGRAPH_CHARS.
_MD_AUTOLINK_OPEN_RE = re.compile(r"<https?://")
_MD_BARE_URL_RE = re.compile(r"https?://\S+")
_MD_HR_RE = re.compile(r"^[ \t]*([-*_][ \t]*){3,}$", re.M)
_MD_LINE_MARKER_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*|>+[ \t]?|[-*+][ \t]+|\d+\.[ \t]+)", re.M)
_MD_EMPHASIS_RE = re.compile(r"[*_`]+")


def _sub_bracket_links(text: str, *, prefix: str, opener: str, closer: str) -> str:
    """`re.sub(prefix + r"\\[([^\\]]*)\\]" + opener + "[^" + closer + "]*" + closer, r"\\1", text)`,
    linear. `prefix` is "!" for images or "", `opener`/`closer` are "(" / ")" for inline links and
    "[" / "]" for reference links."""
    start = prefix + "["
    out: List[str] = []
    pos = 0
    while True:
        i = text.find(start, pos)
        if i < 0:
            break
        j = text.find("]", i + len(start))
        if j < 0:
            break                                   # no `]` left: nothing can match
        if j + 1 >= len(text) or text[j + 1] != opener:
            # Fails here, and every `start` between i and j would hit the same `]`.
            out.append(text[pos:j + 1])
            pos = j + 1
            continue
        k = text.find(closer, j + 2)
        if k < 0:
            break                                   # no closer left: nothing can match
        out.append(text[pos:i])
        out.append(text[i + len(start):j])         # the link text / alt text
        pos = k + 1
    out.append(text[pos:])
    return "".join(out)


def _sub_autolinks(text: str) -> str:
    """`re.sub(r"<https?://[^>]+>", " ", text)`, linear."""
    out: List[str] = []
    pos = 0
    while True:
        m = _MD_AUTOLINK_OPEN_RE.search(text, pos)
        if m is None:
            break
        j = text.find(">", m.end())
        if j < 0:
            break
        if j == m.end():                           # `[^>]+` needs at least one character
            out.append(text[pos:m.end()])
            pos = m.end()
            continue
        out.append(text[pos:m.start()])
        out.append(" ")
        pos = j + 1
    out.append(text[pos:])
    return "".join(out)


def markdown_to_text(md: str) -> str:
    """Markdown reduced to prose: syntax markers and link targets stripped, text kept.

    Applies the same prose floor as `html_to_text`: blank-line-separated paragraphs long
    enough to be body copy, falling back to the whole cleaned text when nothing clears it.
    """
    text = _sub_bracket_links(md, prefix="!", opener="(", closer=")")      # ![alt](src) -> alt
    text = _sub_bracket_links(text, prefix="", opener="(", closer=")")     # [text](url) -> text
    text = _sub_bracket_links(text, prefix="", opener="[", closer="]")     # [text][ref] -> text
    text = _MD_REF_DEF_RE.sub(" ", text)
    text = _sub_autolinks(text)
    text = _MD_BARE_URL_RE.sub(" ", text)
    text = _MD_HR_RE.sub(" ", text)
    text = _MD_LINE_MARKER_RE.sub("", text)
    text = _MD_EMPHASIS_RE.sub("", text)
    text = html_lib.unescape(text)
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    prose = [p for p in paragraphs if len(p) >= MIN_PARAGRAPH_CHARS]
    if prose:
        return "\n".join(prose)
    return re.sub(r"\s+", " ", text).strip()


# --- SSRF guard, network layer -------------------------------------------------
#
# `is_public_web_url` judges the URL STRING. That cannot see a public-looking name whose DNS
# answer is 10.0.0.5, nor a name that rebinds between the check and the connect. This
# backend closes both: it resolves the host itself, keeps only addresses `is_public_ip`
# accepts, and dials exactly those IPs (TLS SNI and the Host header still carry the name —
# httpcore passes them separately). It runs on EVERY connection httpx opens, so each
# redirect hop is resolved and vetted before a single byte is sent to it.
#
# For a real news URL nothing changes: the same system resolver is asked, and the same
# default backend connects (to the resolved address instead of resolving again itself).
# The only difference is that multiple addresses are tried in order rather than raced.

# Prefix of the error a refused connection raises; `_fetch_one` turns it into a
# "refused" unreadable reason (httpx keeps the message when it maps the exception).
_DNS_REFUSAL = "ssrf-guard: refused"


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):  # `timeout` params: httpcore interface
    """An httpcore network backend that only connects to public unicast addresses."""

    def __init__(self, inner: Optional[httpcore.AsyncNetworkBackend] = None):
        self._inner = inner or httpcore.AnyIOBackend()

    async def _resolve(self, host: str, port: int, timeout: Optional[float]) -> List[str]:  # noqa: ASYNC109
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            pass
        else:
            return [str(ip)]
        try:
            with anyio.fail_after(timeout):
                infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(type(exc).__name__) from exc
        except OSError as exc:   # gaierror: the name does not resolve — plain unreachable
            raise httpcore.ConnectError(type(exc).__name__) from exc
        seen: List[str] = []
        for _family, _type, _proto, _canon, sockaddr in infos:
            addr = str(sockaddr[0]).split("%", 1)[0]
            if addr not in seen:
                seen.append(addr)
        return seen

    async def connect_tcp(self, host, port, timeout=None, local_address=None,  # noqa: ASYNC109
                          socket_options=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        addresses = await self._resolve(host, port, timeout)
        public = [a for a in addresses if is_public_ip(ipaddress.ip_address(a))]
        if not public:
            logger.warning("web_fetch: %s REFUSED at connect (resolves to no public address)",
                           host)
            raise httpcore.ConnectError(f"{_DNS_REFUSAL}: {host} resolves to no public address")
        last_exc: Optional[Exception] = None
        for addr in public:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("ConnectTimeout")
            try:
                return await self._inner.connect_tcp(addr, port, timeout=remaining,
                                                     local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # noqa: ASYNC109
        raise httpcore.ConnectError(f"{_DNS_REFUSAL}: unix sockets are never fetched")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _guarded_transport(limits: httpx.Limits, *,
                       inner: Optional[httpcore.AsyncNetworkBackend] = None) -> httpx.AsyncHTTPTransport:
    """The transport every outbound fetch uses: httpx's default one, with the pool's network
    backend swapped for `GuardedNetworkBackend`. Same TLS context, limits and HTTP/1.1."""
    transport = httpx.AsyncHTTPTransport(limits=limits)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(),
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
        network_backend=GuardedNetworkBackend(inner),
    )
    return transport


# --- fetching ----------------------------------------------------------------


async def _fetch_one(client: httpx.AsyncClient, hit: SearchResult) -> object:
    """Fetch one hit. Returns a LivePage, a DeadCandidate, or an UnreadableCandidate.

    The three-way split is the whole point. A page that answered 404 is EVIDENCE that a
    copy existed and is gone; a page we merely could not parse is NOT. Collapsing them
    would let a JavaScript-rendered site look exactly like a deleted one.
    """
    # Cheapest check first: a URL that plainly isn't a news page (a PDF, an image, a sitemap)
    # is never an article. Reject it WITHOUT fetching — this is the common case when the
    # search backend degrades and returns academic PDFs; downloading those in full is what
    # turns a sub-second fetch step into tens of seconds.
    if not looks_like_an_article_url(hit.url):
        logger.info("web_fetch: %s unreadable (non-article URL, not fetched)", hit.url)
        return UnreadableCandidate(url=hit.url, title=hit.title,
                                   reason="non-article URL (PDF/image/archive), not a news page")

    # SSRF guard. URLs reaching here can come straight from the caller (the step-4 premise
    # probe fetches `removed_article.url`, and step 4 fetches links parsed out of the
    # caller's `content`), and the per-URL outcome is reported back — so an unguarded fetch
    # makes this endpoint an internal-network probe oracle. Unreadable, never `dead`: a
    # blocked URL is evidence of nothing, and must never reach the censorship flag.
    if not is_public_web_url(hit.url):
        logger.warning("web_fetch: %s REFUSED (not a public http(s) address)", hit.url)
        return UnreadableCandidate(url=hit.url, title=hit.title,
                                   reason="refused: not a public http(s) address")

    try:
        # Stream so we can reject on the response HEADERS before pulling a large body: a PDF
        # that slips past the URL check (no suffix) is dropped after headers, not downloaded.
        async with client.stream("GET", hit.url) as resp:
            # FIRST, before the status is even interpreted: a public URL may redirect
            # anywhere, and an open redirect on a real publisher would otherwise walk us onto
            # the metadata endpoint. Checking here means such a landing can never be read
            # (no body is pulled) and can never become `dead` evidence either — a 404 from an
            # internal address must not be allowed to manufacture a censorship flag.
            #
            # Every hop's connection also passes `GuardedNetworkBackend`, which resolves the
            # name and refuses a private/link-local/loopback/CGNAT address BEFORE connecting,
            # so a redirect onto an internal target is normally never sent at all. This
            # string check stays as the second layer (e.g. for an injected test transport).
            if not is_public_web_url(str(resp.url)):
                logger.warning("web_fetch: %s REFUSED — landed on %s (not a public address)",
                               hit.url, resp.url)
                return UnreadableCandidate(
                    url=hit.url, title=hit.title,
                    reason="refused: redirected to a non-public address")

            if resp.status_code in GONE_STATUSES:
                # The server explicitly says the article is not there. THIS is evidence.
                logger.info("web_fetch: %s DEAD (HTTP %d)", hit.url, resp.status_code)
                return DeadCandidate(url=hit.url, title=hit.title, snippet=hit.snippet,
                                     status=resp.status_code, reason=f"HTTP {resp.status_code}",
                                     source=hit.source)

            if not resp.is_success:
                # 403 (bot-blocked — several publishers do this to us), 401, 429, 5xx. The
                # article may be fine and simply not served TO US; "blocked" is not "deleted".
                logger.info("web_fetch: %s unreadable (HTTP %d — blocked or erroring, not gone)",
                            hit.url, resp.status_code)
                return UnreadableCandidate(url=hit.url, title=hit.title,
                                           reason=f"HTTP {resp.status_code} (blocked/erroring, not gone)")

            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and not content_type.startswith(_READABLE_TYPES):
                # A PDF or an image answered 200. Not a dead article — just not an article.
                logger.info("web_fetch: %s unreadable (not HTML: %s)", hit.url, content_type)
                return UnreadableCandidate(url=hit.url, title=hit.title,
                                           reason=f"not HTML ({content_type})")

            # HTML: read the body, but cap it — a news page is small, and this bounds the
            # cost of a mislabelled giant response.
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) >= MAX_HTML_BYTES:
                    break
            final_url = str(resp.url)
    except Exception as exc:  # noqa: BLE001 — TLS, timeout, DNS, redirect loop…
        if isinstance(exc, httpx.ConnectError) and str(exc).startswith(_DNS_REFUSAL):
            # The name (or a redirect hop) resolved only to non-public addresses. Nothing was
            # sent to it. Unreadable, never `dead`, exactly like the string-level refusal.
            return UnreadableCandidate(url=hit.url, title=hit.title,
                                       reason="refused: resolves to a non-public address")
        # Could not reach it. Host may be gone — or the network blinked, or the site is slow,
        # or it dropped us. Ambiguous, so NOT evidence.
        logger.info("web_fetch: %s unreadable (%s — unreachable, not provably gone)",
                    hit.url, type(exc).__name__)
        return UnreadableCandidate(url=hit.url, title=hit.title,
                                   reason=f"{type(exc).__name__} (unreachable, not provably gone)")

    raw = body.decode("utf-8", errors="replace")
    text = markdown_to_text(raw) if content_type.startswith(_MARKDOWN_TYPES) else html_to_text(raw)
    if len(text) < MIN_TEXT_CHARS:
        # 200 but no prose. Could be a removed article — or a JS-rendered shell we cannot read
        # (yenicag.com.cy is exactly this). Ambiguous, so NOT dead-link evidence.
        logger.info("web_fetch: %s unreadable (200 but %d chars of prose)", hit.url, len(text))
        return UnreadableCandidate(url=hit.url, title=hit.title,
                                   reason=f"200 but only {len(text)} chars of prose")

    return LivePage(
        url=hit.url,
        final_url=final_url,
        status=200,
        title=hit.title,
        text=text[:MAX_STORED_PAGE_CHARS],
    )


async def fetch_candidates(hits: List[SearchResult], exclude_url: str) -> FetchOutcome:
    """Drop the removed article's own URL, fetch the rest, and sort the results three ways.

    Never raises. The caller needs all three buckets: `live` is evidence the story survives,
    `dead` is evidence a copy existed and is gone, and `unreadable` is evidence of nothing.
    """
    outcome = FetchOutcome()

    candidates = []
    variant_keys: set = set()
    for hit in hits:
        if is_same_url(hit.url, exclude_url):
            # The removed article is still in the index. It is never evidence of its own survival.
            outcome.excluded_self.append(hit.url)
            continue
        if is_junk_suffix_variant(hit.url, exclude_url):
            # Textually the removed article's own URL with garbage glued onto the slug
            # (an unescaped `&nbsp;` glued onto the slug). Provably self, so not even fetched.
            logger.info("web_fetch: %s excluded (variant of the removed article's own URL: "
                        "junk-suffix slug, not fetched)", hit.url)
            outcome.excluded_variant.append(hit.url)
            continue
        if same_article_variant(hit.url, exclude_url):
            # The same article id under another slug or section. That may be the removed
            # article's own corpse — or its live RELOCATION: some publishers keep the id
            # across sections (/gundem/ -> /haber/). Only a fetch can tell them apart, so fetch it and
            # let the outcome decide: live keeps full standing, dead is reclassified below.
            variant_keys.add(normalize_url(hit.url))
        candidates.append(hit)

    if outcome.excluded_self:
        logger.info(
            "web_fetch: excluded %d hit(s) pointing at the removed article's own URL (%s)",
            len(outcome.excluded_self), exclude_url,
        )
    if not candidates:
        return outcome

    started = time.perf_counter()
    limits = httpx.Limits(max_connections=MAX_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S, follow_redirects=True, limits=limits,
        headers={"User-Agent": _UA}, transport=_guarded_transport(limits),
    ) as client:
        results = await asyncio.gather(*(_fetch_one(client, h) for h in candidates))
    elapsed = time.perf_counter() - started

    for r in results:
        if isinstance(r, LivePage):
            # Did the fetch LAND on the removed article? Exact final-URL equality is always
            # self. A final URL that merely shares the article id counts only when a real
            # redirect moved us there — a same-id candidate that serves 200 at its OWN URL
            # is the relocation (the id kept across /gundem/ -> /haber/) and must keep
            # standing.
            was_redirected = not is_same_url(r.final_url, r.url)
            if is_same_url(r.final_url, exclude_url) or (
                was_redirected and same_article_variant(r.final_url, exclude_url)
            ):
                # The page we actually reached IS the removed article: the publisher
                # redirected this candidate to the removed URL itself (or a slug variant of
                # it). A classic false MOVED verdict is a self-citation through exactly
                # this redirect — never a match, never a relocation.
                logger.info("web_fetch: %s excluded (redirected to the removed article's "
                            "own URL %s — self, not evidence)", r.url, r.final_url)
                outcome.excluded_redirect_self.append(r.url)
                continue
            # One line per KEPT live page, so downstream triage tooling can see what was
            # read even when the page never becomes a match (a human lead).
            redirected = f" — final URL differs: {r.final_url}" if was_redirected else ""
            logger.info("web_fetch: %s LIVE (%d chars of prose%s)", r.url, len(r.text), redirected)
            outcome.live.append(r)
        elif isinstance(r, DeadCandidate):
            if normalize_url(r.url) in variant_keys:
                # A dead variant of the removed article's own URL IS the removed article —
                # its 404 is the removal itself, never evidence a separate copy is gone.
                logger.info("web_fetch: %s excluded (variant of the removed article's own "
                            "URL answered HTTP %s — the removal itself, not a lost copy)",
                            r.url, r.status)
                outcome.excluded_variant.append(r.url)
            else:
                outcome.dead.append(r)
        else:
            outcome.unreadable.append(r)

    logger.info(
        "web_fetch: %d live, %d dead, %d unreadable (of %d candidates) in %.1fs",
        len(outcome.live), len(outcome.dead), len(outcome.unreadable), len(candidates), elapsed,
    )
    return outcome


async def probe_removed_url(url: str, title: str = "") -> Optional[LivePage]:
    """Fetch the removed article's OWN URL once, with the same client settings as
    `fetch_candidates`. Returns the LivePage when the URL answers 2xx with readable prose,
    else None. Never raises.

    Stage 3 runs this before searching, as an audit-premise check: "removed" URLs regularly
    answer 200 with the article text intact, and the pipeline had no way to say so. The
    caller must still MEASURE the text
    against the archived article — a 200 with prose can be a soft-404 or a parked page.
    """
    limits = httpx.Limits(max_connections=MAX_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S, follow_redirects=True, limits=limits,
        headers={"User-Agent": _UA}, transport=_guarded_transport(limits),
    ) as client:
        result = await _fetch_one(client, SearchResult(url=url, title=title, snippet=""))
    return result if isinstance(result, LivePage) else None
