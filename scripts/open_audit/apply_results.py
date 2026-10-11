"""Apply a step-wise run to the archive: write the IPTC classification onto each audit record and
post one discussion comment per audit. DRY-RUN BY DEFAULT — nothing is sent unless --apply is given.

The archive keeps the IPTC fields on the AUDIT (PATCH /audits/{id}: subject_tag_notations,
content_type_notation, geographic_scope); the article endpoints are read-only. Comments go to
POST /audits/{id}/discussions as a bare string (≤ 10,000 chars).

Thin CLI (2026-10-05): the per-record body lives in src/archive_flow/applier.py::apply_record (shared
with the article job worker); this file keeps the flags, the log file and the printed report.

Guards, re-checked live per audit — the policy itself lives in src/archive_flow/rules.py (pure, unit-tested;
the operator's STANDING rules of 2026-09-15, not run-specific):
  * the audit must still exist (backlog drift deletes/merges audits daily)
  * only audits in status 0 (screening), 2 (under investigation) or 3 (ready to publish) are
    touched; every other status (1 claimed, 4 published) is left alone — no tags, no comment
  * tags are written ONLY when none of the audit's three IPTC fields is set; one set field stops
    the write and there is no override. The bot's proposal then goes into the comment, worded
    as PROPOSED ONLY (the comment never claims codes were set when they were not)
  * a comment is posted only when the audit has ZERO existing discussions (unknown ≠ zero)
  * apply_log.json records every outcome; a re-run never repeats a successful write (idempotent)

Usage:
  .venv311/bin/python scripts/open_audit/apply_results.py runs/<batch>/results.json --template C --tags --comments
  .venv311/bin/python scripts/open_audit/apply_results.py <results.json> --template C --tags --comments --apply
  .venv311/bin/python scripts/open_audit/apply_results.py <results.json> --template C --apply --tags --comments --ids 231023 231018
"""
import argparse, asyncio, json, sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)

from src.archive_flow.applier import apply_record, classification_payload  # noqa: E402,F401 — re-exported
from src.archive_flow.comments import TEMPLATES  # noqa: E402
from src.archive_flow.rules import result_usable  # noqa: E402
from src.clients import get_client  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("results")
ap.add_argument("--template", choices=sorted(TEMPLATES), default="A")
ap.add_argument("--apply", action="store_true", help="actually send requests (default: dry-run)")
ap.add_argument("--tags", action="store_true", help="write the IPTC classification onto the audit records")
ap.add_argument("--comments", action="store_true", help="post the discussion comment")
ap.add_argument("--ids", nargs="*", type=int, help="restrict to these audit ids")
ap.add_argument("--log", default=None, help="apply log path (default: beside the results file)")
args = ap.parse_args()

RESULTS = Path(args.results).resolve()
LOG = Path(args.log).resolve() if args.log else RESULTS.with_name(RESULTS.stem + "-apply_log.json")
log = json.loads(LOG.read_text()) if LOG.exists() else {}


def save_log():
    LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1))


async def main():
    data = json.loads(RESULTS.read_text())
    rows = [r for r in data["results"] if not args.ids or r["audit_id"] in set(args.ids)]
    c = get_client()
    if not c.enabled:
        raise SystemExit("archive client disabled (no wallet keyfile)")
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: {len(rows)} audit(s) · template {args.template} · tags={args.tags} comments={args.comments}", flush=True)
    done_tags = done_comments = skipped = ineligible = proposed = unusable = 0
    for r in rows:
        # The ledger entry exists only for usable records (an unusable one never touches the log).
        entry = log.setdefault(str(r["audit_id"]), {}) if result_usable(r).write else {}
        out = await apply_record(c, r, ledger_entry=entry, persist=save_log, template=args.template,
                                 do_tags=args.tags, do_comments=args.comments, dry_run=not args.apply,
                                 say=print)
        unusable += out.stage == "unusable"
        skipped += out.stage == "missing"
        ineligible += out.stage == "ineligible"
        done_tags += out.tags.written
        done_comments += out.comment.posted
        proposed += out.proposed_wording
    log["_last_run"] = {"at": datetime.now(timezone.utc).isoformat(), "mode": mode, "template": args.template,
                        "tags": args.tags, "comments": args.comments, "ids": args.ids}
    save_log()
    print(f"\n{mode} done: tags written {done_tags} · comments posted {done_comments} · comments with PROPOSED-ONLY "
          f"IPTC wording {proposed} · ineligible status {ineligible} · missing {skipped} · "
          f"unusable results (re-run) {unusable} · log {LOG}")


if __name__ == "__main__":
    asyncio.run(main())
