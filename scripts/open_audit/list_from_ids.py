"""Build a list.json (the shape probe_audits.py / fetch_listed_cases.py consume) from an explicit set
of audit ids — READ-ONLY. Each id is read from the archive (GET /audits/{id}) for its article link,
title, publisher and status; the detection window comes from the article's check history
(first dead check after the last 200) when available.

Usage:
  .venv311/bin/python scripts/open_audit/list_from_ids.py --ids 900001 900002 … --out runs/<batch>/list.json
  .venv311/bin/python scripts/open_audit/list_from_ids.py --ids-file ids.txt --out runs/<batch>/list.json
"""
import argparse, asyncio, json, re, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)
from src.clients import get_client  # noqa: E402

STATUS_NAMES = {0: "screening", 1: "claimed", 2: "under investigation", 3: "ready to publish", 4: "published"}
ap = argparse.ArgumentParser()
ap.add_argument("--ids", nargs="*", type=int, default=[])
ap.add_argument("--ids-file")
ap.add_argument("--out", required=True)
ap.add_argument("--source", default="operator-supplied audit ids")
args = ap.parse_args()
ids = list(args.ids)
if args.ids_file:
    ids += [int(x) for x in re.findall(r"\d{4,}", Path(args.ids_file).read_text())]
ids = list(dict.fromkeys(ids))
fmt = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "?"


async def main():
    c = get_client()
    if not c.enabled or not await c.auth.get_token():
        raise SystemExit("archive client unavailable (wallet / token)")
    sem = asyncio.Semaphore(3)
    rows, missing = {}, []

    async def one(aid):
        async with sem:
            a = await c.get_audit(aid)
            if a is None:
                missing.append(aid); return
            art = a.get("article") or {}
            first_dead = await c.first_dead_check(art["id"]) if art.get("id") else None
            rows[aid] = {"audit_id": aid, "publisher": art.get("newspaper") or "", "list_state": STATUS_NAMES.get(a.get("status"), str(a.get("status"))),
                         "title": art.get("title") or "", "published": (art.get("publish_date") or "")[:10] or None,
                         "detection_window": f"first dead check {fmt(first_dead)}" if first_dead else f"audit created {fmt(a.get('created_at'))}",
                         "url": art.get("link") or "", "audit_url": f"https://karakutu.xyz/#/audit/{aid}",
                         "detection": {"first_dead": first_dead}, "created_at": a.get("created_at"), "decision": a.get("decision"),
                         "status": a.get("status"), "article_status": art.get("status")}
    await asyncio.gather(*(one(a) for a in ids))
    out = Path(args.out).resolve(); out.parent.mkdir(parents=True, exist_ok=True)
    ordered = [rows[a] for a in ids if a in rows]
    out.write_text(json.dumps({"source": args.source, "generated_at": datetime.now(timezone.utc).isoformat(), "requested": len(ids),
                               "missing": missing, "count": len(ordered), "rows": ordered}, ensure_ascii=False, indent=1))
    print(f"wrote {out}: {len(ordered)} of {len(ids)} ids found; missing {missing}")
    print("publishers:", Counter(r["publisher"] for r in ordered).most_common(), "· status:", Counter(r["list_state"] for r in ordered))
    print("first dead checks:", Counter((fmt(r["detection"]["first_dead"]) if r["detection"]["first_dead"] else "unknown") for r in ordered).most_common(8))
    print("article http status:", Counter(r["article_status"] for r in ordered).most_common(5), "· decisions:", Counter(r["decision"] for r in ordered))


asyncio.run(main())
