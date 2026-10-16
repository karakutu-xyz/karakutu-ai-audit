"""Probe a list of audit ids against the archive — READ-ONLY.

For every id records the HTTP status of GET /audits/{id}, the audit's status/decision/tag fields,
the article link, and the discussion count (GET /audits/{id}/discussions?page_size=1 → total).
Used before a live apply to know which audits still exist, which are still open, which already
carry tags (never overwritten) and which already have a discussion.

Usage:
  .venv311/bin/python scripts/open_audit/probe_audits.py --list runs/<batch>/list.json \
      --out runs/<batch>/probe.json
"""
import argparse, asyncio, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)

import httpx  # noqa: E402

from src.clients import get_client  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--list", required=True, help="JSON with rows[].audit_id (or a bare list of ids)")
ap.add_argument("--out", required=True)
ap.add_argument("--concurrency", type=int, default=3)
args = ap.parse_args()
OUT = Path(args.out).resolve()
HEAVY = {"content", "contents", "body", "html", "snapshots", "checks"}


def load_ids(path):
    d = json.loads(Path(path).read_text())
    rows = d["rows"] if isinstance(d, dict) else d
    return [r["audit_id"] if isinstance(r, dict) else int(r) for r in rows]


async def get(hc, c, path, params=None):
    for attempt in range(1, 5):
        token = await c.auth.get_token()
        try:
            r = await hc.get(f"{c.base_url}{path}", params=params, timeout=60,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}"
        else:
            if r.status_code == 401 and attempt == 1:
                await c.auth.get_token(force=True); continue
            if r.status_code < 500:
                body = None
                try: body = r.json()
                except ValueError: pass
                return r.status_code, body
            err = f"HTTP {r.status_code}"
        await asyncio.sleep(3 * attempt)
    return None, {"error": err}


def slim(audit):
    if not isinstance(audit, dict):
        return audit
    a = {k: v for k, v in audit.items() if k not in HEAVY}
    art = a.get("article")
    if isinstance(art, dict):
        a["article"] = {k: v for k, v in art.items() if k not in HEAVY}
    return a


async def main():
    ids = load_ids(args.list)
    c = get_client()
    if not c.enabled or not await c.auth.get_token():
        raise SystemExit("archive client unavailable (wallet / token)")
    sem = asyncio.Semaphore(args.concurrency)
    results, t0 = {}, time.time()

    async def one(aid):
        async with sem:
            st, body = await get(hc, c, f"/audits/{aid}")
            rec = {"http": st, "audit": slim(body) if st == 200 else body}
            if st == 200:
                dst, dbody = await get(hc, c, f"/audits/{aid}/discussions", {"page_size": 1})
                rec["discussions_http"] = dst
                rec["discussions"] = dbody.get("total") if isinstance(dbody, dict) else None
            results[str(aid)] = rec
            n = len(results)
            if n % 25 == 0 or n == len(ids):
                print(f"  {n}/{len(ids)} probed ({time.time()-t0:.0f}s)", flush=True)
                flush()

    def flush():
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                                   "list": args.list, "count": len(results), "audits": results},
                                  ensure_ascii=False, indent=1))

    async with httpx.AsyncClient() as hc:
        await asyncio.gather(*(one(a) for a in ids))
    flush()
    print(f"wrote {OUT} ({len(results)} audits, {time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
