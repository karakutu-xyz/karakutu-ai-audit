"""The operator's standing live-apply rules (2026-09-15), pinned:

  * only audits in status 0 / 2 / 3 are touched (screening, under investigation, ready to publish);
  * IPTC tags are written only when none of the three tag fields is set — one set field stops
    the write, and there is no override;
  * when the tags are not written the comment carries the bot's proposal, worded as PROPOSED
    ONLY — never "set on this audit".

The rules and templates live in `src/archive_flow/` (moved from `scripts/open_audit/` on
2026-10-05); the scripts keep re-export shims, pinned identical at the end of this file.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from src.archive_flow import comments as templates
from src.archive_flow import rules

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "open_audit"


def _load_shim(name):
    """Load a scripts/open_audit shim by path, as the operator scripts import it."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(f"_shim_{name}", SCRIPTS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


PAYLOAD = {"subject_tag_notations": ["medtop:20000087"], "content_type_notation": "genre:Current",
           "geographic_scope": "national"}
EMPTY_PAYLOAD = {"subject_tag_notations": [], "content_type_notation": None, "geographic_scope": None}


def audit(status=0, decision=None, **tags):
    a = {"id": 1, "status": status, "decision": decision, "subject_tag_notations": [],
         "content_type_notation": None, "geographic_scope": None}
    a.update(tags)
    return a


# --- eligibility ---------------------------------------------------------------------

def test_eligible_statuses_are_exactly_screening_under_investigation_and_ready_to_publish():
    assert rules.ELIGIBLE_STATUSES == frozenset({0, 2, 3})


@pytest.mark.parametrize("status", [1, 4, 5, None])
def test_other_statuses_get_neither_tags_nor_comment(status):
    a = audit(status=status)
    assert not rules.tag_decision(a, PAYLOAD, {}).write
    assert not rules.comment_decision(a, 0, "hi", {}).write
    assert "eligible" in rules.tag_decision(a, PAYLOAD, {}).reason


@pytest.mark.parametrize("status,decision", [(0, None), (2, None), (3, "censored"), (3, "preservation"), (2, "relocated")])
def test_eligible_statuses_are_written_even_when_a_human_decision_exists(status, decision):
    a = audit(status=status, decision=decision)
    assert rules.tag_decision(a, PAYLOAD, {}).write
    assert rules.comment_decision(a, 0, "hi", {}).write


# --- tags: never overwrite ---------------------------------------------------------------

@pytest.mark.parametrize("field,value", [("subject_tag_notations", ["medtop:04000000"]),
                                         ("content_type_notation", "genre:Opinion"),
                                         ("geographic_scope", "local")])
def test_one_set_field_is_enough_to_stop_the_tag_write(field, value):
    d = rules.tag_decision(audit(**{field: value}), PAYLOAD, {})
    assert not d.write
    assert "never overwritten" in d.reason


def test_tags_are_written_only_when_all_three_fields_are_empty():
    assert rules.tag_decision(audit(), PAYLOAD, {}).write


def test_no_classification_means_nothing_to_write():
    assert not rules.tag_decision(audit(), EMPTY_PAYLOAD, {}).write


def test_a_logged_write_is_never_repeated():
    assert not rules.tag_decision(audit(), PAYLOAD, {"tags": {"ok": True}}).write
    assert not rules.comment_decision(audit(), 0, "hi", {"comment": {"ok": True}}).write


def test_there_is_no_override_for_the_no_overwrite_rule():
    import inspect
    assert "overwrite_tags" not in inspect.getsource(rules)
    assert "overwrite-tags" not in (SCRIPTS / "apply_results.py").read_text()
    assert "include-decided" not in (SCRIPTS / "apply_results.py").read_text()


# --- comments ---------------------------------------------------------------------------

def test_comment_needs_zero_known_discussions():
    assert not rules.comment_decision(audit(), None, "hi", {}).write   # unknown is not zero
    assert not rules.comment_decision(audit(), 1, "hi", {}).write
    assert not rules.comment_decision(audit(), 0, "x" * 10_001, {}).write
    assert rules.comment_decision(audit(), 0, "x" * 10_000, {}).write


