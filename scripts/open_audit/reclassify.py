"""Re-run STEP 1 ONLY (classification) on cases from a fixture with the current prompt/rules and the
live model, next to the tags recorded in an earlier results.json — to measure a classifier change
without re-running the other five steps. READ-ONLY toward the archive (catalog from cache/API).

Usage:
  .venv311/bin/python scripts/open_audit/reclassify.py --cases runs/<b>/cases.json --results runs/<b>/results.json \
      --out runs/<b>/reclassify-<date>.json [--ids 1 2 3] [--sample 25 --seed 1]
"""
import argparse, asyncio, json, os, random, sys, time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO)); os.chdir(REPO)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)
from src.api.models import Article  # noqa: E402
from src.clients import get_client  # noqa: E402
from src.clients.concepts import load_catalog  # noqa: E402
from src.config.settings import get_settings  # noqa: E402
from src.providers import get_provider  # noqa: E402
from src.workflow import classification  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--cases", required=True); ap.add_argument("--results", required=True); ap.add_argument("--out", required=True)
ap.add_argument("--ids", nargs="*", type=int, default=[]); ap.add_argument("--sample", type=int, default=0); ap.add_argument("--seed", type=int, default=1)
args = ap.parse_args()


async def main():
    settings = get_settings()
    cases = {c["audit_id"]: c for c in json.loads(Path(args.cases).read_text())["cases"]}
    old = {r["audit_id"]: r for r in json.loads(Path(args.results).read_text())["results"]}
    ids = list(args.ids)
    if args.sample:
        random.seed(args.seed)
        pool = [a for a in cases if a not in ids]
        ids += random.sample(pool, min(args.sample, len(pool)))
    provider = get_provider(settings.PROVIDER_TYPE)
    agents = classification.build_agents(provider.get_model())
    catalog = await load_catalog(get_client(), Path(settings.KARAKUTU_CONCEPTS_CACHE))
    out = Path(args.out).resolve(); rows = []
    for i, aid in enumerate(ids, 1):
        c = cases[aid]; t0 = time.time()
        art = Article(url=c["url"], title=c["title"], content=(c.get("content") or c["title"])[:120_000])
        res = await classification.run(art, agents, catalog)
        new = res.classification.model_dump(mode="json") if res.classification else None
        rows.append({"audit_id": aid, "title": c["title"], "old": (old.get(aid) or {}).get("classification"), "new": new,
                     "details": res.details, "explanation": res.explanation, "elapsed_s": round(time.time() - t0, 1)})
        print(f"[{i}/{len(ids)}] {aid} {c['title'][:50]} → old {len(((old.get(aid) or {}).get('classification') or {}).get('subject_tags') or [])} tags, "
              f"new {len((new or {}).get('subject_tags') or [])} tags ({rows[-1]['elapsed_s']}s)", flush=True)
        out.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "model": settings.MODEL_NAME,
                                   "count": len(rows), "rows": rows}, ensure_ascii=False, indent=1))
    print("done:", out)


asyncio.run(main())
