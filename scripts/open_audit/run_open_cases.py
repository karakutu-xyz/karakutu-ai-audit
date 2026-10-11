"""Run the real audit pipeline over the open-cases fixture (open_cases.json), sequentially.

Mirrors what src/api/app.py::lifespan builds (one provider, the StageAgents bundle for the
six steps, the concept catalog, one search provider), but takes an arbitrary fixture of UNDECIDED audits and keeps the trace light.
Flushes atomically after every case; resumes by audit_id (skip cases already in the output).
Designed to be killed and re-run.

Usage: .venv311/bin/python scripts/open_audit/run_open_cases.py --input open_cases.json --output open_results.json [--limit N] [--fresh] [--case-timeout S]
"""
import argparse, asyncio, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

# Repo root, derived from this file (scripts/open_audit/<x>.py) so the script runs
# from any checkout. Override with KARAKUTU_AUDIT_REPO if it is ever moved.
REPO = Path(os.environ.get("KARAKUTU_AUDIT_REPO") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(REPO))
os.chdir(REPO)  # relative paths (KARAKUTU_CONCEPTS_CACHE, a relative KARAKUTU_WALLET_PATH) resolve from the repo

from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env.lmstudio", override=True)

from src.archive_flow.runner import (  # noqa: E402,F401 — CONTENT_LIMIT / LogCapture re-exported for old callers
    CASE_TIMEOUT_S, CONTENT_LIMIT, RETRY_VERDICTS, LogCapture, article_for, dedupe_cases, install_instrumentation,
    run_case, wait_for_model as _wait_for_model,
)
from src.config.settings import get_settings  # noqa: E402
from src.runtime import build_runtime  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True); ap.add_argument("--output", required=True)
ap.add_argument("--limit", type=int, default=None, help="run at most N unprocessed cases")
ap.add_argument("--case-timeout", type=int, default=CASE_TIMEOUT_S, help="hard per-case time cap in seconds")
ap.add_argument("--fresh", action="store_true")
ap.add_argument("--fail-streak", type=int, default=3,
                help="after this many consecutive failed audits, pause and poll the model server's health "
                     "(measured 2026-09-17: LM Studio went down mid-batch and 58 cases burned through as "
                     "AUDIT_FAILED in ~8 s each before anyone noticed)")
ap.add_argument("--health-wait-max", type=int, default=4 * 3600, help="max seconds to wait for the model server")
ap.add_argument("--retry-failed", action="store_true",
                help="on resume, treat AUDIT_FAILED / ERROR records as not done and run them again")
args = ap.parse_args()
INP, OUT = Path(args.input).resolve(), Path(args.output).resolve()


def flush(meta, results):
    payload = json.dumps({**meta, "updated_at": datetime.now(timezone.utc).isoformat(),
                          "cases_done": len(results), "results": results},
                         indent=1, ensure_ascii=False)
    tmp = OUT.with_suffix(".tmp"); tmp.write_text(payload); os.replace(tmp, OUT)


async def wait_for_model(provider, every=60, max_wait=4 * 3600):
    """Block until the model server answers its health check again (or give up after max_wait)."""
    return await _wait_for_model(provider, every=every, max_wait=max_wait, say=lambda m: print(m, flush=True))


async def main():
    settings = get_settings()
    print(f"model={settings.MODEL_NAME} search={settings.WEB_SEARCH_PROVIDER} "
          f"max_tokens={settings.MODEL_MAX_TOKENS} timeout={settings.MODEL_REQUEST_TIMEOUT}s "
          f"thinking_control={settings.MODEL_THINKING_CONTROL} "
          f"temperature={settings.MODEL_TEMPERATURE or 'server default'}", flush=True)
    if not settings.MODEL_ENABLED:
        raise SystemExit("MODEL_ENABLED is false")

    data = json.loads(INP.read_text())
    cases = data["cases"]
    # Dedupe audits that are the SAME article under URL variants (slug rewrites, junk
    # suffixes, duplicate captures): the archive opens one audit per captured URL, so one
    # story can hold several open audits — and in the v1 run each variant pair cited the
    # OTHER variant as its own "dead copy". Keep the newest audit per variant group.
    kept, dropped = dedupe_cases(cases)
    if dropped:
        print(f"deduped {len(dropped)} duplicate audit(s) of the same article:", flush=True)
        for d in dropped:
            print(f"   {d['audit_id']} -> duplicate of {d['duplicate_of']}", flush=True)
    cases = kept
    prior = {}
    if OUT.exists() and not args.fresh:
        try:
            prior = {r["audit_id"]: r for r in json.loads(OUT.read_text()).get("results", [])}
            if args.retry_failed:
                failed = [k for k, r in prior.items() if r.get("verdict") in RETRY_VERDICTS]
                for k in failed:
                    prior.pop(k)
                print(f"retry-failed: {len(failed)} failed/errored record(s) will be re-run", flush=True)
        except (ValueError, OSError):
            prior = {}
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(),
            "source_fixture": str(INP), "total_cases": len(cases),
            "deduped_duplicates": dropped,
            "config": {"model": settings.MODEL_NAME, "search": settings.WEB_SEARCH_PROVIDER,
                        "base_url": settings.MODEL_BASE_URL,
                        "max_tokens": settings.MODEL_MAX_TOKENS,
                        "request_timeout": settings.MODEL_REQUEST_TIMEOUT,
                        "thinking": settings.MODEL_THINKING_CONTROL == "none",
                        "thinking_control": settings.MODEL_THINKING_CONTROL,
                        "temperature": settings.model_temperature, "content_limit": CONTENT_LIMIT,
                        "case_timeout": args.case_timeout}}
    results = list(prior.values())
    if prior:
        print(f"resuming: {len(prior)} case(s) already done", flush=True)

    # One wiring for API, worker and CLI (src/runtime.py). The archive client it builds is used for
    # the concept catalog only: run_case does NOT pass it to run_audit (each stage builds its own,
    # as this script always did — see design §4.3 for why that matters to step 4's window cache).
    rt = await build_runtime(settings)
    provider = rt.provider
    print("lm studio health:", await provider.check_health(), flush=True)
    catalog = rt.catalog
    print("concept catalog:", f"{catalog.topic_count} topics / {catalog.genre_count} genres" if catalog else "NONE", flush=True)
    install_instrumentation()   # per-case log capture + stage timings (ContextVar-keyed)

    done_this_run = 0
    consecutive_failed = 0
    for n, case in enumerate(cases, 1):
        if case["audit_id"] in prior:
            continue
        if args.limit is not None and done_this_run >= args.limit:
            break
        art = article_for(case)
        print(f"[{n}/{len(cases)}] audit={case['audit_id']} {art.title[:60]}…", flush=True)
        rec = await run_case(case, rt, timeout=args.case_timeout)
        v = rec["verdict"]
        print(f"      → {v}  ({rec['elapsed_s']}s)", flush=True)
        results.append(rec)
        done_this_run += 1
        flush(meta, results)
        consecutive_failed = consecutive_failed + 1 if v in ("AUDIT_FAILED", "ERROR") else 0
        if consecutive_failed >= args.fail_streak:
            print(f"!! {consecutive_failed} consecutive failed audits — checking the model server before going on", flush=True)
            if not await wait_for_model(provider, max_wait=args.health_wait_max):
                break
            consecutive_failed = 0
    flush(meta, results)
    print(f"done: {len(results)}/{len(cases)} cases in {OUT.name}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
