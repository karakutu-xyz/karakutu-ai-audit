"""Generate/check the public API schema, never sample requests or article records."""
import argparse
import json
from pathlib import Path
from audit.api import create_app
from audit.config import Settings

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
path = root / "openapi.json"
value = json.dumps(create_app(Settings()).openapi(), indent=2, sort_keys=True) + "\n"
if args.check:
    if not path.exists() or path.read_text() != value:
        raise SystemExit("OpenAPI contract is stale; run scripts/export_openapi.py")
    print("OpenAPI contract matches the application.")
else:
    path.write_text(value)
    print("Wrote openapi.json.")
