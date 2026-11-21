"""Environment is read at application construction, never as import-time class defaults."""
from dataclasses import dataclass, field
from pathlib import Path
import ipaddress
import os
import re

from contextlib import closing
import sqlite3

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


RUN_MODES = ("test", "live")
TEST_DATABASE_SUFFIX = "-test.sqlite3"
DEFAULT_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "testserver"})
DEFAULT_ALLOWED_ORIGINS = frozenset({
    "http://localhost:2200", "http://127.0.0.1:2200",
    "http://localhost:2201", "http://127.0.0.1:2201",
})


def _host(value: str) -> str:
    value = value.lower()
    if "%" in value:
        raise ValueError("Invalid ALLOWED_HOSTS entry")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        pass
    # Browsers parse a host whose last label is numeric as IPv4 (192.0.2.001, 0xc0.0.2.1,
    # 192.1) and send it canonicalized, so a non-canonical spelling here could never match.
    if re.fullmatch(r"\d+|0x[0-9a-f]*", value.rstrip(".").rsplit(".", 1)[-1]):
        raise ValueError("Invalid ALLOWED_HOSTS entry")
    if (len(value) > 253 or not value.isascii() or
            any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in value.split("."))):
        raise ValueError("Invalid ALLOWED_HOSTS entry")
    return value


def _url_ipv6(host: str) -> str:
    """Serialize IPv6 as browsers do in Origin: hex pieces only, the first longest run of two or
    more zero pieces compressed (no dotted IPv4 tail, unlike ipaddress for mapped addresses)."""
    packed = ipaddress.IPv6Address(host).packed
    pieces = [int.from_bytes(packed[i:i + 2], "big") for i in range(0, 16, 2)]
    start, length = -1, 1
    i = 0
    while i < 8:
        j = i
        while j < 8 and pieces[j] == 0:
            j += 1
        if j - i > length:
            start, length = i, j - i
        i = max(j, i + 1)
    text = [format(piece, "x") for piece in pieces]
    if start < 0:
        return ":".join(text)
    return ":".join(text[:start]) + "::" + ":".join(text[start + length:])


def _origin(value: str) -> str:
    # Match the entire input, including delimiters that urlsplit would discard.
    match = re.fullmatch(r"(https?)://(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::([0-9]+))?",
                         value, re.IGNORECASE)
    if not match:
        raise ValueError("Invalid ALLOWED_ORIGINS entry")
    scheme, authority, port = match.groups()
    try:
        host = _host(authority.strip("[]"))
        if authority.startswith("[") and ipaddress.ip_address(host).version != 6:
            raise ValueError
        if port is not None and (len(port) > 5 or int(port) > 65535):
            raise ValueError
    except ValueError:
        raise ValueError("Invalid ALLOWED_ORIGINS entry") from None
    scheme = scheme.lower()
    authority = f"[{_url_ipv6(host)}]" if ":" in host else host
    # Browsers serialize Origin with a numeric port and omit the scheme default.
    number = int(port) if port is not None else None
    if number == {"http": 80, "https": 443}[scheme]:
        number = None
    return f"{scheme}://{authority}" + (f":{number}" if number is not None else "")


def canonical_request_host(hostname: str) -> str:
    """Normalize a request hostname the same way configured hosts are normalized."""
    try:
        return ipaddress.ip_address(hostname).compressed
    except ValueError:
        return hostname


def _loopback(host: str) -> bool:
    if host in DEFAULT_ALLOWED_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_test_database_path(path: Path) -> bool:
    """The one rule for a test (demo) database, shared with the demo seeder: a file named
    `*-test.sqlite3` or anything under a `data/demo/` directory. Case-insensitive, since macOS
    and Windows filesystems alias `AUDIT-TEST.SQLITE3` or `Data/Demo` to the same file."""
    parts = [part.lower() for part in path.parts]
    return parts[-1].endswith(TEST_DATABASE_SUFFIX) or any(
        parts[i] == "data" and parts[i + 1] == "demo" for i in range(len(parts) - 2))


