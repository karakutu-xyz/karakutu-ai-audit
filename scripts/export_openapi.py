"""Generate/check the public API schema, never sample requests or article records.

The contract is written as YAML for people to read: sorted keys, block style, no anchors or
aliases, and non-ASCII text kept as is, so the same application always gives the same bytes
and `--check` can compare them exactly. The running app still serves `/openapi.json`.
"""
import argparse
from pathlib import Path

import yaml

from audit.api import create_app
from audit.config import Settings


class _NoAliasDumper(yaml.SafeDumper):
    """Write a shared schema object out in full each time instead of as an anchor/alias."""

    def ignore_aliases(self, data):
        return True


def render(schema: dict) -> str:
    return yaml.dump(
        schema, Dumper=_NoAliasDumper, sort_keys=True, default_flow_style=False,
        allow_unicode=True, width=100,
    )


parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
path = root / "openapi.yaml"
value = render(create_app(Settings()).openapi())
if args.check:
    if not path.exists() or path.read_text(encoding="utf-8") != value:
        raise SystemExit("OpenAPI contract is stale; run scripts/export_openapi.py")
    print("OpenAPI contract matches the application.")
else:
    path.write_text(value, encoding="utf-8")
    print("Wrote openapi.yaml.")
