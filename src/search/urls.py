"""Pure URL helpers for stage 3: normalization, self-comparison, publisher/relocation checks.

Kept separate from the fetching logic in `fetch.py` because these are network-free string
functions that both the fetcher and the workflow reason about. Every name here is re-exported
from `src.search.fetch` for backwards compatibility.
"""
import ipaddress
import re
from typing import Optional, Set
from urllib.parse import urlsplit, urlunsplit

# A 404 on a PDF/image/archive is not a deleted news article. Under rate limiting the search
# backend throws up academic PDFs and datasheets; one of them 404'd and would otherwise have
# become "a same-story copy is gone". A censorship flag must not rest on a dead spreadsheet.
NON_ARTICLE_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".csv", ".jpg", ".jpeg", ".png", ".gif", ".mp3", ".mp4",
)


def looks_like_an_article_url(url: str) -> bool:
    """False for URLs that are plainly not a news page (a PDF, an image, an archive)."""
    path = (urlsplit(url).path or "").lower().rstrip("/")
    return not path.endswith(NON_ARTICLE_SUFFIXES)


# --- SSRF guard ---------------------------------------------------------------
#
# `/api/audit` is unauthenticated and fetches URLs the CALLER supplies: the removed
# article's own URL (the step-4 premise probe) and every link extracted from the caller's
# `content`. The per-URL outcome is then reported back — live / dead / unreadable plus a
# containment percentage — which turns the audit endpoint into a probe oracle for anything
# the server can reach but the caller cannot.
#
# Two layers:
#   1. `is_public_web_url` — a network-free check on the URL string: non-HTTP schemes,
#      IP literals outside public space (169.254.169.254, the cloud metadata endpoint,
#      included), NON-CANONICAL IPv4 spellings (`127.1`, `0x7f.0.0.1`, `017700000001`,
#      `2130706433` — browsers and some resolvers read these as 127.0.0.1, `ipaddress` does
#      not), single-label names (Docker/K8s service names like `redis`) and internal
#      namespaces (`*.svc`, `*.local`, `*.internal`, `*.localhost`, …).
#   2. `fetch.GuardedNetworkBackend` — resolves DNS at CONNECT time and only ever connects to
#      an address that `is_public_ip` accepts. It runs for every connection, so it also
#      covers every redirect hop and a public name pointing at a private address (or
#      rebinding to one between check and connect: the vetted IP is the one dialled).

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that never denote a public news site.
BLOCKED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "instance-data",
})

# Name suffixes that only exist inside a private network or a cluster. No news publisher
# lives under any of them (they are reserved, special-use or de-facto internal TLDs).
BLOCKED_HOST_SUFFIXES = (
    ".localhost", ".local", ".localdomain", ".internal", ".svc", ".cluster.local",
    ".home.arpa", ".arpa", ".lan", ".intranet", ".corp", ".home",
)

# A label a URL parser may read as an IPv4 component: decimal, octal (leading 0) or hex.
_NUMERIC_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")


def is_public_ip(ip) -> bool:
    """True when `ip` (an `ipaddress` object) is a globally routable unicast address.

    IPv4-mapped/compatible IPv6 is judged by its embedded IPv4 address. Rejects private,
    loopback, link-local (169.254/16, fe80::/10), CGNAT (100.64/10), unspecified,
    multicast and reserved space.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None or ip.teredo is not None:
            return False   # tunnelled addresses can smuggle an arbitrary IPv4 target
    return (ip.is_global and not ip.is_private and not ip.is_loopback and not ip.is_link_local
            and not ip.is_multicast and not ip.is_unspecified and not ip.is_reserved)


def is_public_web_url(url: str) -> bool:
    """True when `url` is an http(s) URL that does not target the local network by its
    spelling. DNS is checked separately, at connect time (see the note above).

    Conservative on purpose: anything unparseable is rejected. A false negative costs one
    candidate; a false positive hands out an internal-network probe.
    """
    try:
        parts = urlsplit((url or "").strip())
        hostname = parts.hostname
    except ValueError:
        return False

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return False

    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_HOST_SUFFIXES):
        return False

    # An IP literal can be checked exactly — but only the CANONICAL spelling parses here.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return is_public_ip(ip)

    # A host whose LAST label is numeric is an IPv4 address to a WHATWG URL parser
    # (`127.1`, `0x7f.0.0.1`, `2130706433`, a zero-padded `010.0.0.1`) even though
    # `ipaddress` refused it above. Real publishers never have a numeric TLD, so any such
    # non-canonical form is refused outright rather than re-interpreted.
    if _NUMERIC_LABEL_RE.match(host.rsplit(".", 1)[-1]):
        return False

    # A single-label name ("redis", "searxng", "metadata") is a container/service address,
    # never a news publisher. Real publishers always carry a dot.
    if "." not in host:
        return False

    return True


def normalize_url(url: str) -> str:
    """Canonical form for comparison: drop scheme, `www.`, query, fragment, trailing slash."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    return urlunsplit(("", host, path, "", "")).lstrip("/").lower()


def is_same_url(a: str, b: str) -> bool:
    """True when two URLs point at the same page once normalized."""
    return normalize_url(a) == normalize_url(b)


