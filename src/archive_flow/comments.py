"""Discussion-comment templates for the step-wise flow. Each renders ONE result record (as written
by run_open_cases.py) into plain text that also reads fine as Markdown — the archive's discussion
API takes a bare string (≤ 10,000 chars, no format field) and the frontend has no discussion UI
yet, so nothing here depends on a renderer: no HTML, no tables, no code fences.

RENDERING (measured 2026-10-01): the archive's frontend shows a comment as plain text in a normal block
— no Markdown, no whitespace-pre-wrap — so single newlines collapse into spaces. Every template therefore
(a) prefixes each step with a visible marker (①…⑥, →, ⚑) so the collapsed, one-paragraph form still
scans, and (b) separates blocks with BLANK lines, which read as paragraphs the day the frontend
honours whitespace or Markdown. Do not rely on a line break to separate two facts.

Every template states, in words, what happened to the IPTC classification — SET on the audit by
this system, or PROPOSED ONLY when the bot did not write it (operator's rule, 2026-09-15: an audit
that already carries any IPTC field is never overwritten; the bot's ranked proposal then goes into
the comment for comparison) — gives the verdict in plain language, lists the evidence URLs, and
closes with the non-finding disclaimer. `render(key, record, tags_state=…)` takes the state from
apply_rules.tags_state_for_comment. They differ in shape and length:

  A  ledger     — labelled block per step, most complete
  B  narrative  — a short auditor-voice paragraph per step
  C  checklist  — one line per step with a status marker, then evidence
  D  minimal    — verdict, IPTC line, evidence; three short blocks
"""
from datetime import datetime, timezone

from src.archive_flow.rules import TAGS_PROPOSED, TAGS_PROPOSED_EXISTING, TAGS_SET  # noqa: F401

BOT = "karakutu-audit (automated six-step audit)"
BOT_PREFIX = "karakutu-audit (automated"   # matches every byline the bot has ever used (five-step before 2026-09-30)

VERDICT_WORDS = {
    "MOVED": "MOVED — the same publisher republished the article at a new URL (measured, verbatim copy).",
    "LIKELY_MOVED": "LIKELY MOVED — a same-publisher copy exists but the text only partly matches; a human should compare.",
    "FOUND_ONLINE": "FOUND ONLINE — the story is live at another outlet. This says nothing about why this copy was removed.",
    "POSSIBLE_CENSORSHIP": "FLAGGED FOR REVIEW — a copy of this story is demonstrably gone. Not a finding of censorship; a human must decide.",
    "NOT_REMOVED": "NOT REMOVED (today) — the original URL answers with the article's text. Possibly restored or intermittent; please re-check.",
    "LIKELY_LOW_PUBLIC_INTEREST": "OUT OF SCOPE — screened as low public interest; the remaining steps were not run.",
    "INCONCLUSIVE": "INCONCLUSIVE — nothing could be established either way (search recall is limited).",
    "AUDIT_FAILED": "AUDIT FAILED — the model failed; this is not a result and the audit should be re-run.",
}
DISCLAIMER = ("Automated triage signal, not a finding. Every URL above was actually fetched by the bot; "
              "verdicts are pointers for a human auditor.")


def _stages(r):
    return {s["stage"]: s for s in ((r.get("response") or {}).get("stages") or [])}


def _pct(x):
    return "—" if x is None else f"{x:.0%}"


TAGS_WORDING = {
    TAGS_SET: "set on this audit by the bot — ranked, most central first",
    TAGS_PROPOSED_EXISTING: ("PROPOSED ONLY, not written — this audit already carried IPTC tags when the bot ran "
                             "and the bot never overwrites; ranked, most central first, for comparison"),
    TAGS_PROPOSED: "PROPOSED ONLY, not written onto this audit — ranked, most central first",
}


def iptc_body(r):
    """'topics …; genre …; scope …' from the record's classification, or None when there is nothing."""
    c = r.get("classification") or {}
    tags = c.get("subject_tags") or []
    if not tags and not c.get("genre") and not c.get("geographic_scope"):
        return None
    parts = []
    if tags:
        parts.append("topics " + " › ".join(f"{t['label']} [{t['notation']}]" for t in tags))
    if c.get("genre"):
        parts.append(f"genre {c['genre']['label']} [{c['genre']['notation']}]")
    if c.get("geographic_scope"):
        parts.append(f"scope {c['geographic_scope']}")
    return "; ".join(parts)


