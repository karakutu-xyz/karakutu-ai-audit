"""Seed a separate local SQLite database with fictional test-mode review records."""
import argparse
from pathlib import Path
import sys

# Keep direct Python invocation working as well as the project's editable uv install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from audit.demo import DemoSeedRefused, seed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--db", type=Path, help="Separate demo SQLite filesystem path")
    target.add_argument("--database-url", help="Separate SQLite SQLAlchemy URL")
    args = parser.parse_args()
    database_url = f"sqlite:///{args.db.expanduser().resolve()}" if args.db else args.database_url
    try:
        counts = seed(database_url)
    except DemoSeedRefused as exc:
        print(f"Demo seed refused: {exc}", file=sys.stderr)
        return 1
    for group, values in counts.items():
        print(f"{group}: " + ", ".join(f"{key}={count}" for key, count in values.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