def host_of(url: str) -> str:
    """The bare host of a URL, lowercased and stripped of a leading `www.` (e.g. `news.example.org`).

    Empty string when there is no host. Used both for same-publisher checks and to build the
    domain-scoped `site:` query that looks for a relocation on the publisher's own domain.
    """
    h = (urlsplit(url.strip()).netloc or "").lower()
    return h[4:] if h.startswith("www.") else h


def same_publisher(a: str, b: str) -> bool:
    """True when two URLs share a host (ignoring `www.`) — i.e. the same publisher.

    Relocation detection rests on this. A publisher relocating its own article keeps the
    host and changes the path (a section move such as /gundem/→/haber/, a re-numbered row
    such as /yazi/12345→/yazi/12346, and so on).
    """
    ha, hb = host_of(a), host_of(b)
    return bool(ha) and ha == hb


# --- same-article variants ---------------------------------------------------
#
# A false POSSIBLE_CENSORSHIP flag typically cites a "dead same-story copy" that is really
# the removed article's OWN URL in a different spelling — junk glued onto the slug (`…-aciklamanbsp`, from an unescaped
# &nbsp;) or the same numeric article id under a new slug (`/12345/first-slug` vs
# `/12345/second-slug`). Normalized-URL equality cannot see those, so stage 3 needs a slightly
# wider notion of "self".

# Path segments that are pure digits usually name the article's database row
# (`/news/12345`, `/a/4242`, `/section/67890`); some publishers append the row id after
# a comma (`some-slug,1234567`). Four digits is the floor; a 4-digit 19xx/20xx token is treated
# as a date-path year, never as an id — otherwise every `/2022/05/…` article on a
# date-routed site would look like a variant of every other article that year.
_MIN_ID_DIGITS = 4
_COMMA_ID_RE = re.compile(r",(\d{4,})$")
# A junk suffix is a SHORT alnum run glued straight onto an already-long slug. The
# slug-length floor keeps `/tag/a` vs `/tag/ab` from matching.
_MAX_JUNK_SUFFIX_CHARS = 8
_MIN_SLUG_CHARS_FOR_SUFFIX = 10


def _looks_like_year(token: str) -> bool:
    return len(token) == 4 and 1900 <= int(token) <= 2099


def _clean_path(url: str) -> str:
    try:
        path = urlsplit(url.strip()).path or ""
    except ValueError:
        path = ""
    return path.rstrip("/").lower()


def _article_id_tokens(path: str) -> Set[str]:
    """Numeric tokens in a path that plausibly name an article row (see the note above)."""
    tokens: Set[str] = set()
    for seg in path.split("/"):
        if seg.isdigit() and len(seg) >= _MIN_ID_DIGITS:
            if not _looks_like_year(seg):
                tokens.add(seg)
            continue
        m = _COMMA_ID_RE.search(seg)
        if m and not _looks_like_year(m.group(1)):
            tokens.add(m.group(1))
    return tokens


def is_junk_suffix_variant(a: str, b: str) -> bool:
    """True when, on the same host, one path is the other plus a short alnum garbage run
    glued to the final slug (`…-aciklama` vs `…-aciklamanbsp`). Textually the same
    URL, so provably self — callers may act on it without a fetch."""
    if not same_publisher(a, b):
        return False
    pa, pb = _clean_path(a), _clean_path(b)
    if pa == pb:
        return False
    shorter, longer = sorted((pa, pb), key=len)
    if not longer.startswith(shorter):
        return False
    extra = longer[len(shorter):]
    if not (1 <= len(extra) <= _MAX_JUNK_SUFFIX_CHARS and extra.isalnum()):
        return False
    # The suffix must extend a real slug, not a stub of a path (isalnum() above already
    # rules out the suffix opening a new path segment).
    return len(shorter.rsplit("/", 1)[-1]) >= _MIN_SLUG_CHARS_FOR_SUFFIX


def same_article_variant(a: str, b: str) -> bool:
    """True when two URLs almost certainly name the SAME article row at the same publisher:
    the same normalized URL, the same numeric article id under a different slug or section,
    or one slug being the other plus a short junk suffix.

    Deliberately conservative — adjacent ids are genuinely different rows, so a
    relocation to the next row id (`/yazi/12345/…` -> `/yazi/12346/…`) stays False, as do same-host
    articles that merely share a date-path year.
    """
    if not same_publisher(a, b):
        return False
    if is_same_url(a, b):
        return True
    if _article_id_tokens(_clean_path(a)) & _article_id_tokens(_clean_path(b)):
        return True
    return is_junk_suffix_variant(a, b)


# --- language guessing --------------------------------------------------------

# Hosts whose articles are known to be Azerbaijani; searching them with language=tr
# finds little and ends the audit INCONCLUSIVE.
_AZ_HOSTS = frozenset({"bizim.media"})


def guess_language(url: str) -> Optional[str]:
    """Best-effort language of the article behind a URL, or None when unsure.

    Conservative by design: "az" only for a known-Azerbaijani host or an explicit `/az/`
    path segment. None means "no guess" — search providers then fall back to their
    configured default (SEARXNG_LANGUAGE for SearXNG).
    """
    if host_of(url) in _AZ_HOSTS:
        return "az"
    try:
        segments = [s for s in (urlsplit(url.strip()).path or "").lower().split("/") if s]
    except ValueError:
        return None
    if "az" in segments:
        return "az"
    return None
