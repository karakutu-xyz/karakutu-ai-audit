"""`/api/audit` is unauthenticated and fetches URLs the CALLER supplies.

Step 4 probes `removed_article.url` directly and fetches links parsed out of the caller's
`content`, and the per-URL outcome (live / dead / unreadable, plus a containment percentage)
comes back in the response. Unguarded, that is an internal-network probe oracle: point it at
`http://169.254.169.254/latest/meta-data/` and read the answer off the audit.

Two properties are pinned here:
  1. a non-public target is refused, and refused WITHOUT a network call;
  2. a refusal is `unreadable`, never `dead` — a blocked URL is evidence of nothing and must
     never be able to reach the censorship flag.
"""
import httpx
import pytest

from src.search import fetch as fetch_module
from src.search.base import SearchResult
from src.search.fetch import DeadCandidate, LivePage, UnreadableCandidate, _fetch_one
from src.search.urls import is_public_web_url

BLOCKED = [
    pytest.param("http://169.254.169.254/latest/meta-data/", id="aws-imds"),
    pytest.param("http://metadata.google.internal/computeMetadata/v1/", id="gcp-metadata"),
    pytest.param("http://127.0.0.1:8080/admin", id="loopback-v4"),
    pytest.param("http://localhost:2200/health", id="localhost"),
    pytest.param("http://[::1]:8080/", id="loopback-v6"),
    pytest.param("http://10.0.0.5/internal", id="rfc1918-10"),
    pytest.param("http://192.168.1.1/router", id="rfc1918-192"),
    pytest.param("http://172.16.4.2/", id="rfc1918-172"),
    pytest.param("http://0.0.0.0/", id="unspecified"),
    pytest.param("http://[::ffff:127.0.0.1]/", id="ipv4-mapped-loopback"),
    pytest.param("http://searxng:8080/search", id="single-label-service-name"),
    pytest.param("http://redis/", id="single-label-container"),
    pytest.param("file:///etc/passwd", id="file-scheme"),
    pytest.param("gopher://127.0.0.1:6379/_SET%20x%20y", id="gopher-scheme"),
    pytest.param("ftp://internal.example.com/x", id="ftp-scheme"),
    pytest.param("", id="empty"),
    pytest.param("not a url", id="garbage"),
]

ALLOWED = [
    "https://www.evrensel.net/haber/98981/bir-haber",
    "http://t24.com.tr/haber/123",
    "https://jinnews.com.tr/haber/270809",
    "https://8.8.8.8/article",  # a public IP literal is fine
]


@pytest.mark.parametrize("url", BLOCKED)
def test_non_public_targets_are_rejected(url):
    assert is_public_web_url(url) is False


@pytest.mark.parametrize("url", ALLOWED)
def test_real_publisher_urls_still_pass(url):
    assert is_public_web_url(url) is True


async def test_a_blocked_url_is_never_fetched(monkeypatch):
    """The guard must run BEFORE the request — otherwise the connection itself is the leak
    (timing, and a port scan by response latency)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="<p>" + "x" * 400 + "</p>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _fetch_one(
            client, SearchResult(url="http://169.254.169.254/latest/meta-data/", title="", snippet=""))

    assert calls == [], f"the guard let a request through to {calls}"
    assert isinstance(result, UnreadableCandidate)
    assert "refused" in result.reason


async def test_a_blocked_url_is_unreadable_not_dead():
    """`dead` is the ONLY bucket that can raise POSSIBLE_CENSORSHIP. A URL we refused to
    fetch must never land there — that would manufacture a censorship flag out of our own
    security policy."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _fetch_one(
            client, SearchResult(url="http://127.0.0.1/gone", title="", snippet=""))

    assert not isinstance(result, DeadCandidate)
    assert isinstance(result, UnreadableCandidate)


async def test_a_redirect_onto_a_private_address_is_refused(monkeypatch):
    """A public URL may redirect anywhere. An open redirect on a real publisher would
    otherwise be a free pass to the metadata endpoint."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "news.example.com":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<p>" + "secret " * 200 + "</p>")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        result = await _fetch_one(
            client, SearchResult(url="https://news.example.com/a", title="", snippet=""))

    assert isinstance(result, UnreadableCandidate), f"got {type(result).__name__}"
    assert "redirect" in result.reason
    assert "secret" not in result.reason


async def test_an_ordinary_redirect_still_yields_a_live_page():
    """The guard must not break the normal relocation case — a publisher 301ing to its own
    new URL is exactly what step 5 exists to find."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://news.example.com/new"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<p>" + ("Bu bir haber metnidir. " * 40) + "</p>")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        result = await _fetch_one(
            client, SearchResult(url="https://news.example.com/old", title="t", snippet=""))

    assert isinstance(result, LivePage), f"got {type(result).__name__}"
    assert result.final_url == "https://news.example.com/new"


