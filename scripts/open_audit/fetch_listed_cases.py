"""Build a run fixture (cases.json) for an explicit LIST of audits — READ-ONLY.

Input: a list JSON (rows[].audit_id, url, plus any descriptive columns) and, optionally, the
probe JSON from probe_audits.py (so audits that no longer exist are skipped without a call and
the probe facts — status, decision, existing tags, discussion count — travel with each case).
For every kept audit the article body is recovered from the archive (longest snapshot) exactly
as fetch_fresh_cases.py does. Output shape is the one run_open_cases.py consumes.

Thin CLI (2026-10-05): the per-audit body lives in src/archive_flow/intake.py::build_case.

Usage:
  .venv311/bin/python scripts/open_audit/fetch_listed_cases.py --list runs/<batch>/list.json \
      --probe runs/<batch>/probe.json --out runs/<batch>/cases.json
"""
import argparse, asyncio, json, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)

from src.archive_flow.intake import MIN_BODY_CHARS, Skip, build_case  # noqa: E402
from src.clients import get_client  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--list", required=True)
ap.add_argument("--probe", default=None)
ap.add_argument("--out", required=True)
ap.add_argument("--min-chars", type=int, default=MIN_BODY_CHARS)
ap.add_argument("--concurrency", type=int, default=3)
args = ap.parse_args()
OUT = Path(args.out).resolve()


async def main():
    rows = json.loads(Path(args.list).read_text())["rows"]
    probe = json.loads(Path(args.probe).read_text())["audits"] if args.probe else {}
    c = get_client()
    if not c.enabled or not await c.auth.get_token():
        raise SystemExit("archive client unavailable (wallet / token)")
    sem = asyncio.Semaphore(args.concurrency)
    skips, cases, t0 = Counter(), {}, time.time()

    async def one(row):
        aid = row["audit_id"]
        pr = probe.get(str(aid))
        if pr is not None and pr.get("http") != 200:
            skips[f"probe_http_{pr.get('http')}"] += 1; return
        out = await build_case(c, audit=(pr or {}).get("audit") or {}, list_row=row,
                               discussions=(pr or {}).get("discussions"), min_chars=args.min_chars, limiter=sem)
        if isinstance(out, Skip):
            skips[out.reason] += 1
            print(f"  {aid}: body {out.body_chars} chars — skipped", flush=True); return
        cases[aid] = out
        n = len(cases) + sum(skips.values())
        if n % 25 == 0:
            print(f"  {n}/{len(rows)} ({len(cases)} kept, {time.time()-t0:.0f}s)", flush=True)

    await asyncio.gather(*(one(r) for r in rows))
    ordered = [cases[r["audit_id"]] for r in rows if r["audit_id"] in cases]   # keep the list's order
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{args.list} (+ probe {args.probe})",
        "selection": {"min_chars": args.min_chars}, "skip_counts": dict(skips),
        "count": len(ordered), "cases": ordered}, ensure_ascii=False, indent=1))
    print(f"wrote {OUT} ({len(ordered)} cases, {time.time()-t0:.0f}s); skips={dict(skips)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
