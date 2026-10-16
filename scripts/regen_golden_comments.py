#!/usr/bin/env python3
"""Regenerate tests/fixtures/golden_comments.json from tests/fixtures/sample_records.json.

The hashes pin the rendered discussion comment for every template × tag state over the
synthetic sample records (see CONTRIBUTING.md, "How the suite is built"). Run this ONLY as part
of a deliberately labelled behaviour change to the comment templates, and commit the result in
the same change. It renders exactly as tests/test_archive_flow.py does: `_stamp` frozen to the
fixture's `frozen_date`.

    .venv311/bin/python scripts/regen_golden_comments.py          # rewrite the fixture
    .venv311/bin/python scripts/regen_golden_comments.py --check  # exit 1 if it would change
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.archive_flow import comments, rules  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RECORDS = FIXTURES / "sample_records.json"
GOLDEN = FIXTURES / "golden_comments.json"
STATES = (rules.TAGS_SET, rules.TAGS_PROPOSED, rules.TAGS_PROPOSED_EXISTING)


def render_hashes(records, frozen_date: str) -> dict:
    original = comments._stamp
    comments._stamp = lambda: frozen_date
    try:
        out = {}
        for r in records:
            for k in sorted(comments.TEMPLATES):
                for s in STATES:
                    text = comments.render(k, r, tags_state=s)
                    out[f"{r['audit_id']}/{k}/{s}"] = hashlib.sha256(text.encode()).hexdigest()
        return out
    finally:
        comments._stamp = original


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 if the fixture is stale")
    args = ap.parse_args()

    records = json.loads(RECORDS.read_text())["records"]
    golden = json.loads(GOLDEN.read_text())
    hashes = render_hashes(records, golden["frozen_date"])
    changed = hashes != golden.get("hashes")
    if args.check:
        print("golden_comments.json is", "STALE" if changed else "up to date")
        return 1 if changed else 0
    golden["hashes"] = hashes
    GOLDEN.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {len(hashes)} hashes to {GOLDEN.relative_to(ROOT)}" + (" (changed)" if changed else " (unchanged)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
