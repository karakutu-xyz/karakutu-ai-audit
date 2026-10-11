"""Run the six-step audit for one case and produce the run RECORD the reports and the applier read.

Moved from `scripts/open_audit/run_open_cases.py` (2026-10-05). `run_case` returns exactly the
record dict that script wrote (same keys, same values, same order), so `build_stepwise_report.py`,
`build_batch_artifact.py`, `batch_summary.py` and `applier.apply_record` keep consuming it.

What changed structurally (never a field of the record): the log capture and the per-stage timing
used to be one process-global handler and one shared dict, cleared before each case — correct only
while cases run strictly one after another. They are now keyed by a `ContextVar`, so each
`run_case` collects its own logs and timings even when two run concurrently.
"""
import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from src.api.models import Article, AuditRequest
from src.providers import served
from src.search.urls import normalize_url, same_article_variant
from src.workflow.pipeline import run_audit

logger = logging.getLogger(__name__)

CONTENT_LIMIT = 120_000  # was 2500 — that cap corrupted stage-3 containment measurement
# (QA 2026-08-30: a sibling measured 0.48 on truncated text vs 0.17 on full text). The model
# prompt is truncated separately in the repo (_build_prompt MAX_REMOVED_CHARS / relevance cap),
# so passing full text here is safe; only the measurement sees all of it.
CASE_TIMEOUT_S = 600          # hard per-case cap (run_open_cases.py --case-timeout default)
LOG_TAIL = 40                 # the record keeps the last 40 captured log lines
RETRY_VERDICTS = ("AUDIT_FAILED", "ERROR", None)   # run_open_cases.py --retry-failed predicate
STAGE_KEYS = ("classification", "similarity", "relevance", "related_removals", "source", "web_search")
CAPTURED_LOGGERS = ("src.workflow.pipeline", "src.search.fetch", "src.workflow.web_search",
                    "src.workflow.similarity", "src.workflow.source", "src.workflow.classification",
                    "src.workflow.related_removals")


# --- instrumentation (logs + stage timings per case) -----------------------------------

@dataclass
class CaseCapture:
    records: List[str] = field(default_factory=list)
    stage_times: dict = field(default_factory=dict)


_CAPTURE: ContextVar[Optional[CaseCapture]] = ContextVar("archive_flow_case_capture", default=None)


class LogCapture(logging.Handler):
    """Appends `<logger leaf>: <message>` to the CURRENT case's capture (no case → dropped)."""

    def __init__(self):
        super().__init__(level=logging.INFO)

    def emit(self, r):
        cap = _CAPTURE.get()
        if cap is not None:
            cap.records.append(f"{r.name.split('.')[-1]}: {r.getMessage()}")


_installed = {"handler": None, "wrapped": False}


def _timed(key, real):
    async def w(*a, **k):
        t0 = time.perf_counter()
        try:
            return await real(*a, **k)
        finally:
            cap = _CAPTURE.get()
            if cap is not None:
                cap.stage_times[key] = round(time.perf_counter() - t0, 2)
    w.__wrapped_stage__ = real
    return w


def install_instrumentation(*, set_level: bool = True) -> None:
    """Install the log handler and the stage-timing wrappers once per process (idempotent).

    `set_level=True` lowers the captured loggers to INFO, as the batch script always did (with no
    logging configured they would otherwise sit at WARNING and the record's `logs` would be empty).
    The timing wrapper only measures; it calls the real stage `run` unchanged.
    """
    if _installed["handler"] is None:
        cap = LogCapture()
        for name in CAPTURED_LOGGERS:
            logging.getLogger(name).addHandler(cap)
            if set_level:
                logging.getLogger(name).setLevel(logging.INFO)
        _installed["handler"] = cap
    if not _installed["wrapped"]:
        import src.workflow.pipeline as pm
        for key in STAGE_KEYS:
            stage_mod = getattr(pm, f"{key}_stage")
            if not hasattr(stage_mod.run, "__wrapped_stage__"):
                stage_mod.run = _timed(key, stage_mod.run)
        _installed["wrapped"] = True


# --- batch helpers -----------------------------------------------------------------------

