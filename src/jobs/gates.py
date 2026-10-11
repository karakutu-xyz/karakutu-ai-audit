"""Fail-closed publish gates. Nothing here computes or changes a result — it only decides whether
a finished, usable result may be WRITTEN to the archive.

1. `write_blockers(settings, rt)` — configuration gates. Writes happen only when ALL hold:
   ARCHIVE_WRITES_ENABLED=true, the model is a real one (not pydantic-ai's TestModel), the search
   provider is not the canned `stub`, the IPTC concept catalog is loaded, and the archive client
   is enabled. Any blocker forces the job to a dry run and is recorded in the job.

2. `archive_degradation(record)`. In a recorded batch run (2026-10-01) six audits ran
   while `/login` was unreachable: step 2 reported "the archive returned no similar articles"
   (available=false) and step 4 "the archive's audit list could not be read"; both were posted
   permanently, and for one of them a working step 4 could have changed the verdict. The record
   is read, never altered; the signals are matched on the EXACT reason strings the stages emit:

   * step 4 (related_removals): `details.available is False` and `details.reason ==
     "the archive's audit list could not be read"` — emitted ONLY when every window listing failed
     (`related_removals.run`), so it is an unambiguous archive outage.
   * step 2 (similarity): `details.available is False` and the reason is "the archive returned no
     similar articles (…)". That string is also what a genuinely empty similarity answer produces,
     so on its own it is NOT proof of an outage (it appears ~1% of recorded runs with step 4
     reading the archive fine). It counts as degraded only when the same run has no proof the
     archive was reachable — i.e. step 4 is not `available=True`.
"""
from typing import List

from pydantic_ai.models.test import TestModel

RR_UNREADABLE = "the archive's audit list could not be read"
SIM_EMPTY_PREFIX = "the archive returned no similar articles"


def write_blockers(settings, rt) -> List[str]:
    """Why this process must not write to the archive right now ([] = writes allowed)."""
    out: List[str] = []
    if not settings.ARCHIVE_WRITES_ENABLED:
        out.append("writes_disabled")
    if rt is None or isinstance(getattr(rt, "model", None), TestModel):
        out.append("synthetic_model")
    if str(getattr(settings, "WEB_SEARCH_PROVIDER", "stub")).strip().lower() == "stub":
        out.append("stub_search")
    if rt is None or getattr(rt, "catalog", None) is None:
        out.append("no_concept_catalog")
    client = getattr(rt, "karakutu_client", None)
    if client is None or not getattr(client, "enabled", False):
        out.append("archive_client_disabled")
    return out


def _stage_details(record: dict, stage: str) -> dict:
    for s in ((record.get("response") or {}).get("stages") or []):
        if s.get("stage") == stage:
            return s.get("details") or {}
    return {}


def archive_degradation(record: dict) -> List[str]:
    """Signals that the archive was unreachable while this record was produced ([] = none)."""
    rr = _stage_details(record, "related_removals")
    sim = _stage_details(record, "similarity")
    out: List[str] = []
    rr_unreadable = rr.get("available") is False and rr.get("reason") == RR_UNREADABLE
    if rr_unreadable:
        out.append("related_removals_unreadable")
    sim_empty = sim.get("available") is False and str(sim.get("reason") or "").startswith(SIM_EMPTY_PREFIX)
    if sim_empty and rr.get("available") is not True:
        out.append("similarity_unreachable")
    return out