def iptc_line(r, explicit=True, tags_state=TAGS_SET):
    body = iptc_body(r)
    if body is None:
        return "IPTC classification: not set (the classifier produced nothing usable)."
    if tags_state != TAGS_SET:
        return f"IPTC classification ({TAGS_WORDING[tags_state]}): {body}."
    who = f" ({TAGS_WORDING[TAGS_SET]})" if explicit else ""
    return f"IPTC classification{who}: {body}."


def similar_line(r):
    d = (_stages(r).get("similarity") or {}).get("details") or {}
    if not d.get("available"):
        return "Similar link: the archive returned no candidates (not archived, nothing similar, or archive unreachable)."
    cands = d.get("candidates") or []
    live = sum(1 for c in cands if c["status"] == "live")
    if d.get("relocated_to"):
        return (f"Similar link: {len(cands)} archive candidates checked; {d['relocated_to']} contains "
                f"{_pct(d.get('old_in_new'))} of this article's text → relocation confirmed.")
    partial = [c for c in cands if c.get("fit") == "partial"]
    extra = ""
    if partial:
        best = max(partial, key=lambda c: c.get("old_in_new") or 0)
        noun = "1 partial copy" if len(partial) == 1 else f"{len(partial)} partial copies, the closest"
        extra = f" {noun} ({_pct(best['old_in_new'])} of the text at {best['url']})."
    return f"Similar link: {len(cands)} archive candidates checked ({live} live); no verbatim republication.{extra}"


def scope_line(r):
    s = _stages(r).get("relevance") or {}
    if s.get("skipped"):
        return "Scope: not assessed (audit ended earlier)."
    verdict = "in scope" if s.get("outcome") == "pass" else "out of scope"
    return f"Scope: {verdict} — {s.get('explanation', '').strip()}"


def related_line(r):
    """None when the record predates the step (older runs), else one line."""
    s = _stages(r).get("related_removals")
    if not s:
        return None
    if s.get("skipped"):
        return "Related removals: not checked (audit ended earlier)."
    d = s.get("details") or {}
    if not d.get("available"):
        return f"Related removals: not checked ({d.get('reason') or 'archive unavailable'})."
    mem = d.get("members") or []
    if not d.get("flag"):
        return (f"Related removals: no pattern — {len(mem)} other removal(s) within ±{d.get('window_days', 14)} days confirmed "
                f"as the same subject (a flag needs 3 from 2 publishers, or one already decided censored).")
    items = "; ".join(f"{m.get('newspaper')}: {m.get('title', '')[:80]}" + (" [decided censored]" if m.get("decision") == "censored" else "") for m in mem)
    kept = " The scope screen alone had judged this article out of scope; the pattern kept it in the audit." if d.get("kept_in_scope") else ""
    return (f"RELATED REMOVALS — PATTERN FLAG: {len(mem)} other article(s) on the same subject disappeared within "
            f"±{d.get('window_days', 14)} days, from {len(d.get('publishers') or {})} publisher(s): {items}.{kept} "
            f"A pointer, not a finding — related removals can also be a publisher pruning a section.")


def source_line(r):
    s = _stages(r).get("source") or {}
    if s.get("skipped"):
        return "Source: not checked (audit ended earlier)."
    d = s.get("details") or {}
    itself = {"live_intact": "the original URL is live with the text intact",
              "live_other": "the original URL answers but without the article's text",
              "dead_or_unreachable": "the original URL does not return a readable page"}.get(d.get("self_status"), "the original URL was not probed")
    if d.get("source_kind") in ("agency", "other_outlet"):
        status = {"live": f"its copy at {d.get('source_url')} is live",
                  "dead": f"its copy at {d.get('source_url')} is GONE (404/410) — RED FLAG",
                  "unreadable": f"its copy at {d.get('source_url')} could not be read",
                  "leads_only": f"no URL cited; {len(d.get('extra_hits') or [])} lead(s) searched on {d.get('source_host')}",
                  "not_located": "no URL cited and the outlet could not be searched"}.get(d.get("source_status"), d.get("source_status", ""))
        cited = f"the text credits {d.get('source_name')}; {status}"
    elif d.get("source_kind") == "self":
        cited = "the text credits no agency or other outlet (own reporting)"
    else:
        cited = "the text does not identify where the reporting originated"
    return f"Source: {itself}; {cited}."


