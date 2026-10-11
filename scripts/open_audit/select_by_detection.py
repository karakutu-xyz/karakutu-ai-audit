"""Select audits by the DETECTION date of the removal — READ-ONLY.

Lists audits in the given statuses newest-first (GET /audits?view=public&status=S), stops at
--created-since (an audit is created at or shortly after the archive detects the dead link, so the
creation date is only the coarse pre-filter), then pulls each article's check history
(GET /articles/{id}?expand=checks) and computes the detection window the auditors use:
last-known-alive (HTTP 200) → first-known-dead (404/410/451). Keeps the audits whose first-dead
check falls inside [--detected-from, --detected-to]. Writes a list.json in the shape the batch
tooling consumes (probe_audits.py → fetch_listed_cases.py → run_open_cases.py):
rows[{audit_id, publisher, list_state, title, published, detection_window, url, audit_url,
      detection: {last_alive, first_dead, dead_status, checks_seen, has_more}}]

Usage:
  .venv311/bin/python scripts/open_audit/select_by_detection.py --status 0 2 3 \
      --created-since 2026-08-20 --detected-from 2026-09-01 --detected-to 2026-09-30 \
      --exclude-lists runs/<batch>/list.json --exclude-results runs/<earlier-batch>/results.json \
      --out runs/sept-2026/list.json
"""
import argparse, asyncio, json, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)

import httpx  # noqa: E402

from src.clients import get_client  # noqa: E402

STATUS_NAMES = {0: "screening", 1: "claimed", 2: "under investigation", 3: "ready to publish", 4: "published"}
DEAD = {404, 410, 451}

ap = argparse.ArgumentParser()
ap.add_argument("--status", nargs="+", type=int, required=True)
ap.add_argument("--created-since", required=True, help="YYYY-MM-DD (UTC); coarse pre-filter on audit creation")
ap.add_argument("--detected-from", required=True); ap.add_argument("--detected-to", required=True)
ap.add_argument("--exclude-lists", nargs="*", default=[], help="list.json files whose audit ids are excluded")
ap.add_argument("--exclude-results", nargs="*", default=[], help="results.json files whose audit ids are excluded")
ap.add_argument("--out", required=True)
ap.add_argument("--concurrency", type=int, default=3)
args = ap.parse_args()


def day(s, end=False):
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp() + (86399 if end else 0)


CREATED_SINCE, DET_FROM, DET_TO = day(args.created_since), day(args.detected_from), day(args.detected_to, end=True)


