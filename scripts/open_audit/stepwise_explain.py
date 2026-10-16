"""Plain-language explanations of what each of the five steps DID on one case.

Shared by build_stepwise_report.py (HTML/Markdown) and the batch report page. Every function
takes the recorded `StageResult` (with its `details`) and returns a list of short sentences a
reviewer can read without knowing the code. Notations are resolved to labels through the cached
IPTC catalog when it is present (.cache/karakutu-concepts.json)."""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STEP_TITLES = {
    "classification": "Classification",
    "similarity": "Find similar link",
    "relevance": "Out of scope?",
    "related_removals": "Related removals",
    "source": "Find the source",
    "web_search": "Web search",
}
STEP_WHAT = {
    "classification": "Two model calls label the article with IPTC media topics (up to 5, ranked), one genre and a geographic scope; every notation is checked against the vocabulary the model was shown.",
    "similarity": "The archive's own similarity index proposes up to 5 articles; each is fetched and, when it is on the same publisher, its text is measured against the removed article.",
    "relevance": "The public-interest screen: does the article carry accountability stakes? If not, the audit stops here — unless the next step finds a pattern.",
    "related_removals": "Did other articles on the SAME subject vanish from the archive within ±14 days? Key terms → every audit created in the window → the model confirms same subject/event → a pattern flag (≥3 from ≥2 publishers, or one human-decided censored) keeps an out-of-scope article in the audit.",
    "source": "(a) Is the removed URL itself live with the article's text? (b) Does the text credit an agency or another outlet, and is that copy live?",
    "web_search": "Three searches (the publisher's site, the open web, one distinctive quoted phrase) plus leads from earlier steps; pages are fetched and the model judges which are the same story.",
}


def load_labels() -> dict:
    """notation -> label from the cached catalog; {} when the cache is absent."""
    try:
        data = json.loads((REPO / ".cache" / "karakutu-concepts.json").read_text())
    except (OSError, ValueError):
        return {}
    return {c["notation"]: c["pref_label"] for c in data.get("topics", []) + data.get("genres", [])}


def _lab(n, labels):
    return f"{labels[n]} ({n})" if n in labels else n


def _pct(x):
    return "—" if x is None else f"{x:.0%}"


def explain_classification(stage, labels) -> list:
    d = stage.get("details") or {}
    if stage.get("skipped") or d.get("skipped"):
        return [f"Skipped — {stage.get('explanation', '')}"]
    if d.get("model_failed"):
        return ["The classification model failed on both attempts, so no tags were assigned. This does not affect the other steps."]
    out = []
    tops = d.get("coarse_top_levels") or []
    g = d.get("genre")
    out.append(
        "Call 1 filed the article under " + ("; ".join(_lab(n, labels) for n in tops) or "no top-level topic")
        + f". Genre: {g['label'] + ' (' + g['notation'] + ')' if g else 'not determined'}."
        + f" Geographic scope: {d.get('geographic_scope') or 'not determined'}.")
    tags = d.get("subject_tags") or []
    if d.get("fine_failed"):
        out.append("Call 2 (the specific topics) failed twice, so the broad top-level topic(s) stand as the tags.")
    elif tags:
        out.append(f"Call 2 was shown {d.get('fine_menu_size', '?')} candidate topics beneath those and chose "
                   f"{len(tags)}, most central first: " + "; ".join(f"{i}. {t['label']}" for i, t in enumerate(tags, 1)) + ".")
    for n in d.get("dropped") or []:
        if n in labels:
            out.append(f"The model also proposed {_lab(n, labels)}, a real IPTC concept that was NOT among the candidates it was shown "
                       "(it sits under a top-level topic it did not select), so the vocabulary check discarded it.")
        else:
            out.append(f"The model also proposed “{n}”, which is not an IPTC notation, so the vocabulary check discarded it.")
    if d.get("gist"):
        out.append(f"Gist written by the model: “{d['gist']}”")
    return out