def dedupe_cases(cases: list) -> Tuple[list, list]:
    """Drop audits that are the SAME article under URL variants (batch runs only).

    Slug rewrites, junk suffixes, duplicate captures: the archive opens one audit per captured
    URL, so one story can hold several open audits — and in the v1 run each variant pair cited
    the OTHER variant as its own "dead copy". Keeps the first (the fixture is newest-first).
    Returns (kept, dropped[{audit_id, duplicate_of, url}]).
    """
    kept, dropped = [], []
    for case in cases:  # fixture is already newest-first
        dup_of = next((k for k in kept if normalize_url(k["url"]) == normalize_url(case["url"])
                       or (k.get("article_id") and k.get("article_id") == case.get("article_id"))
                       or same_article_variant(k["url"], case["url"])), None)
        if dup_of is None:
            kept.append(case)
        else:
            dropped.append({"audit_id": case["audit_id"], "duplicate_of": dup_of["audit_id"],
                            "url": case["url"]})
    return kept, dropped


async def wait_for_model(provider, every=60, max_wait=4 * 3600,
                         say: Optional[Callable[[str], None]] = None) -> bool:
    """Block until the model server answers its health check again (or give up after max_wait)."""
    say = say or logger.info
    t0 = time.time()
    n = 0
    while True:
        try:
            ok = await provider.check_health()
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            say(f"   model server healthy again after {time.time() - t0:.0f}s")
            return True
        if time.time() - t0 > max_wait:
            say(f"   model server still down after {max_wait}s — stopping this pass; resume with --retry-failed")
            return False
        if n % 10 == 0:
            say(f"   model server down for {time.time() - t0:.0f}s — waiting ({every}s polls)")
        n += 1
        await asyncio.sleep(every)


# --- one case -> one record ----------------------------------------------------------------

def article_for(case: dict) -> Article:
    return Article(url=case["url"], title=case["title"],
                   content=(case.get("content") or case["title"])[:CONTENT_LIMIT])


async def run_case(case: dict, rt, *, timeout: int = CASE_TIMEOUT_S, karakutu_client=None,  # noqa: ASYNC109
                   instrument: bool = True) -> dict:
    """Audit one case and return its record (the shape of run_open_cases.py's results rows).

    `rt` carries `agents`, `search_provider` and `catalog` (a `src.runtime.Runtime`). The batch
    script never passed an archive client (each stage then built its own); `karakutu_client`
    stays None unless the caller decides otherwise. Never raises for an audit failure: a timeout
    or an exception becomes `verdict="ERROR"` with `error` set.
    """
    if instrument:
        install_instrumentation()
    art = article_for(case)
    cap = CaseCapture()
    token = _CAPTURE.set(cap)
    served_ids, served_token = served.start()
    try:
        t0 = time.perf_counter()
        response, err = None, None
        try:
            response = await asyncio.wait_for(
                run_audit(AuditRequest(removed_article=art), agents=rt.agents,
                          search_provider=rt.search_provider, catalog=rt.catalog,
                          **({"karakutu_client": karakutu_client} if karakutu_client is not None else {})),
                timeout=timeout)
        except asyncio.TimeoutError:  # hard per-case cap (builtin TimeoutError on 3.11) — must precede Exception
            err = f"case timeout: exceeded {timeout}s"
        except Exception as exc:  # noqa: BLE001 — record, never abort the batch
            err = f"{type(exc).__name__}: {exc}"
        elapsed = round(time.perf_counter() - t0, 2)
    finally:
        served.stop(served_token)
        _CAPTURE.reset(token)
    v = response.verdict.value if response else "ERROR"
    return {
        "audit_id": case["audit_id"], "article_id": case.get("article_id"),
        "url": case["url"], "title": case["title"],
        "newspaper": case.get("newspaper"), "publish_date": case.get("publish_date"),
        "http_status": case.get("http_status"), "created_at": case.get("created_at"),
        "content_chars": len(case.get("content") or ""),
        "list": case.get("list"), "probe": case.get("probe"),   # fixture provenance (fetch_listed_cases.py)
        "elapsed_s": elapsed, "stage_times_s": dict(cap.stage_times),
        "error": err, "logs": list(cap.records)[-LOG_TAIL:],
        "verdict": v,
        "flags": [f.value for f in (response.flags if response else [])],
        "classification": response.classification.model_dump(mode="json") if (response and response.classification) else None,
        "response": response.model_dump(mode="json") if response else None,
        "model": model_info(rt, served_ids),
    }


def model_info(rt, served_ids) -> dict:
    """The requested model, its call settings and the ids that actually answered (additive;
    no report or comment reads it). TestModel has no `describe`, so it is reported as synthetic."""
    model = getattr(rt, "model", None)
    describe = getattr(model, "describe", None)
    info = describe() if callable(describe) else {"requested": "synthetic"}
    return {**info, "served": sorted(served_ids)}