# --- the comment's IPTC wording follows the actual outcome ---------------------------------

def test_tags_state_is_set_only_after_a_real_or_projected_write():
    assert rules.tags_state_for_comment(audit(), {"tags": {"ok": True}}, projected_write=False, dry_run=False) == "set"
    assert rules.tags_state_for_comment(audit(), {}, projected_write=True, dry_run=True) == "set"
    # apply mode, write did not happen (failed or not enabled): never claim "set"
    assert rules.tags_state_for_comment(audit(), {"tags": {"ok": False}}, projected_write=True, dry_run=False) == "proposed"
    assert rules.tags_state_for_comment(audit(), {}, projected_write=False, dry_run=False) == "proposed"
    assert rules.tags_state_for_comment(audit(subject_tag_notations=["medtop:1"]), {}, projected_write=False,
                                        dry_run=False) == "proposed_existing"


RECORD = {
    "audit_id": 1, "url": "https://example.com/a", "verdict": "INCONCLUSIVE",
    "classification": {"subject_tags": [{"notation": "medtop:20000087", "label": "corporate crime"}],
                       "genre": {"notation": "genre:Current", "label": "Current Events"},
                       "geographic_scope": "national", "gist": "x"},
    "response": {"verdict": "INCONCLUSIVE", "flags": [], "stages": [
        {"stage": "classification", "outcome": "pass", "skipped": False, "details": {}},
        {"stage": "similarity", "outcome": "inconclusive", "skipped": False, "details": {"available": False}},
        {"stage": "relevance", "outcome": "pass", "skipped": False, "explanation": "in scope", "details": {}},
        {"stage": "source", "outcome": "inconclusive", "skipped": False, "details": {"self_status": "dead_or_unreachable"}},
        {"stage": "web_search", "outcome": "fail", "skipped": False, "details": {"hits_searched": 3, "live_pages_read": 1}},
    ]},
}


@pytest.mark.parametrize("key", sorted(templates.TEMPLATES))
def test_every_template_says_set_when_the_tags_were_written(key):
    text = templates.render(key, RECORD, tags_state="set")
    assert "medtop:20000087" in text or "corporate crime" in text
    assert "PROPOSED" not in text
    assert "set on this audit" in text or "wrote the IPTC codes onto this audit" in text


@pytest.mark.parametrize("key", sorted(templates.TEMPLATES))
@pytest.mark.parametrize("state", ["proposed_existing", "proposed"])
def test_every_template_marks_unwritten_codes_as_proposed_and_still_lists_them(key, state):
    text = templates.render(key, RECORD, tags_state=state)
    assert "medtop:20000087" in text or "corporate crime" in text
    assert "PROPOSED" in text or "PROPOSAL" in text
    assert "set on this audit" not in text
    assert "wrote the IPTC codes onto this audit" not in text
    if state == "proposed_existing":
        assert "never overwrites" in text


def test_default_render_wording_is_set():
    assert templates.render("C", RECORD) == templates.render("C", RECORD, tags_state="set")


# --- a failed audit is not a result ---------------------------------------------------------

@pytest.mark.parametrize("verdict", ["AUDIT_FAILED", "ERROR", None])
def test_failed_or_errored_records_drive_no_writes(verdict):
    rec = {"audit_id": 1, "verdict": verdict, "classification": RECORD["classification"],
           "response": ({"verdict": verdict, "stages": []} if verdict else None)}
    d = rules.result_usable(rec)
    assert not d.write
    assert "re-run" in d.reason


def test_a_completed_record_is_usable():
    assert rules.result_usable(RECORD).write


# --- comment wording hygiene (review of 2026-09-20) ----------------------------------------

def _rec_with(similarity_details=None, web_details=None):
    rec = json.loads(json.dumps(RECORD))
    st = {s["stage"]: s for s in rec["response"]["stages"]}
    if similarity_details is not None:
        st["similarity"]["details"] = similarity_details
    if web_details is not None:
        st["web_search"]["details"] = web_details
    return rec