async def test_probe_removed_url_refuses_a_caller_supplied_private_target(monkeypatch):
    """The step-4 premise probe takes `removed_article.url` verbatim from the request body —
    the most directly caller-controlled fetch in the service."""
    calls = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="<p>" + "x" * 500 + "</p>")

    monkeypatch.setattr(
        fetch_module.httpx, "AsyncClient",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))

    result = await fetch_module.probe_removed_url("http://169.254.169.254/latest/meta-data/")

    assert result is None
    assert calls == []


# --- (2026-10-05): the observed bypasses + DNS resolution at connect time ---------

import socket  # noqa: E402

import httpcore  # noqa: E402

from src.search.fetch import GuardedNetworkBackend, _guarded_transport  # noqa: E402

BYPASSES = [
    # Non-canonical IPv4 spellings: `ipaddress` refuses them, a URL parser reads 127.0.0.1.
    pytest.param("http://127.1/", id="short-form"),
    pytest.param("http://0x7f.0.0.1/", id="hex-octet"),
    pytest.param("http://0x7f000001/", id="hex-int"),
    pytest.param("http://017700000001/", id="octal-int"),
    pytest.param("http://2130706433/", id="decimal-int"),
    pytest.param("http://0177.0.0.1/", id="octal-octet"),
    pytest.param("http://010.0.0.1/", id="zero-padded"),
    pytest.param("http://169.254.169.254./latest/", id="imds-trailing-dot"),
    pytest.param("http://1.1.1.1.1/", id="five-numeric-labels"),
    # Internal namespaces with a dot (the single-label rule cannot see these).
    pytest.param("http://kubernetes.default.svc/", id="k8s-svc"),
    pytest.param("http://redis.prod.svc.cluster.local:6379/", id="k8s-cluster-local"),
    pytest.param("http://printer.local/", id="mdns"),
    pytest.param("http://vault.internal/", id="internal-tld"),
    pytest.param("http://METADATA.GOOGLE.INTERNAL./", id="gcp-metadata-case-dot"),
    pytest.param("http://app.localhost/", id="localhost-subdomain"),
    pytest.param("http://LocalHost./", id="localhost-case-dot"),
    pytest.param("http://router.home.arpa/", id="home-arpa"),
    # IP literals in space the old check let through.
    pytest.param("http://100.64.0.1/", id="cgnat"),
    pytest.param("http://[fe80::1]/", id="link-local-v6"),
    pytest.param("http://[fc00::1]/", id="ula-v6"),
    pytest.param("http://224.0.0.1/", id="multicast"),
    pytest.param("http://[2002:7f00:1::]/", id="6to4-wrapping-loopback"),
]


@pytest.mark.parametrize("url", BYPASSES)
def test_observed_bypasses_are_rejected(url):
    assert is_public_web_url(url) is False


@pytest.mark.parametrize("url", [
    "https://www.haber7.com/gundem/haber/3412345-baslik",   # digits in a label, not the TLD
    "https://m.bianet.org/bianet/insan-haklari/298765",
    "https://www.sozcu.com.tr/2024/gundem/x-123456/",
    "https://bizim.media/cemiyyet/591993",
    "https://news.example.co.uk/a?b=1#c",
    "https://1.example.com/a",
    "https://[2606:4700:4700::1111]/",                      # a public IPv6 literal
])
def test_real_publisher_shapes_still_pass(url):
    assert is_public_web_url(url) is True


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """Stands in for the real socket layer: records what would be dialled, then fails."""

    def __init__(self):
        self.dialled = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109
        self.dialled.append((host, port))
        raise httpcore.ConnectError("test backend: no network")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover  # noqa: ASYNC109
        raise AssertionError("unix sockets must never be opened")

    async def sleep(self, seconds):  # pragma: no cover
        pass