def web_line(r):
    s = _stages(r).get("web_search") or {}
    if s.get("skipped"):
        return "Web search: not run (audit ended earlier)."
    d = s.get("details") or {}
    parts = [f"{d.get('hits_searched', 0)} links found, {d.get('live_pages_read', 0)} pages read"]
    if d.get("matches"):
        parts.append("same story live at " + ", ".join(dict.fromkeys(d["matches"])))   # de-duplicated, order kept
    else:
        parts.append("no page matched the story")
    if d.get("dead_same_story"):
        parts.append("gone: " + ", ".join(dict.fromkeys(x["url"] for x in d["dead_same_story"])))
    if d.get("related_same_publisher"):
        parts.append(f"{len(d['related_same_publisher'])} same-topic page(s) on the publisher measured as different articles")
    return "Web search: " + "; ".join(parts) + "."


def evidence_urls(r):
    st = _stages(r); urls = []
    d = (st.get("similarity") or {}).get("details") or {}
    if d.get("relocated_to"):
        urls.append(("relocation", d["relocated_to"]))
    d = (st.get("web_search") or {}).get("details") or {}
    for u in d.get("matches") or []:
        if all(u != x[1] for x in urls):
            urls.append(("same story", u))
    for x in d.get("dead_same_story") or []:
        urls.append(("gone", x["url"]))
    d = (st.get("source") or {}).get("details") or {}
    if d.get("source_url"):
        urls.append(("cited source", d["source_url"]))
    return urls


def _verdict(r):
    v = (r.get("response") or {}).get("verdict") or r.get("verdict") or "ERROR"
    return v, VERDICT_WORDS.get(v, v)


def _flags(r):
    f = (r.get("response") or {}).get("flags") or []
    return ("Red flags: " + ", ".join(f) + ".") if f else "Red flags: none."


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- A: ledger ------------------------------------------------------------------

def render_A(r, tags_state=TAGS_SET):
    v, words = _verdict(r)
    lines = [f"{BOT} · {_stamp()}", f"VERDICT: {words}",
             "① " + iptc_line(r, tags_state=tags_state),
             "② " + similar_line(r),
             "③ " + scope_line(r)]
    rl = related_line(r)
    if rl is not None:
        lines.append("④ " + rl)
    lines += ["⑤ " + source_line(r),
              "⑥ " + web_line(r),
              "⚑ " + _flags(r)]
    ev = evidence_urls(r)
    if ev:
        lines += ["Evidence: " + " · ".join(f"{k}: {u}" for k, u in ev)]
    lines += [DISCLAIMER]
    return "\n\n".join(lines)


# --- B: narrative ----------------------------------------------------------------

def render_B(r, tags_state=TAGS_SET):
    v, words = _verdict(r)
    st = _stages(r)
    c = r.get("classification") or {}
    tags = " › ".join(t["label"] for t in (c.get("subject_tags") or [])) or "none"
    genre = (c.get("genre") or {}).get("label") or "not determined"
    scope = c.get("geographic_scope") or "not determined"
    codes = f"topics {tags}; genre {genre}; geographic scope {scope}"
    if tags_state == TAGS_SET:
        p1 = (f"This audit was run by {BOT}. The bot classified the article and wrote the IPTC codes onto this audit "
              f"itself: {codes}. Codes are ranked, most central first, and were validated against the archive's IPTC vocabulary.")
    elif tags_state == TAGS_PROPOSED_EXISTING:
        p1 = (f"This audit was run by {BOT}. The bot classified the article but did NOT write its codes onto this audit, "
              f"because the audit already carried IPTC tags and the bot never overwrites. Its ranked PROPOSAL (not written), "
              f"for comparison: {codes}.")
    else:
        p1 = (f"This audit was run by {BOT}. The bot classified the article; the codes below are a ranked PROPOSAL and "
              f"were not written onto this audit: {codes}.")
    rel = st.get("relevance") or {}
    if v == "LIKELY_LOW_PUBLIC_INTEREST":
        p2 = ("The public-interest screen judged the article out of scope, so the source and web checks were not run: "
              + rel.get("explanation", "").strip())
    elif v == "MOVED" and (st.get("similarity") or {}).get("outcome") == "pass":
        d = st["similarity"]["details"]
        p2 = (f"Among the archive's similar articles the bot found a same-publisher copy that contains "
              f"{_pct(d.get('old_in_new'))} of this article's text ({d['relocated_to']}), so it recorded the article "
              f"as relocated and stopped there.")
    else:
        p2 = " ".join([similar_line(r), scope_line(r).replace("Scope: ", "The scope screen found it "),
                       source_line(r).replace("Source: ", "On the source: "), web_line(r).replace("Web search: ", "On the web: ")])
    rl = related_line(r)
    if rl and rl.startswith("RELATED REMOVALS"):
        p2 += " " + rl
    p3 = f"Result: {words} {_flags(r)}"
    ev = evidence_urls(r)
    p4 = ("Links: " + "; ".join(f"{k} {u}" for k, u in ev)) if ev else ""
    return "\n\n".join(x for x in [p1, p2, p3, p4, DISCLAIMER] if x)