def explain_similarity(stage) -> list:
    d = stage.get("details") or {}
    if not d.get("available"):
        return [f"No archive candidates — {d.get('reason') or stage.get('explanation', '')}."]
    cands = d.get("candidates") or []
    hosts = sorted({c["url"].split("/")[2].removeprefix("www.") for c in cands})
    live = [c for c in cands if c["status"] == "live"]
    dead = [c for c in cands if c["status"] == "dead"]
    other = [c for c in cands if c["status"] not in ("live", "dead")]
    out = [f"The archive proposed {len(cands)} similar article(s) on {', '.join(hosts)}. Fetched: {len(live)} live, {len(dead)} dead (404/410), {len(other)} unreadable or excluded as the removed URL itself."]
    measured = [c for c in live if c.get("same_publisher")]
    if measured:
        verbatim = [c for c in measured if c.get("fit") == "verbatim"]
        partial = [c for c in measured if c.get("fit") == "partial"]
        sibling = [c for c in measured if c.get("fit") == "sibling"]
        unm = [c for c in measured if c.get("fit") == "unmeasured"]
        parts = []
        if verbatim:
            parts.append(f"{len(verbatim)} verbatim copy ({', '.join(_pct(c['old_in_new']) + ' of the original present' for c in verbatim)})")
        if partial:
            parts.append(f"{len(partial)} partial copy ({', '.join(_pct(c['old_in_new']) + ' of the original present at ' + c['url'] for c in partial)})")
        if sibling:
            parts.append(f"{len(sibling)} different article(s) on the same topic (at most {max(_pct(c['old_in_new']) for c in sibling)} of the original's text present — siblings, not copies)")
        if unm:
            parts.append(f"{len(unm)} unmeasurable")
        out.append(f"Measured against the removed article's text ({len(measured)} same-publisher live page(s)): " + "; ".join(parts) + ".")
    if d.get("relocated_to"):
        out.append(f"→ Measured relocation: {d['relocated_to']} contains {_pct(d.get('old_in_new'))} of the original's text "
                   f"(and {_pct(d.get('new_in_old'))} of its own text comes from the original). Verdict MOVED; the remaining steps are skipped.")
    else:
        out.append("No verbatim same-publisher republication was found here, so nothing was decided at this step; "
                   f"the {len(live)} live candidate(s) were handed to the web-search step as leads.")
    return out


def explain_relevance(stage) -> list:
    if stage.get("skipped"):
        return [f"Skipped — {stage.get('explanation', '')}"]
    conf = stage.get("confidence")
    conf_s = f" (model confidence {conf:.0%})" if isinstance(conf, (int, float)) else ""
    if stage.get("outcome") == "pass":
        return [f"IN SCOPE{conf_s}: {stage.get('explanation', '')}"]
    return [f"OUT OF SCOPE{conf_s} — the audit stops here (verdict LIKELY_LOW_PUBLIC_INTEREST): {stage.get('explanation', '')}"]


def explain_related_removals(stage) -> list:
    if stage.get("skipped"):
        return [f"Skipped — {stage.get('explanation', '')}"]
    d = stage.get("details") or {}
    if not d.get("available"):
        return [f"Not checked — {d.get('reason') or stage.get('explanation', '')}."]
    when = {"first_dead_check": "the archive's own detection moment", "now": "the run time (detection moment unknown)"}.get(d.get("anchor_source"), "?")
    out = [f"Window: every audit the archive created within ±{d.get('window_days', 14)} days of {when} — {d.get('window_size', 0)} other removal(s) after excluding this article's own URL variants."]
    ents = d.get("entities") or []
    out.append("Key terms taken from the title and text: " + (", ".join(ents[:10]) or "none") + (f" (dropped as generic in this window: {', '.join(d['generic_dropped'])})" if d.get("generic_dropped") else "") + ".")
    pre = d.get("prefiltered") or []; mem = d.get("members") or []
    if not pre:
        out.append("No other removal shared those terms, so nothing was put to the model.")
        return out
    out.append(f"{len(pre)} removal(s) shared the terms and were shown to the model with their publication dates; "
               + ("the model could not answer, so nothing was confirmed." if d.get("model_failed") else f"it confirmed {len(mem)} as the SAME subject or event (not merely the same topic)."))
    for m in mem:
        out.append(f"  · {m.get('newspaper')}: {m.get('title', '')[:100]}" + (" — a human has already decided this one censored" if m.get("decision") == "censored" else "") + f" (shared: {', '.join(m.get('shared_terms') or [])})")
    if d.get("flag"):
        out.append(f"→ PATTERN FLAG (RELATED_REMOVALS): {len(mem)} confirmed from {len(d.get('publishers') or {})} publisher(s)"
                   + (f"; {len(d.get('human_censored') or [])} already human-decided censored" if d.get("human_censored") else "")
                   + (". The scope screen had said out of scope; this flag kept the audit going to the source and web-search steps." if d.get("kept_in_scope") else ".")
                   + " A pointer for the reviewer, not a finding — related removals can also be a publisher pruning a section.")
    else:
        out.append("No flag: fewer than 3 confirmed members from 2 publishers and none human-decided censored.")
    return out