def _fake_dns(monkeypatch, table):
    async def getaddrinfo(host, port, *a, **kw):
        if host not in table:
            raise socket.gaierror(8, "not known")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port)) for ip in table[host]]
    monkeypatch.setattr(fetch_module.anyio, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("answer", [
    ["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], ["100.64.1.2"], ["::1"],
    ["fe80::1%en0"], ["::ffff:127.0.0.1"], ["0.0.0.0"],  # noqa: S104 — a DNS answer, not a bind
], ids=lambda a: a[0])
async def test_a_public_name_resolving_to_a_private_address_is_never_dialled(monkeypatch, answer):
    _fake_dns(monkeypatch, {"innocent.example.com": answer})
    inner = _RecordingBackend()
    with pytest.raises(httpcore.ConnectError, match="ssrf-guard"):
        await GuardedNetworkBackend(inner).connect_tcp("innocent.example.com", 443, timeout=5)
    assert inner.dialled == []


async def test_only_the_public_addresses_of_a_mixed_answer_are_dialled(monkeypatch):
    _fake_dns(monkeypatch, {"mixed.example.com": ["10.0.0.5", "93.184.216.34", "127.0.0.1"]})
    inner = _RecordingBackend()
    with pytest.raises(httpcore.ConnectError, match="no network"):
        await GuardedNetworkBackend(inner).connect_tcp("mixed.example.com", 443, timeout=5)
    assert inner.dialled == [("93.184.216.34", 443)]


async def test_a_public_answer_is_dialled_by_ip_in_resolver_order(monkeypatch):
    _fake_dns(monkeypatch, {"news.example.com": ["2606:4700::1", "93.184.216.34"]})
    inner = _RecordingBackend()
    with pytest.raises(httpcore.ConnectError):
        await GuardedNetworkBackend(inner).connect_tcp("news.example.com", 80, timeout=5)
    assert inner.dialled == [("2606:4700::1", 80), ("93.184.216.34", 80)]


async def test_an_unresolvable_name_is_a_plain_connect_error_not_a_refusal(monkeypatch):
    _fake_dns(monkeypatch, {})
    with pytest.raises(httpcore.ConnectError) as info:
        await GuardedNetworkBackend(_RecordingBackend()).connect_tcp("gone.example.com", 80)
    assert "ssrf-guard" not in str(info.value)


async def test_dns_refusal_through_the_fetcher_is_unreadable_not_dead(monkeypatch):
    """End to end through httpx: the refusal surfaces as a 'refused' unreadable candidate,
    never `dead`, and no connection is attempted."""
    _fake_dns(monkeypatch, {"rebind.example.com": ["192.168.1.10"]})
    inner = _RecordingBackend()
    limits = httpx.Limits(max_connections=5)
    async with httpx.AsyncClient(transport=_guarded_transport(limits, inner=inner),
                                 follow_redirects=True) as client:
        result = await _fetch_one(
            client, SearchResult(url="http://rebind.example.com/haber/1", title="", snippet=""))
    assert isinstance(result, UnreadableCandidate)
    assert result.reason == "refused: resolves to a non-public address"
    assert inner.dialled == []


async def test_a_redirect_hop_is_resolved_and_refused_before_it_is_sent(monkeypatch):
    """The first hop is public and answers a 302 onto a name that resolves privately. The
    second connection is refused at connect time — the internal target receives nothing."""
    _fake_dns(monkeypatch, {"news.example.com": ["93.184.216.34"],
                            "open-redirect-target.example.net": ["10.1.2.3"]})
    dialled = []

    class OneHop(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109
            dialled.append(host)
            return await httpcore.AsyncMockBackend([
                b"HTTP/1.1 302 Found\r\n",
                b"Location: http://open-redirect-target.example.net/admin\r\n",
                b"Content-Length: 0\r\n\r\n",
            ]).connect_tcp(host, port)

        async def connect_unix_socket(self, *a, **kw):  # pragma: no cover
            raise AssertionError

        async def sleep(self, seconds):  # pragma: no cover
            pass

    limits = httpx.Limits(max_connections=5)
    async with httpx.AsyncClient(transport=_guarded_transport(limits, inner=OneHop()),
                                 follow_redirects=True) as client:
        result = await _fetch_one(
            client, SearchResult(url="http://news.example.com/a", title="", snippet=""))
    assert dialled == ["93.184.216.34"]
    assert isinstance(result, UnreadableCandidate)
    assert result.reason.startswith("refused")


async def test_fetch_candidates_and_the_probe_use_the_guarded_transport(monkeypatch):
    """Both production entry points must build their client with the guarded transport."""
    seen = []
    real = httpx.AsyncClient

    def spy(*a, **kw):
        seen.append(kw.get("transport"))
        return real(*a, **{**kw, "transport": httpx.MockTransport(lambda r: httpx.Response(404))})

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", spy)
    await fetch_module.probe_removed_url("https://news.example.com/a")
    await fetch_module.fetch_candidates(
        [SearchResult(url="https://news.example.com/b", title="", snippet="")],
        exclude_url="https://other.example.com/x")
    assert len(seen) == 2
    for transport in seen:
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        assert isinstance(transport._pool._network_backend, GuardedNetworkBackend)
