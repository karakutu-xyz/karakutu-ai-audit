"""Create local settings once, preserving existing operator values and secrets."""
import argparse
import os
from pathlib import Path
import re
import secrets

from env_requirements import read_settings, report

ROOT = Path(__file__).resolve().parents[1]


def setup(target: Path, *, api_token: bool = False, mode: str | None = None) -> None:
    target.mkdir(parents=True, exist_ok=True)
    path = target / ".env"
    text = (ROOT / ".env.example").read_text()
    text = re.sub(r"^SEARXNG_SECRET=.*$", "SEARXNG_SECRET=" + secrets.token_hex(32),
                  text, flags=re.M)
    if api_token:
        text = re.sub(r"^API_AUTH_TOKEN=.*$", "API_AUTH_TOKEN=" + secrets.token_hex(32),
                      text, flags=re.M)
    try:
        # O_EXCL also protects against symlinks and races. Never touch an existing file.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(".env exists, left unchanged")
    else:
        with os.fdopen(descriptor, "w") as output:
            output.write(text)
        path.chmod(0o600)
        print("Created .env with mode 600")
    (target / "data").mkdir(mode=0o700, exist_ok=True)
    try:
        values = read_settings(path)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    chosen = mode or values.get("RUN_MODE", "test") or "test"
    print("Chosen mode:", chosen if chosen in {"test", "live"} else "invalid RUN_MODE")
    for run_mode in dict.fromkeys((chosen, "test", "live")):
        report(values, run_mode)
    print("Live with LM Studio: load a chat model; verify MODEL_BASE_URL and MODEL_NAME.")
    print("Docker live: verify CONTAINER_MODEL_BASE_URL is reachable from containers.")
    print("Optional archive: set ARCHIVE_BASE_URL and your read wallet if required.")
    print("Optional live ingestion: set LIVE_INGESTION_ENABLED and API_AUTH_TOKEN.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", "--path", type=Path, default=ROOT, dest="target")
    parser.add_argument("--api-token", action="store_true")
    parser.add_argument("--mode", choices=("test", "live"))
    args = parser.parse_args()
    setup(args.target, api_token=args.api_token, mode=args.mode)


if __name__ == "__main__":
    main()