def explain_source(stage) -> list:
    if stage.get("skipped"):
        return [f"Skipped — {stage.get('explanation', '')}"]
    d = stage.get("details") or {}
    out = []
    itself = {
        "live_intact": f"(a) The removed URL itself still answers with the article's text ({_pct(d.get('self_containment'))} of the archived text present) → the premise of a removed article does not hold today (verdict NOT_REMOVED; descriptive, not exculpatory).",
        "live_other": "(a) The removed URL answers HTTP 200 but not with the article's text — a placeholder, a soft-404 or a script shell — so it does not count as live.",
        "dead_or_unreachable": "(a) The removed URL does not return a readable page (dead, blocked or unreachable) — consistent with a removal.",
    }.get(d.get("self_status"), "(a) The removed URL was not probed.")
    out.append(itself)
    n_links, n_phr = len(d.get("extracted_links") or []), len(d.get("extracted_phrases") or [])
    found = f"{n_links} outbound link(s) and {n_phr} attribution phrase(s) were found in the text"
    if d.get("model_failed"):
        out.append(f"(b) The source-finding model failed, so the cited source is unknown ({found}).")
    elif d.get("source_kind") == "self":
        out.append(f"(b) The text credits no agency or other outlet ({found}) → the reporting is the publisher's own; there is no separate source copy to check.")
    elif d.get("source_kind") == "unknown":
        out.append(f"(b) The text does not say where the reporting originated ({found}); nothing further to check.")
    else:
        who = f"{d.get('source_kind', '').replace('_', ' ')} “{d.get('source_name')}”"
        status = {
            "live": f"its copy at {d.get('source_url')} is LIVE ({_pct(d.get('source_containment'))} of the archived text present) and was handed to the web-search step as evidence",
            "dead": f"its copy at {d.get('source_url')} answers 404/410 → RED FLAG SOURCE_DEAD: the origin copy of the story is gone too (a pointer for a human, not a finding)",
            "unreadable": f"its copy at {d.get('source_url')} could not be read (blocked or not an article page) — that is not evidence of anything",
            "leads_only": f"no URL was cited, so one site-scoped search on {d.get('source_host')} produced {len(d.get('extra_hits') or [])} lead(s) for the web-search step",
            "not_located": "no URL was cited and the outlet could not be searched",
        }.get(d.get("source_status"), d.get("source_status", ""))
        out.append(f"(b) The text credits {who}; {status}.")
        if d.get("attribution_quote"):
            out.append(f"Credit line found: “{d['attribution_quote'][:200]}”")
    for note in d.get("dropped_claims") or []:
        out.append(f"A model claim was discarded by the text check: {note}.")
    return out


def explain_web_search(stage) -> list:
    if stage.get("skipped"):
        return [f"Skipped — {stage.get('explanation', '')}"]
    d = stage.get("details") or {}
    out = [f"{d.get('hits_searched', 0)} candidate link(s) in total (three searches plus leads from earlier steps) → {d.get('live_pages_read', 0)} readable page(s) fetched, {d.get('unreadable_count', 0)} unreadable (blocked, non-article or the removed URL itself)."]
    matches = d.get("matches") or []
    siblings = d.get("related_same_publisher") or []
    if siblings:
        out.append(f"The model called {len(siblings)} same-publisher page(s) the same story, but their text measured as DIFFERENT "
                   "articles on the same topic (well under the 20% sibling ceiling), so they were set aside as reviewer context "
                   "and do not count as matches.")
    if matches:
        out.append(f"{len(matches)} page(s) count as the same story: " + ", ".join(matches) + ".")
    else:
        out.append("No fetched page counts as a copy of the same story.")
    if d.get("relocated_to"):
        out.append(f"One of them is on the same publisher → relocation {d['relocated_to']} ({_pct(d.get('old_in_new'))} of the original present, {_pct(d.get('new_in_old'))} of the copy from the original).")
    if d.get("dead_same_story"):
        out.append("Dead same-story copies (the only basis for the review flag): " + "; ".join(f"{x['url']} ({x['reason']})" for x in d["dead_same_story"]) + ".")
    out.append(f"Model's note: {stage.get('explanation', '')}")
    return out


EXPLAIN = {
    "classification": lambda s, labels: explain_classification(s, labels),
    "similarity": lambda s, labels: explain_similarity(s),
    "relevance": lambda s, labels: explain_relevance(s),
    "related_removals": lambda s, labels: explain_related_removals(s),
    "source": lambda s, labels: explain_source(s),
    "web_search": lambda s, labels: explain_web_search(s),
}
