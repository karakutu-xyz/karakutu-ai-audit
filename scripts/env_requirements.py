"""Shared, value-free preflight for setup and the local runner (stdlib only)."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import socket
import sys
from typing import Mapping
from urllib.parse import urlsplit


# Shell variables Bash treats specially; never accepted as setting names.
SHELL_RESERVED = re.compile(
    r"BASH.*|BASHOPTS|CDPATH|ENV|FUNCNAME|GROUPS|HISTFILE|HOME|HOSTNAME|IFS|LANG|LC_.*"
    r"|LINENO|OLDPWD|OPTARG|OPTERR|OPTIND|PATH|PIPESTATUS|PPID|PS[0-9]|PWD|RANDOM"
    r"|SECONDS|SHELL|SHELLOPTS|SHLVL|TMPDIR|UID|EUID|USER")


def read_settings(path: Path) -> dict[str, str]:
    """Read the documented KEY=value subset without executing operator input.

    The grammar is the intersection that Docker Compose, python-dotenv and this
    runner read identically: no `$` (interpolation) and no backslash (escapes) in
    any value, quoted or not, so switching loaders never changes a value.
    """
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        valid_key = (separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                     and not SHELL_RESERVED.fullmatch(key))
        quoted = (len(value) >= 2 and value[0] in "\"'"
                  and value[-1] == value[0] and value[0] not in value[1:-1])
        unquoted = not re.search(r"[\s\"'`;&|<>()]", value)
        if (not separator or not valid_key or "\0" in value
                or re.search(r"[$\\]", value) or not (quoted or unquoted)):
            label = f" ({key})" if valid_key else ""
            raise ValueError(f"invalid .env line {number}{label}")
        values[key] = value[1:-1] if quoted else value
    return values


def documented_keys(example: Path) -> set[str]:
    """Setting names the runner may import: exactly the keys `.env.example` documents."""
    return set(read_settings(example))


def missing(value: str) -> bool:
    return not value.strip() or "<" in value or ">" in value


def is_loopback(host: str) -> bool:
    """localhost names and every loopback address literal (127.0.0.0/8, ::1 in any form)."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        # Shorthand IPv4 spellings HTTP clients accept (127.1, 2130706433, 0x7f.1);
        # inet_aton parses literals only and never resolves a name.
        return ipaddress.IPv4Address(socket.inet_aton(host)).is_loopback
    except (OSError, ValueError):
        return False


def local_search_url(values: Mapping[str, str]) -> str:
    """Resolve the local runner's search endpoint before starting any process."""
    url = values.get("SEARXNG_URL", "")
    if values.get("RUN_MODE") != "live" or values.get("SEARCH_PROVIDER") != "searxng":
        return url
    port = values.get("KARAKUTU_SEARCH_PORT") or "2202"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("KARAKUTU_SEARCH_PORT: choose a port from 1 to 65535")
    if not url:
        url = f"http://127.0.0.1:{port}"
    try:
        parsed = urlsplit(url)
        url_port = parsed.port
        if url_port is None:
            url_port = 443 if parsed.scheme == "https" else 80
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        raise ValueError("SEARXNG_URL: use a valid search URL") from None
    if is_loopback(host) and url_port != int(port):
        raise ValueError(
            f"SEARXNG_URL loopback port {url_port} differs from "
            f"KARAKUTU_SEARCH_PORT={port}. Set SEARXNG_URL to "
            f"http://127.0.0.1:{port}, clear it to derive it, or change "
            "KARAKUTU_SEARCH_PORT to match."
        )
    return url


