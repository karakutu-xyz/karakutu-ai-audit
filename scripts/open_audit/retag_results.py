"""Re-tag audits the bot tagged EARLIER with a corrected classification (the 2026-10-01 indexing
rules), and leave a threaded REPLY under the bot's original comment saying what changed.
DRY-RUN by default. Policy in apply_rules.retag_decision: the archive must still hold exactly the
list the bot wrote (any human edit → left alone), the status rule applies, and the new list must
differ. Discussion text cannot be edited (the API's PATCH takes `resolved` only), so the record of
the change is a reply under the original message, never a second top-level comment.

Inputs: the ORIGINAL apply log (what the bot wrote, with the discussion id to reply under) and a
file with the corrected classifications — either reclassify.py output (rows[].new) or a fresh
results.json (results[].classification).

Usage:
  .venv311/bin/python scripts/open_audit/retag_results.py --new runs/<b>/reclassify-<date>.json \
      --apply-log runs/<b>/results-apply_log.json [--ids …] [--apply]
"""
import argparse, asyncio, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)
from src.clients import get_client  # noqa: E402
from src.clients.concepts import ConceptCatalog  # noqa: E402
from apply_rules import retag_decision, status_label  # noqa: E402
from comment_templates import BOT  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--new", required=True); ap.add_argument("--apply-log", required=True)
ap.add_argument("--ids", nargs="*", type=int); ap.add_argument("--apply", action="store_true")
args = ap.parse_args()
LOG = Path(args.apply_log).resolve()
log = json.loads(LOG.read_text())
cat = ConceptCatalog.from_json(json.loads((REPO / ".cache" / "karakutu-concepts.json").read_text()))


def save():
    LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1))


def payload_of(c):
    c = c or {}
    return {"subject_tag_notations": [t["notation"] for t in (c.get("subject_tags") or [])],
            "content_type_notation": (c.get("genre") or {}).get("notation"), "geographic_scope": c.get("geographic_scope")}


def load_new(path):
    d = json.loads(Path(path).read_text())
    if "rows" in d:
        return {r["audit_id"]: r.get("new") for r in d["rows"]}
    return {r["audit_id"]: r.get("classification") for r in d.get("results", [])}


def reply_text(before, after):
    lab = lambda ns: ", ".join(cat.label(n) for n in ns) or "none"
    lines = [f"{BOT} · IPTC update · {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
             f"Topics on this audit were revised from [{lab(before['subject_tag_notations'])}] to [{lab(after['subject_tag_notations'])}].",
             "Why: the bot's indexing rules changed — a topic is no longer listed beside its own broader parent (the hierarchy implies it), "
             "near-duplicate topics are not stacked, and only topics the article clearly establishes are kept, so the list is no longer padded to five."]
    if before.get("content_type_notation") != after.get("content_type_notation") or before.get("geographic_scope") != after.get("geographic_scope"):
        lines.append(f"Genre/scope: {cat.label(before.get('content_type_notation') or '')} / {before.get('geographic_scope')} → "
                     f"{cat.label(after.get('content_type_notation') or '')} / {after.get('geographic_scope')}.")
    else:
        lines.append("Genre and scope are unchanged.")
    lines.append("Automated metadata change, not a finding. A human may still change the tags; the bot never overwrites a human's edit.")
    return "\n\n".join(lines)


async def main():
    new = load_new(args.new)
    c = get_client()
    if not c.enabled:
        raise SystemExit("archive client disabled")
    mode = "APPLY" if args.apply else "DRY-RUN"
    ids = [int(k) for k in log if not k.startswith("_") and (not args.ids or int(k) in set(args.ids)) and int(k) in new]
    print(f"{mode}: {len(ids)} audit(s) with a corrected classification and an apply-log entry", flush=True)
    n_tags = n_replies = 0
    for aid in ids:
        entry = log[str(aid)]
        prev = (entry.get("tags") or {}).get("payload")
        if not prev or not (entry.get("tags") or {}).get("ok"):
            print(f"  {aid}: SKIP — the bot never wrote tags here"); continue
        audit = await c.get_audit(aid)
        if audit is None:
            print(f"  {aid}: SKIP — audit not found"); continue
        after = payload_of(new.get(aid))
        # a re-run that produced no genre / scope must not erase the one already written
        after["content_type_notation"] = after["content_type_notation"] or prev.get("content_type_notation")
        after["geographic_scope"] = after["geographic_scope"] or prev.get("geographic_scope")
        d = retag_decision(audit, prev, after, entry)
        print(f"\n  {aid}: status={status_label(audit.get('status'))} · {d.action.upper()} — {d.reason}")
        if not d.write:
            continue
        print(f"    before: {[cat.label(n) for n in prev['subject_tag_notations']]}\n    after:  {[cat.label(n) for n in after['subject_tag_notations']]}")
        text = reply_text(prev, after)
        parent = (entry.get("comment") or {}).get("discussion_id")
        print(f"    reply under discussion {parent}: {len(text)} chars")
        if args.apply:
            ok = await c.update_audit_classification(aid, **after)
            entry["retag"] = {"ok": ok, "at": time.time(), "before": prev, "payload": after}
            n_tags += ok
            if ok and parent:
                out = await c.post_discussion(aid, text, parent_id=int(parent))
                entry["retag"]["reply_id"] = (out or {}).get("id")
                n_replies += out is not None
            save()
            print(f"    tags: {'written' if ok else 'FAILED'} · reply: {entry['retag'].get('reply_id') or 'none'}")
            await asyncio.sleep(0.5)
    log["_last_retag"] = {"at": datetime.now(timezone.utc).isoformat(), "mode": mode, "new": args.new}
    save()
    print(f"\n{mode} done: tags re-written {n_tags} · replies posted {n_replies} · log {LOG}")


asyncio.run(main())