import json  # noqa: E402


def test_partial_copies_are_pluralised_and_the_closest_one_is_named():
    cands = [{"url": "https://p.example/a", "status": "live", "fit": "partial", "old_in_new": 0.28},
             {"url": "https://p.example/b", "status": "live", "fit": "partial", "old_in_new": 0.33}]
    text = templates.similar_line(_rec_with(similarity_details={"available": True, "candidates": cands}))
    assert "2 partial copies, the closest (33% of the text at https://p.example/b)" in text
    text1 = templates.similar_line(_rec_with(similarity_details={"available": True, "candidates": cands[:1]}))
    assert "1 partial copy (28%" in text1


def test_duplicate_match_urls_are_listed_once():
    text = templates.web_line(_rec_with(web_details={"hits_searched": 10, "live_pages_read": 8,
                                                     "matches": ["https://p.example/x", "https://p.example/x"]}))
    assert text.count("https://p.example/x") == 1


# --- rendering: the archive frontend collapses single newlines (measured 2026-10-01) -------------

@pytest.mark.parametrize("key", ["A", "C", "D"])
def test_blocks_are_separated_by_blank_lines_and_carry_visible_markers(key):
    text = templates.render(key, RECORD, tags_state="set")
    assert "\n\n" in text and "\n \n" not in text
    collapsed = " ".join(text.split())          # what the frontend shows today
    assert "①" in collapsed                     # steps stay scannable when the breaks are gone
    assert "Automated triage signal" in collapsed
    for block in text.split("\n\n"):
        assert "\n" not in block, "a single newline inside a block would silently vanish in the archive UI"


# --- re-tagging the bot's own earlier lists (2026-10-01) ------------------------------------------

PREV = {"subject_tag_notations": ["medtop:20000082", "medtop:20000093"], "content_type_notation": "genre:Current", "geographic_scope": "national"}
NEW = {"subject_tag_notations": ["medtop:20000093"], "content_type_notation": "genre:Current", "geographic_scope": "national"}


def test_retag_only_when_the_archive_still_holds_exactly_what_the_bot_wrote():
    held = audit(status=2, **PREV)
    assert rules.retag_decision(held, PREV, NEW, {}).write
    # a human touched any field → left alone
    edited = audit(status=2, **{**PREV, "subject_tag_notations": ["medtop:20000093", "medtop:11000000"]})
    assert "human changed" in rules.retag_decision(edited, PREV, NEW, {}).reason
    scoped = audit(status=2, **{**PREV, "geographic_scope": "local"})
    assert not rules.retag_decision(scoped, PREV, NEW, {}).write


def test_retag_skips_identical_lists_ineligible_statuses_and_logged_retags():
    held = audit(status=2, **PREV)
    assert "identical" in rules.retag_decision(held, PREV, PREV, {}).reason
    assert not rules.retag_decision(audit(status=4, **PREV), PREV, NEW, {}).write
    assert not rules.retag_decision(held, PREV, NEW, {"retag": {"ok": True}}).write
    assert not rules.retag_decision(held, PREV, {"subject_tag_notations": [], "content_type_notation": None, "geographic_scope": None}, {}).write


# --- the scripts/open_audit shims re-export the moved modules identically -----------------

@pytest.mark.parametrize("shim,module", [("apply_rules", rules), ("comment_templates", templates)])
def test_scripts_shims_reexport_the_same_objects(shim, module):
    mod = _load_shim(shim)
    names = [n for n in vars(module) if not n.startswith("__")
             and getattr(vars(module)[n], "__module__", module.__name__) == module.__name__]
    names += [n for n in ("TAGS_SET", "TAGS_PROPOSED", "TAGS_PROPOSED_EXISTING") if hasattr(module, n)]
    assert names
    for n in names:
        assert getattr(mod, n) is getattr(module, n), n