def fmt(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "?"


async def get(hc, c, path, params=None):
    for attempt in range(1, 5):
        tok = await c.auth.get_token()
        try:
            r = await hc.get(f"{c.base_url}{path}", params=params, timeout=90, headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            err = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        await asyncio.sleep(3 * attempt)
    print(f"  giving up on {path}: {err}", flush=True)
    return None


def detection(checks):
    """(last_alive_ts, first_dead_ts, dead_status) from entries sorted newest-first."""
    entries = sorted((e for e in (checks or {}).get("entries") or [] if e.get("check_date")), key=lambda e: e["check_date"])
    last_alive = first_dead = None; dead_status = None
    for e in entries:                      # oldest → newest
        st = e.get("status")
        if st == 200:
            last_alive, first_dead, dead_status = e["check_date"], None, None
        elif st in DEAD and first_dead is None:
            first_dead, dead_status = e["check_date"], st
    return last_alive, first_dead, dead_status, len(entries)


async def main():
    excl = set()
    for p in args.exclude_lists:
        excl |= {r["audit_id"] for r in json.loads(Path(p).read_text())["rows"]}
    for p in args.exclude_results:
        excl |= {r["audit_id"] for r in json.loads(Path(p).read_text()).get("results", [])}
    c = get_client()
    if not c.enabled or not await c.auth.get_token():
        raise SystemExit("archive client unavailable (wallet / token)")
    cands = {}
    async with httpx.AsyncClient() as hc:
        for st in args.status:
            for page in range(1, 500):
                body = await get(hc, c, "/audits", {"view": "public", "status": st, "page": page, "page_size": 100,
                                                    "sort_by": "created_at", "sort_order": "desc"})
                items = (body or {}).get("items") or []
                stop = False
                for it in items:
                    if (it.get("created_at") or 0) < CREATED_SINCE:
                        stop = True; break
                    a = it.get("article") or {}
                    cands[it["id"]] = {"audit_id": it["id"], "status": st, "created_at": it["created_at"], "decision": it.get("decision"),
                                       "article_id": a.get("id"), "url": a.get("link"), "title": a.get("title") or "",
                                       "newspaper": a.get("newspaper") or "", "article_status": a.get("status")}
                print(f"  status {st} page {page}: {len(items)} items, candidates {len(cands)}", flush=True)
                if stop or len(items) < 100:
                    break
                await asyncio.sleep(0.3)
        print(f"{len(cands)} candidates created since {args.created_since}; {len(cands.keys() & excl)} excluded as already listed/run", flush=True)
        sem = asyncio.Semaphore(args.concurrency); t0 = time.time(); done = 0

        async def enrich(cd):
            nonlocal done
            async with sem:
                art = await get(hc, c, f"/articles/{cd['article_id']}", {"expand": ["checks"]}) if cd.get("article_id") else None
                ch = (art or {}).get("checks") or {}
                la, fd, ds, n = detection(ch)
                cd["detection"] = {"last_alive": la, "first_dead": fd, "dead_status": ds, "checks_seen": n, "has_more": bool(ch.get("has_more"))}
                done += 1
                if done % 50 == 0:
                    print(f"  checks {done}/{len(cands)} ({time.time()-t0:.0f}s)", flush=True)

        await asyncio.gather(*(enrich(cd) for cd in cands.values()))

    rows, reasons = [], Counter()
    for cd in sorted(cands.values(), key=lambda x: (x["status"], -x["created_at"])):
        d = cd["detection"]
        if cd["audit_id"] in excl:
            reasons["already_listed_or_run"] += 1; continue
        if not cd.get("url"):
            reasons["no_link"] += 1; continue
        if d["first_dead"] is None:
            reasons["never_seen_dead_in_history" if d["checks_seen"] else "no_check_history"] += 1; continue
        if not (DET_FROM <= d["first_dead"] <= DET_TO):
            reasons[f"detected_{fmt(d['first_dead'])[:7]}"] += 1; continue
        rows.append({"audit_id": cd["audit_id"], "publisher": cd["newspaper"], "list_state": STATUS_NAMES.get(cd["status"], str(cd["status"])),
                     "title": cd["title"], "published": None, "detection_window": f"{fmt(d['last_alive'])} → {fmt(d['first_dead'])} (HTTP {d['dead_status']})",
                     "url": cd["url"], "audit_url": f"https://karakutu.xyz/#/audit/{cd['audit_id']}", "detection": d,
                     "created_at": cd["created_at"], "decision": cd["decision"], "article_status": cd["article_status"]})
    out = Path(args.out).resolve(); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"source": f"archive selection by detection date {args.detected_from}..{args.detected_to}, statuses {args.status}, audits created since {args.created_since}",
                               "generated_at": datetime.now(timezone.utc).isoformat(), "candidates": len(cands), "skip_reasons": dict(reasons),
                               "count": len(rows), "rows": rows}, ensure_ascii=False, indent=1))
    print(f"wrote {out}: {len(rows)} rows; skipped {dict(reasons)}", flush=True)
    print("by status:", Counter(r["list_state"] for r in rows), "· by publisher:", Counter(r["publisher"] for r in rows).most_common(10), flush=True)
    print("has_more on first page:", sum(1 for r in rows if r["detection"]["has_more"]), "· dead status:", Counter(r["detection"]["dead_status"] for r in rows), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