def has_demo_marker(path: Path, *, include_wal: bool = False) -> bool:
    """True when the database holds a demo workflow (`wf_demo_*`), even after a rename or copy.

    Default: `immutable=1`, which never writes or creates sidecar files, so it reads only the
    main file (the seeder's quiescent-file check). `include_wal=True` reads what a normal
    connection will see, including an uncheckpointed WAL; it may create the `-shm` index file
    and raises ValueError when the file cannot be inspected. A file that is not SQLite is no demo."""
    query = "?mode=ro" if include_wal else "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(path.as_uri() + query, uri=True)) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # Literal, case-sensitive prefix: LIKE would treat `_` as a wildcard.
            return "workflows" in tables and connection.execute(
                "SELECT 1 FROM workflows WHERE substr(id, 1, 8) = 'wf_demo_' LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        if include_wal:
            raise ValueError("DATABASE_URL could not be inspected for demo data") from None
        return False
    except sqlite3.DatabaseError:
        return False


def canonical_database(database_url: str) -> tuple[str, bool, bool]:
    """Return the URL to open (the resolved path that was checked), whether that path is a
    test database path (`is_test_database_path`), and whether the file already holds demo data.

    Judged on the resolved path, not the URL text, so query strings, symlinks or `..` after a
    symlink cannot make the checked file differ from the opened one. URI filenames and paths
    that do not reparse to themselves are refused.
    """
    try:
        url = make_url(database_url)
    except ArgumentError:
        raise ValueError("Invalid DATABASE_URL") from None
    if url.get_backend_name() != "sqlite":
        raise ValueError("Only SQLite storage is supported")
    if "uri" in url.query or (url.database or "").startswith("file:"):
        raise ValueError("SQLite URI filenames are not supported in DATABASE_URL")
    if not url.database or url.database == ":memory:":
        return database_url, False, False
    path = Path(url.database).resolve()
    canonical = f"sqlite:///{path}"
    reparsed = make_url(canonical)
    if reparsed.database != str(path) or reparsed.query:
        raise ValueError("DATABASE_URL does not name one unambiguous file")
    # Live startup opens this file read-write next, so reading it as a normal connection
    # would (main file plus WAL) is safe and closes the uncheckpointed-WAL gap.
    return canonical, is_test_database_path(path), path.is_file() and has_demo_marker(path, include_wal=True)


def _entries(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def _port(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError(f"Invalid {name}")
    return int(value)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


MODEL_STEPS = ("classification", "relevance", "related_removals", "source", "web_search")


@dataclass(frozen=True)
class Settings:
    # test: offline demo engine, fake integrations, its own SQLite file seeded with demo data.
    # live: the configured model, search and read-only archive integrations.
    mode: str = "live"
    data_dir: Path = field(default_factory=lambda: Path("data"))
    database_url: str = ""
    model_enabled: bool = False
    model_base_url: str = "http://127.0.0.1:1234/v1"
    model_name: str = "auto"
    model_api_key: str = "lm-studio"
    model_timeout: float = 90
    max_tokens: int = 2048
    temperature: float = 1.0
    model_provider: str = "lmstudio"
    thinking_control: str | None = None
    response_format: str | None = None
    model_name_classification: str = ""
    model_name_relevance: str = ""
    model_name_related_removals: str = ""
    model_name_source: str = ""
    model_name_web_search: str = ""
    search_provider: str = "disabled"
    searxng_url: str = "http://127.0.0.1:2202"
    archive_base_url: str = ""
    archive_wallet_path: str = ""
    archive_wallet_json: str = ""
    api_auth_token: str = ""
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    allowed_origins: frozenset[str] = DEFAULT_ALLOWED_ORIGINS
    live_ingestion_enabled: bool = False
    max_concurrent_runs: int = 1
    # The host address Docker publishes the API and UI on (KARAKUTU_BIND_ADDRESS); empty when
    # the service is not started by Compose. Only checked, never bound by the app itself.
    bind_address: str = ""
    # The host port Docker publishes the UI on (KARAKUTU_WEB_PORT). A non-default port adds its
    # two loopback browser origins, so the UI on that port can still submit runs.
    web_port: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        if self.mode not in RUN_MODES:
            raise ValueError("RUN_MODE must be test or live")
        test = self.mode == "test"
        if not self.database_url:
            # The demo seeder accepts only *-test.sqlite3 files or paths under a demo/ directory.
            path = self.data_dir / "demo" / f"audit{TEST_DATABASE_SUFFIX}" if test else self.data_dir / "audit.sqlite3"
            object.__setattr__(self, "database_url", f"sqlite:///{path}")
        # Fail closed: test mode never opens a live database or reaches a real integration,
        # and live mode never opens the demo database.
        database_url, test_path, demo_data = canonical_database(self.database_url)
        object.__setattr__(self, "database_url", database_url)
        # Test mode needs a test database path; the demo marker alone is not enough (startup
        # writes before seeding, and the seeder only trusts a quiescent marker). Live mode
        # refuses both, so a renamed or copied demo database never receives real runs.
        if (test and not test_path) or (not test and (test_path or demo_data)):
            raise ValueError("Test mode requires its own test database (*-test.sqlite3 or under data/demo/); "
                             "live mode must not use a test database or one holding demo data")
        if test and (self.model_enabled or self.search_provider != "disabled" or self.archive_base_url
                     or self.archive_wallet_path or self.archive_wallet_json or self.live_ingestion_enabled):
            raise ValueError("Test mode runs without model, search, archive or live ingestion")
        object.__setattr__(self, "allowed_hosts", DEFAULT_ALLOWED_HOSTS | frozenset(
            _host(host) for host in self.allowed_hosts))
        object.__setattr__(self, "allowed_origins", DEFAULT_ALLOWED_ORIGINS | frozenset(
            _origin(origin) for origin in self.allowed_origins))
        if any(not _loopback(host) for host in self.allowed_hosts) and not self.api_auth_token:
            raise ValueError("Non-loopback ALLOWED_HOSTS require API_AUTH_TOKEN")
        if self.web_port is not None:
            if not 1 <= self.web_port <= 65535:
                raise ValueError("Invalid KARAKUTU_WEB_PORT")
            # Normalized like configured origins: a browser omits the default port 80.
            object.__setattr__(self, "allowed_origins", self.allowed_origins | {
                _origin(f"http://localhost:{self.web_port}"), _origin(f"http://127.0.0.1:{self.web_port}")})
        if self.bind_address:
            try:
                # Compose accepts IPv6 with or without brackets ([::1] or ::1).
                address = self.bind_address
                if address.startswith("[") and address.endswith("]"):
                    address = address[1:-1]
                published = ipaddress.ip_address(address)
            except ValueError:
                raise ValueError("Invalid KARAKUTU_BIND_ADDRESS") from None
            # 0.0.0.0, :: and LAN addresses expose the service beyond this machine.
            if not published.is_loopback and not self.api_auth_token:
                raise ValueError("Non-loopback KARAKUTU_BIND_ADDRESS requires API_AUTH_TOKEN")
        if self.api_auth_token and len(self.api_auth_token) < 32:
            raise ValueError("API_AUTH_TOKEN must contain at least 32 characters")
        if self.live_ingestion_enabled and not self.api_auth_token:
            raise ValueError("Live ingestion requires API_AUTH_TOKEN")
        if self.search_provider not in {"disabled", "searxng"}:
            raise ValueError("SEARCH_PROVIDER must be disabled or searxng")
        if self.model_provider not in {"lmstudio", "openai_compatible"}:
            raise ValueError("Invalid MODEL_PROVIDER")
        if self.thinking_control is None:
            object.__setattr__(self, "thinking_control", "lmstudio" if self.model_provider == "lmstudio" else "none")
        if self.response_format is None:
            object.__setattr__(self, "response_format", "json_schema" if self.model_provider == "lmstudio" else "auto")
        if self.response_format not in {"json_schema", "json_object", "auto"}:
            raise ValueError("Invalid MODEL_RESPONSE_FORMAT")
        if self.model_provider == "openai_compatible" and self.thinking_control == "lmstudio":
            raise ValueError("MODEL_THINKING_CONTROL lmstudio requires MODEL_PROVIDER lmstudio")
        if self.thinking_control not in {"lmstudio", "reasoning_effort", "none"}:
            raise ValueError("Invalid MODEL_THINKING_CONTROL")
        if not 1 <= self.max_concurrent_runs <= 8:
            raise ValueError("MAX_CONCURRENT_RUNS must be between 1 and 8")
        if not 1 <= self.model_timeout <= 600 or not 128 <= self.max_tokens <= 16384:
            raise ValueError("Invalid model request limits")

    def model_overrides(self) -> dict[str, str | None]:
        return {step: getattr(self, "model_name_" + step) or None for step in MODEL_STEPS}

    def engine_config(self) -> dict:
        return {name: getattr(self, name) for name in (
            "model_enabled", "model_base_url", "model_name", "model_api_key", "model_timeout",
            "max_tokens", "temperature", "thinking_control", "model_provider", "response_format",
            *("model_name_" + step for step in MODEL_STEPS), "search_provider", "searxng_url",
            "archive_base_url", "archive_wallet_path", "archive_wallet_json",
        )}

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(override=False)
        mode = os.getenv("RUN_MODE", "live").strip().lower() or "live"
        if mode == "test":
            # Only where to store data and who may connect are read; every integration
            # setting is ignored so a live .env cannot leak into a demo.
            return cls(
                mode="test",
                data_dir=Path(os.getenv("DATA_DIR", "data")),
                api_auth_token=os.getenv("API_AUTH_TOKEN", ""),
                bind_address=os.getenv("KARAKUTU_BIND_ADDRESS", "").strip(),
                web_port=_port("KARAKUTU_WEB_PORT"),
                allowed_hosts=frozenset(_entries("ALLOWED_HOSTS")),
                allowed_origins=frozenset(_entries("ALLOWED_ORIGINS")),
                max_concurrent_runs=int(os.getenv("MAX_CONCURRENT_RUNS", "1")),
            )
        return cls(
            mode=mode,
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            database_url=os.getenv("DATABASE_URL", ""),
            model_enabled=_bool("MODEL_ENABLED"),
            model_base_url=os.getenv("MODEL_BASE_URL", "http://127.0.0.1:1234/v1"),
            model_name=os.getenv("MODEL_NAME", "auto"),
            model_api_key=os.getenv("MODEL_API_KEY", "lm-studio"),
            model_timeout=float(os.getenv("MODEL_TIMEOUT", "90")),
            max_tokens=int(os.getenv("MODEL_MAX_TOKENS", "2048")),
            temperature=float(os.getenv("MODEL_TEMPERATURE", "1.0")),
            model_provider=os.getenv("MODEL_PROVIDER", "lmstudio"),
            thinking_control=os.getenv("MODEL_THINKING_CONTROL"),
            response_format=os.getenv("MODEL_RESPONSE_FORMAT"),
            **{"model_name_" + step: os.getenv("MODEL_NAME_" + step.upper(), "") for step in MODEL_STEPS},
            search_provider=os.getenv("SEARCH_PROVIDER", "disabled"),
            searxng_url=os.getenv("SEARXNG_URL", "http://127.0.0.1:2202"),
            archive_base_url=os.getenv("ARCHIVE_BASE_URL", ""),
            archive_wallet_path=os.getenv("ARCHIVE_WALLET_PATH", ""),
            archive_wallet_json=os.getenv("ARCHIVE_WALLET_JSON", ""),
            api_auth_token=os.getenv("API_AUTH_TOKEN", ""),
            bind_address=os.getenv("KARAKUTU_BIND_ADDRESS", "").strip(),
            web_port=_port("KARAKUTU_WEB_PORT"),
            allowed_hosts=frozenset(_entries("ALLOWED_HOSTS")),
            allowed_origins=frozenset(_entries("ALLOWED_ORIGINS")),
            live_ingestion_enabled=_bool("LIVE_INGESTION_ENABLED"),
            max_concurrent_runs=int(os.getenv("MAX_CONCURRENT_RUNS", "1")),
        )
