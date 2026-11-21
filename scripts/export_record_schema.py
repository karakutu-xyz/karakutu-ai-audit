"""Generate/check the portable audit record contract, never stored article records."""
import argparse
import json
from pathlib import Path

from audit.record import record_json_schema

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
path = root / "docs" / "audit-record.v1.schema.json"
value = json.dumps(record_json_schema(), indent=2, sort_keys=True) + "\n"
if args.check:
    if not path.exists() or path.read_text(encoding="utf-8") != value:
        raise SystemExit("Audit record schema is stale; run scripts/export_record_schema.py")
    print("Audit record schema matches the models.")
else:
    path.write_text(value, encoding="utf-8")
    print("Wrote docs/audit-record.v1.schema.json.")
