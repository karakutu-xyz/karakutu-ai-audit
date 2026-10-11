"""Fetch the N most recently OPENED audits (status=0) with article bodies into a fixture.

READ-ONLY. Newest first; skips: no article link, ids/urls already processed (from the result
files given with --exclude-results), same-article URL variants of an already-kept case, and
outlets over --per-outlet-cap (so ten cases are not ten karar.com items). Over-collects, fetches
bodies (longest archived snapshot), and keeps the first N with at least --min-chars of text.

Usage:
  .venv311/bin/python scripts/open_audit/fetch_fresh_cases.py --limit 10 --per-outlet-cap 3 \
      --exclude-results runs/<earlier-batch>/results.json [runs/<other-batch>/results.json ...] \
      --out runs/<batch>/cases.json
"""
import argparse, asyncio, json, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)

import httpx  # noqa: E402

from src.clients import get_client  # noqa: E402
from src.clients.models import longest_body  # noqa: E402
from src.search.urls import host_of, normalize_url, same_article_variant  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=10)
ap.add_argument("--per-outlet-cap", type=int, default=3)
ap.add_argument("--min-chars", type=int, default=400)
ap.add_argument("--exclude-results", nargs="*", default=[])
ap.add_argument("--out", required=True)
args = ap.parse_args()
OUT = (REPO / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
PAGE_SIZE, MAX_PAGES, LIST_TIMEOUT = 100, 30, 120


def prior_sets(paths):
    ids, urls = set(), set()
    for p in paths:
        d = json.loads(Path(p).read_text())
        for r in d.get("results", []):
            ids.add(r["audit_id"]); urls.add(normalize_url(r["url"]))
        for x in d.get("deduped_duplicates", []):
            ids.add(x["audit_id"]); urls.add(normalize_url(x["url"]))
    return ids, urls


async def list_page(hc, c, page):
    params = {"view": "public", "status": 0, "sort_by": "created_at", "sort_order": "desc",
              "page": page, "page_size": PAGE_SIZE}
    for attempt in range(1, 4):
        token = await c.auth.get_token()
        try:
            r = await hc.get(f"{c.base_url}/audits", params=params, timeout=LIST_TIMEOUT,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        print(f"  page {page} attempt {attempt} failed ({err})", flush=True)
        await asyncio.sleep(5 * attempt)
    raise SystemExit(f"listing page {page} failed")


async def main():
    prior_ids, prior_urls = prior_sets(args.exclude_results)
    print(f"excluding {len(prior_ids)} prior audit ids / {len(prior_urls)} urls", flush=True)
    c = get_client()
    if not c.enabled or not await c.auth.get_token():
        raise SystemExit("archive client unavailable (wallet / token)")
    want = args.limit * 3   # over-collect: some bodies come back empty
    kept, per_outlet, skips, total_open = [], Counter(), Counter(), None
    async with httpx.AsyncClient() as hc:
        for page in range(1, MAX_PAGES + 1):
            payload = await list_page(hc, c, page)
            total_open = total_open or payload.get("total")
            items = payload.get("items") or []
            for it in items:
                art = it.get("article") or {}
                link = art.get("link")
                if not link:
                    skips["no_link"] += 1; continue
                if it.get("id") in prior_ids or normalize_url(link) in prior_urls:
                    skips["already_processed"] += 1; continue
                if any(normalize_url(k["url"]) == normalize_url(link) or same_article_variant(k["url"], link) for k in kept):
                    skips["variant_of_kept"] += 1; continue
                outlet = (art.get("newspaper") or host_of(link) or "?").lower()
                if per_outlet[outlet] >= args.per_outlet_cap:
                    skips["outlet_cap"] += 1; continue
                per_outlet[outlet] += 1
                kept.append({"audit_id": it["id"], "article_id": art.get("id"), "url": link,
                             "title": art.get("title") or "", "newspaper": art.get("newspaper") or "",
                             "newspaper_country": art.get("newspaper_country"),
                             "publish_date": art.get("publish_date"), "http_status": art.get("status"),
                             "created_at": it.get("created_at")})
                if len(kept) >= want:
                    break
            print(f"  page {page}: {len(items)} items, kept so far {len(kept)}", flush=True)
            if len(kept) >= want or len(items) < PAGE_SIZE:
                break
            await asyncio.sleep(0.3)

    final = []
    for case in kept:
        rec = await c.get_article_with_body(case["url"])
        if rec is None:
            await asyncio.sleep(2); rec = await c.get_article_with_body(case["url"])
        body = longest_body(rec) if rec else ""
        if rec and rec.newspaper and rec.newspaper.slug:
            case["newspaper"] = rec.newspaper.slug
        if rec and rec.publish_date:
            case["publish_date"] = rec.publish_date
        case["content"], case["content_chars"] = body, len(body)
        if len(body) < args.min_chars:
            skips["thin_body"] += 1; continue
        final.append(case)
        print(f"  body {case['audit_id']} {case['newspaper']} {len(body)} chars", flush=True)
        if len(final) >= args.limit:
            break
        await asyncio.sleep(0.2)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{c.base_url}/audits view=public status=0 sort=created_at desc",
        "selection": {"limit": args.limit, "per_outlet_cap": args.per_outlet_cap, "min_chars": args.min_chars,
                      "excluded_results": args.exclude_results},
        "total_open": total_open, "skip_counts": dict(skips), "count": len(final), "cases": final,
    }, ensure_ascii=False, indent=1))
    print(f"wrote {OUT} ({len(final)} cases); skips={dict(skips)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