def issues(values: Mapping[str, str], mode: str) -> dict[str, str]:
    """Names and reasons only; this is an offline preflight, not a connectivity check."""
    result = {}
    if mode not in {"test", "live"}:
        return {"RUN_MODE": "choose test or live"}

    def require(name: str, reason: str, default: str = "") -> None:
        if missing(values.get(name, default)):
            result[name] = reason

    token = values.get("API_AUTH_TOKEN", "")
    if token and (missing(token) or len(token) < 32):
        result["API_AUTH_TOKEN"] = "use at least 32 characters"
    bind = values.get("KARAKUTU_BIND_ADDRESS", "127.0.0.1") or "127.0.0.1"
    hosts = values.get("ALLOWED_HOSTS", "").split(",") + [bind]
    for host in hosts:
        host = host.strip().strip("[]").lower()
        if host in {"", "localhost", "testserver"}:
            continue
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            require("API_AUTH_TOKEN", "required for non-loopback access")
    for name, default in (("KARAKUTU_API_PORT", "2200"),
                          ("KARAKUTU_WEB_PORT", "2201"),
                          ("KARAKUTU_SEARCH_PORT", "2202")):
        port = values.get(name) or default
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            result[name] = "choose a port from 1 to 65535"
    if mode == "test":
        return result
    for name, default, minimum, maximum in (
        ("POLL_INTERVAL_MINUTES", "5", 0, 1440),
        ("AUTO_AUDIT_MAX_PER_POLL", "5", 1, 50),
    ):
        try:
            value = int(values.get(name, default))
        except ValueError:
            value = minimum - 1
        if not minimum <= value <= maximum:
            result[name] = f"must be between {minimum} and {maximum}"
    if values.get("AUTO_AUDIT_ENABLED", "false").strip().lower() not in {
        "true", "false", "1", "0",
    }:
        result["AUTO_AUDIT_ENABLED"] = "must be true or false"
    if values.get("MODEL_ENABLED", "false").lower() not in {"true", "1"}:
        result["MODEL_ENABLED"] = "enable the model for useful live audits"
    require("MODEL_BASE_URL", "LM Studio server URL", "http://127.0.0.1:1234/v1")
    require("MODEL_NAME", "loaded chat model name from LM Studio, or auto", "auto")
    provider = values.get("MODEL_PROVIDER", "lmstudio")
    if provider not in {"lmstudio", "openai_compatible"}:
        result["MODEL_PROVIDER"] = "choose lmstudio or openai_compatible"
    search = values.get("SEARCH_PROVIDER", "disabled")
    if search not in {"disabled", "searxng"}:
        result["SEARCH_PROVIDER"] = "choose disabled or searxng"
    if search == "searxng":
        require("SEARXNG_SECRET", "set a search signing secret (openssl rand -hex 32)")
        require("SEARXNG_URL", "local search URL", "http://127.0.0.1:2202")
    return result


def report(values: Mapping[str, str], mode: str) -> bool:
    problems = issues(values, mode)
    label = mode if mode in {"test", "live"} else "invalid RUN_MODE"
    print(f"{label}: values still to set" if problems else f"{label}: no required values unset")
    for name, reason in problems.items():
        print(f"  {name}: {reason}")
    return not problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit literal NUL-separated assignments.")
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--local-search", action="store_true")
    parser.add_argument("--example", type=Path,
                        default=Path(__file__).resolve().parent.parent / ".env.example")
    args = parser.parse_args()
    if args.local_search:
        try:
            url = local_search_url(os.environ)
        except ValueError as error:
            raise SystemExit(str(error)) from None
        if not os.environ.get("SEARXNG_URL") and os.environ.get("KARAKUTU_SEARCH_PORT"):
            print(f"Derived SEARXNG_URL={url} from KARAKUTU_SEARCH_PORT.", file=sys.stderr)
        print(url)
        return
    if args.path is None:
        parser.error("path is required")
    try:
        values = read_settings(args.path)
        # Obsolete push-intake setting: accept old files without importing the key.
        values.pop("LIVE_INGESTION_ENABLED", None)
        allowed = documented_keys(args.example)
    except UnicodeError:
        raise SystemExit("invalid .env encoding") from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except OSError:
        raise SystemExit("Cannot read .env or .env.example") from None
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise SystemExit("unknown .env settings (not in .env.example): " + ", ".join(unknown))
    # Validate the whole file before emitting any settings; never emit partial input.
    # Like load_dotenv(override=False), a value the caller already set (even empty)
    # wins over the file, so an exported token or port is never replaced.
    for key, value in values.items():
        if key in os.environ:
            continue
        sys.stdout.buffer.write(f"{key}={value}\0".encode("utf-8"))


if __name__ == "__main__":
    main()