# --- C: checklist ----------------------------------------------------------------

def _mark(stage):
    if not stage:
        return "[ ]"
    if stage.get("skipped"):
        return "[–]"
    return {"pass": "[✓]", "fail": "[✗]", "inconclusive": "[?]"}.get(stage.get("outcome"), "[ ]")


C_HEAD = {
    TAGS_SET: "Classification (IPTC, set on this audit by the bot)",
    TAGS_PROPOSED_EXISTING: "Classification (IPTC — PROPOSED ONLY, not written: the audit already had tags; the bot never overwrites)",
    TAGS_PROPOSED: "Classification (IPTC — PROPOSED ONLY, not written onto this audit)",
}


def render_C(r, tags_state=TAGS_SET):
    v, words = _verdict(r)
    st = _stages(r)
    lines = [f"{BOT} · {_stamp()} · {v}"]
    body = iptc_body(r)
    lines.append(f"① {_mark(st.get('classification'))} {C_HEAD[tags_state]} — "
                 + (body + "." if body else "not set (the classifier produced nothing usable)."))
    lines.append(f"② {_mark(st.get('similarity'))} " + similar_line(r))
    lines.append(f"③ {_mark(st.get('relevance'))} " + scope_line(r))
    rl = related_line(r)
    if rl is not None:
        lines.append(f"④ {_mark(st.get('related_removals'))} " + rl)
    lines.append(f"⑤ {_mark(st.get('source'))} " + source_line(r))
    lines.append(f"⑥ {_mark(st.get('web_search'))} " + web_line(r))
    lines += [f"→ {words}", "⚑ " + _flags(r)]
    ev = evidence_urls(r)
    if ev:
        lines += ["Evidence — " + " · ".join(f"{k}: {u}" for k, u in ev)]
    lines += ["Legend: [✓] established · [✗] failed the screen / no pattern · [?] nothing established · [–] skipped. " + DISCLAIMER]
    return "\n\n".join(lines)


# --- D: minimal ------------------------------------------------------------------

def render_D(r, tags_state=TAGS_SET):
    v, words = _verdict(r)
    ev = evidence_urls(r)
    lines = [f"{BOT}: {words}", "① " + iptc_line(r, tags_state=tags_state)]
    rl = related_line(r)
    if rl and rl.startswith("RELATED REMOVALS"):
        lines += ["④ " + rl]
    if ev:
        lines += ["Evidence: " + " · ".join(f"{k} {u}" for k, u in ev)]
    flags = (r.get("response") or {}).get("flags") or []
    if flags:
        lines += ["⚑ Red flags: " + ", ".join(flags)]
    lines += [DISCLAIMER]
    return "\n\n".join(lines)


TEMPLATES = {"A": render_A, "B": render_B, "C": render_C, "D": render_D}
TITLES = {"A": "Ledger", "B": "Narrative", "C": "Checklist", "D": "Minimal"}


def render(key, r, *, tags_state=TAGS_SET):
    """Render one result record; `tags_state` is TAGS_SET / TAGS_PROPOSED_EXISTING / TAGS_PROPOSED."""
    return TEMPLATES[key](r, tags_state=tags_state)
