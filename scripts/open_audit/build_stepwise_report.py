"""Results JSON (run_open_cases.py) → a step-by-step report: one card per case with the five
steps in order, then a roll-up. Writes <results>.html and <results>.md beside the input.

BATCH MODE (records carry `list` / `probe`, i.e. the fixture came from fetch_listed_cases.py): adds a
batch overview — verdict × publisher, verdict × audit status, the apply PROJECTION under the standing
rules of apply_rules.py (what would be tagged / commented / worded PROPOSED ONLY; the apply script
re-checks everything live), the human-vs-bot tag agreement on audits humans had already tagged, a
human-attention list, and an index — and groups the per-case cards by publisher in collapsible
<details> (open for the attention verdicts). A `probe.json` beside the results, when present,
supplies the humans' genre/scope for the agreement table.

Usage: .venv311/bin/python scripts/open_audit/build_stepwise_report.py runs/fresh-2026-09-09/results.json
"""
import html, json, statistics, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stepwise_explain import EXPLAIN, STEP_WHAT, load_labels  # noqa: E402 — plain-language step explanations
from apply_rules import ELIGIBLE_STATUSES, STATUS_NAMES, result_usable  # noqa: E402 — the standing live-apply rules
LABELS = load_labels()
ATTENTION = {"POSSIBLE_CENSORSHIP", "MOVED", "LIKELY_MOVED", "NOT_REMOVED", "FOUND_ONLINE", "AUDIT_FAILED", "ERROR"}  # + any record with flags

RESULTS = Path(sys.argv[1]).resolve()
data = json.loads(RESULTS.read_text())
rows = data["results"]
BATCH = any(r.get("list") for r in rows)
PROBE = {}
_probe_path = RESULTS.with_name("probe.json")
if BATCH and _probe_path.exists():
    try:
        PROBE = {int(k): v.get("audit") or {} for k, v in json.loads(_probe_path.read_text()).get("audits", {}).items() if v.get("http") == 200}
    except (ValueError, OSError):
        PROBE = {}
ORDER = ["classification", "similarity", "relevance", "related_removals", "source", "web_search"]
TITLES = {"classification": "1 · Classification (IPTC topics · genre · geographic scope)",
          "similarity": "2 · Find similar link (archive similarity → fetched → measured)",
          "relevance": "3 · Out of scope?", "related_removals": "4 · Related removals (same subject vanished within ±14 days?)",
          "source": "5 · Find the source (itself · cited)",
          "web_search": "6 · Web search (elsewhere · publisher's site)"}


def E(x):
    # html.escape needs a str; stage fields (explanation, statuses, findings) can be null in a
    # crashed / skipped record, so coerce None → "" rather than crash the whole report.
    return html.escape("" if x is None else str(x))


def pct(x):
    return "—" if x is None else f"{x:.0%}"


def num(x, fmt=".2f"):
    return "—" if x is None else format(x, fmt)


def stage_block(s):
    d = s.get("details") or {}
    name = s["stage"]
    status = "SKIPPED" if s.get("skipped") else str(s.get("outcome") or "?").upper()
    head = f"{status} — {E(s.get('explanation'))}"
    body = []
    body.append(f"<p class=whatdo>{E(STEP_WHAT.get(name, ''))}</p><ul class=what>" + "".join(f"<li>{E(line)}</li>" for line in EXPLAIN[name](s, LABELS)) + "</ul>")
    if name == "classification" and d and not d.get("skipped"):
        tags = " › ".join(f"<code>{E(t.get('notation'))}</code> {E(t.get('label'))}" for t in (d.get("subject_tags") or [])) or "none"
        body.append(f"<div><b>Topics (ranked):</b> {tags}</div>")
        g = d.get("genre"); body.append(f"<div><b>Genre:</b> {E(g.get('label')) + ' (' + E(g.get('notation')) + ')' if g else '—'} &nbsp; <b>Scope:</b> {E(d.get('geographic_scope') or '—')}</div>")
        body.append(f"<div><b>Gist:</b> {E(d.get('gist') or '')}</div>")
        if d.get("dropped"): body.append(f"<div class=warn>dropped (not in vocabulary): {E(', '.join(d['dropped']))}</div>")
    if name == "similarity" and d.get("candidates"):
        body.append("<table><tr><th>archive candidate</th><th>score</th><th>same pub.</th><th>status</th><th>old→new</th><th>new→old</th><th>fit</th></tr>" + "".join(
            f"<tr><td><a href='{E(c.get('url'))}'>{E((c.get('url') or '')[:90])}</a></td><td>{num(c.get('score'))}</td><td>{'✓' if c.get('same_publisher') else ''}</td><td>{E(c.get('status'))}</td><td>{pct(c.get('old_in_new'))}</td><td>{pct(c.get('new_in_old'))}</td><td>{E(c.get('fit') or '')}</td></tr>"
            for c in d["candidates"]) + "</table>")
    if name == "source" and d:
        body.append(f"<div><b>Itself:</b> {E(d.get('self_status',''))} (containment {pct(d.get('self_containment'))})</div>")
        # Built outside the f-string: a backslash-escaped quote inside an f-string expression is a
        # SyntaxError on Python 3.11 (only 3.12+ allows it).
        src_link = f"<a href='{E(d['source_url'])}'>{E(d['source_url'])}</a>" if d.get("source_url") else "—"
        body.append(f"<div><b>Cited source:</b> kind={E(d.get('source_kind',''))} name={E(d.get('source_name') or '—')} url={src_link} status=<b>{E(d.get('source_status',''))}</b> containment {pct(d.get('source_containment'))}</div>")
        if d.get("attribution_quote"): body.append(f"<div><i>“{E(d['attribution_quote'][:200])}”</i></div>")
        if d.get("extracted_links"): body.append(f"<div class=small>links in text: {', '.join(E(u) for u in d['extracted_links'][:6])}</div>")
        if d.get("extracted_phrases"): body.append(f"<div class=small>attribution phrases: {' | '.join(E(p) for p in d['extracted_phrases'][:4])}</div>")
        if d.get("dropped_claims"): body.append(f"<div class=warn>model claims dropped: {E('; '.join(d['dropped_claims']))}</div>")
    if name == "web_search" and d:
        body.append(f"<div>hits {d.get('hits_searched')} · live pages read {d.get('live_pages_read')} · unreadable {d.get('unreadable_count')}</div>")
        if d.get("matches"): body.append("<div><b>same-story matches:</b> " + ", ".join(f"<a href='{E(u)}'>{E(u[:80])}</a>" for u in d["matches"]) + "</div>")
        if d.get("relocated_to"): body.append(f"<div><b>relocation:</b> {E(d['relocated_to'])} (old→new {pct(d.get('old_in_new'))}, new→old {pct(d.get('new_in_old'))})</div>")
        if d.get("dead_same_story"): body.append("<div class=warn><b>dead same-story copies:</b> " + "; ".join(f"{E(x.get('url'))} ({E(x.get('reason'))})" for x in d["dead_same_story"]) + "</div>")
        if d.get("related_same_publisher"): body.append(f"<div class=small>siblings set aside: {', '.join(E(u) for u in d['related_same_publisher'])}</div>")
    return f"<div class=stage><div class=stagehead>{E(TITLES.get(name, name))}</div><div class=stagestatus>{head}</div>{''.join(body)}</div>"


def card(r):
    resp = r.get("response") or {}
    stages = {s["stage"]: s for s in (resp.get("stages") or [])}
    verdict = resp.get("verdict") or "ERROR"
    flags = " ".join(f"<span class=flag>{E(f)}</span>" for f in (resp.get("flags") or []))
    times = r.get("stage_times_s") or {}
    tline = " · ".join(f"{k} {times[k]}s" for k in ORDER if k in times)
    blocks = "".join(stage_block(stages[k]) for k in ORDER if k in stages) if stages else f"<div class=warn>ERROR: {E(r.get('error') or '')}</div>"
    head = f"#{E(r.get('audit_id'))} <span class='v v-{E(verdict)}'>{E(verdict)}</span> {flags}"
    meta = (f"<div class=meta><a href='{E(r.get('url'))}'>{E(r.get('url'))}</a><br>{E(r.get('newspaper') or '')} · published {E(str(r.get('publish_date') or '?'))[:10]} · archive status {E(str(r.get('http_status')))} · {r.get('content_chars')} chars · {r.get('elapsed_s')}s ({E(tline)})</div>")
    body = f"{meta}<h3>{E(r.get('title'))}</h3>{blocks}<div class=findings><b>Findings:</b> {E(resp.get('findings') or '')}</div>"
    if not BATCH:
        return f"<section class=card id='a{E(r.get('audit_id'))}'><h2>{head}</h2>{body}</section>"
    pr, li = r.get("probe") or {}, r.get("list") or {}
    proj = projection(r)
    status = pr.get("status")
    extra = (f"<span class=small> · {E(li.get('list_state'))} in list → now {E(STATUS_NAMES.get(status, status))}"
             f" · {'tagged by a human' if pr.get('existing_tags') else 'no tags'} · {pr.get('discussions') or 0} discussion(s) · apply: {E(proj)}</span>")
    opened = " open" if verdict in ATTENTION or (resp.get("flags")) else ""
    return (f"<details class=card id='a{E(r.get('audit_id'))}'{opened}><summary><b>{head}</b> {E((r.get('title') or '')[:110])}{extra}</summary>{body}</details>")


def projection(r):
    """What apply_results.py would do under the standing rules, from the probe snapshot (re-checked live)."""
    pr = r.get("probe") or {}
    if not result_usable(r).write:
        return "nothing — failed audit, re-run"
    if pr.get("status") not in ELIGIBLE_STATUSES:
        return f"nothing — status {pr.get('status')} ineligible"
    tags = bool(r.get("classification")) and not pr.get("existing_tags")
    comment = (pr.get("discussions") == 0)
    if tags and comment:
        return "tags + comment"
    if tags:
        return "tags only (has discussions)"
    if comment:
        return "comment, IPTC proposed only" if pr.get("existing_tags") else "comment only (no classification)"
    return "nothing — has discussions" + (", tagged" if pr.get("existing_tags") else "")


def agreement_rows():
    """Human vs bot on audits humans had already tagged: topic overlap, genre, scope."""
    out = []
    for r in rows:
        pr = r.get("probe") or {}
        human = pr.get("subject_tag_notations") or []
        c = r.get("classification") or {}
        if not human or not c:
            continue
        bot = [t["notation"] for t in (c.get("subject_tags") or [])]
        inter = [n for n in human if n in bot]
        pa = PROBE.get(r["audit_id"], {})
        genre_h, scope_h = pa.get("content_type_notation"), pa.get("geographic_scope")
        genre_b, scope_b = (c.get("genre") or {}).get("notation"), c.get("geographic_scope")
        out.append({"audit_id": r["audit_id"], "human": human, "bot": bot, "inter": inter,
                    "overlap": len(inter) / len(human), "jaccard": len(inter) / len(set(human) | set(bot)) if (human or bot) else 0,
                    "top1_in_bot": human[0] in bot, "genre": (genre_h, genre_b, genre_h == genre_b if genre_h and genre_b else None),
                    "scope": (scope_h, scope_b, scope_h == scope_b if scope_h and scope_b else None)})
    return out


def xtab(rows_, key_fn, col_fn, key_title):
    keys = Counter(key_fn(r) for r in rows_)
    cols = [k for k, _ in Counter(col_fn(r) for r in rows_).most_common()]
    cells = Counter((key_fn(r), col_fn(r)) for r in rows_)
    h = "<table><tr><th>" + E(key_title) + "</th>" + "".join(f"<th>{E(c)}</th>" for c in cols) + "<th>total</th></tr>"
    for k, n in keys.most_common():
        h += f"<tr><td>{E(k)}</td>" + "".join(f"<td>{cells.get((k, c), '') or ''}</td>" for c in cols) + f"<td><b>{n}</b></td></tr>"
    h += "<tr><td><b>all</b></td>" + "".join(f"<td><b>{sum(v for (kk, cc), v in cells.items() if cc == c)}</b></td>" for c in cols) + f"<td><b>{len(rows_)}</b></td></tr></table>"
    return h


def overview():
    V = lambda r: (r.get("response") or {}).get("verdict") or "ERROR"
    pub = lambda r: (r.get("list") or {}).get("publisher") or r.get("newspaper") or "?"
    st = lambda r: STATUS_NAMES.get((r.get("probe") or {}).get("status"), str((r.get("probe") or {}).get("status")))
    proj = Counter(projection(r) for r in rows)
    agr = agreement_rows()
    parts = [f"<section class=card><h2>Batch overview — {len(rows)} audits run</h2>",
             "<h3>Verdict × publisher</h3>", xtab(rows, pub, V, "publisher"),
             "<h3>Verdict × audit status today</h3>", xtab(rows, st, V, "status"),
             f"<h3>Apply projection under the standing rules</h3><p class=small>Statuses {'/'.join(str(x) for x in sorted(ELIGIBLE_STATUSES))} only; tags only where none are set; the comment carries the bot's codes as PROPOSED ONLY when tags are not written; a comment only where there are no discussions; failed audits drive nothing. Computed from the probe snapshot — apply_results.py re-checks every audit live.</p>",
             "<table><tr><th>projected action</th><th>audits</th></tr>" + "".join(f"<tr><td>{E(k)}</td><td>{v}</td></tr>" for k, v in proj.most_common()) + "</table>"]
    if agr:
        mean_ov = sum(a["overlap"] for a in agr) / len(agr); mean_j = sum(a["jaccard"] for a in agr) / len(agr)
        top1 = sum(a["top1_in_bot"] for a in agr); exact = sum(1 for a in agr if set(a["human"]) == set(a["bot"]))
        g = [a["genre"][2] for a in agr if a["genre"][2] is not None]; sc = [a["scope"][2] for a in agr if a["scope"][2] is not None]
        parts += [f"<h3>Human vs bot tags — {len(agr)} audits a human had already tagged</h3>",
                  f"<div>Mean share of the human's topics that the bot also chose: <b>{mean_ov:.0%}</b> · mean Jaccard {mean_j:.2f} · the human's first topic among the bot's: {top1}/{len(agr)} · identical sets: {exact}"
                  + (f" · genre agrees {sum(g)}/{len(g)}" if g else "") + (f" · scope agrees {sum(sc)}/{len(sc)}" if sc else "") + "</div>",
                  "<table><tr><th>audit</th><th>human topics</th><th>bot topics (ranked)</th><th>shared</th><th>genre h / b</th><th>scope h / b</th></tr>"
                  + "".join(f"<tr><td><a href='#a{a['audit_id']}'>{a['audit_id']}</a></td><td>{E(', '.join(LABELS.get(n, n) for n in a['human']))}</td><td>{E(' › '.join(LABELS.get(n, n) for n in a['bot']))}</td><td>{len(a['inter'])}/{len(a['human'])}</td><td>{E(a['genre'][0] or '—')} / {E(a['genre'][1] or '—')}</td><td>{E(a['scope'][0] or '—')} / {E(a['scope'][1] or '—')}</td></tr>" for a in agr)
                  + "</table>"]
    att = [r for r in rows if V(r) in ATTENTION or (r.get("response") or {}).get("flags")]
    if att:
        parts += [f"<h3>Human attention — {len(att)} audits</h3><table><tr><th>audit</th><th>publisher</th><th>verdict</th><th>flags</th><th>title</th></tr>"
                  + "".join(f"<tr><td><a href='#a{r['audit_id']}'>{r['audit_id']}</a></td><td>{E(pub(r))}</td><td>{E(V(r))}</td><td>{E(' '.join((r.get('response') or {}).get('flags') or []))}</td><td>{E((r.get('title') or '')[:100])}</td></tr>" for r in att) + "</table>"]
    parts += ["<h3>Index</h3><table><tr><th>audit</th><th>publisher</th><th>list state → now</th><th>verdict</th><th>apply</th><th>s</th></tr>"
              + "".join(f"<tr><td><a href='#a{r['audit_id']}'>{r['audit_id']}</a></td><td>{E(pub(r))}</td><td>{E((r.get('list') or {}).get('list_state'))} → {E(st(r))}</td><td class='v-{E(V(r))}'>{E(V(r))}</td><td>{E(projection(r))}</td><td>{r.get('elapsed_s')}</td></tr>" for r in rows) + "</table></section>"]
    return "".join(parts)


verdicts = Counter(((r.get("response") or {}).get("verdict") or "ERROR") for r in rows)
flag_n = sum(1 for r in rows if (r.get("response") or {}).get("flags"))
step_med = {k: statistics.median([r["stage_times_s"][k] for r in rows if k in (r.get("stage_times_s") or {})] or [0]) for k in ORDER}
cls_n = sum(1 for r in rows if r.get("classification"))
tag_freq = Counter(t.get("label") for r in rows for t in ((r.get("classification") or {}).get("subject_tags") or []))
genre_freq = Counter(((r.get("classification") or {}).get("genre") or {}).get("label") for r in rows if r.get("classification"))
scope_freq = Counter((r.get("classification") or {}).get("geographic_scope") for r in rows if r.get("classification"))
src_status = Counter(((s.get("details") or {}).get("source_status")) for r in rows for s in ((r.get("response") or {}).get("stages") or []) if s["stage"] == "source")
self_status = Counter(((s.get("details") or {}).get("self_status")) for r in rows for s in ((r.get("response") or {}).get("stages") or []) if s["stage"] == "source")

summary = (f"<section class=card><h2>Roll-up — {len(rows)} cases</h2>"
           f"<div>Verdicts: {', '.join(f'{k} {v}' for k, v in verdicts.most_common())}</div>"
           f"<div>Red flags (SOURCE_DEAD): {flag_n}</div>"
           f"<div>Classified: {cls_n}/{len(rows)} · genres: {E(dict(genre_freq))} · scopes: {E(dict(scope_freq))}</div>"
           f"<div>Top topics: {', '.join(f'{E(k)} ×{v}' for k, v in tag_freq.most_common(10))}</div>"
           f"<div>Source step — itself: {E(dict(self_status))} · cited: {E(dict(src_status))}</div>"
           f"<div>Median step time (s): {', '.join(f'{k} {v:.1f}' for k, v in step_med.items())}</div>"
           f"<div class=small>config: {E(json.dumps(data.get('config', {})))}</div></section>")

CSS = """details.card>summary{cursor:pointer;list-style:none}details.card>summary::-webkit-details-marker{display:none}details.card>summary::before{content:'▸ ';color:#888}details[open].card>summary::before{content:'▾ '}
h2.pub{margin-top:36px;border-bottom:2px solid #ddd;padding-bottom:4px}td.v-POSSIBLE_CENSORSHIP{background:#ffd6d6}td.v-NOT_REMOVED{background:#e0f0ff}td.v-MOVED,td.v-LIKELY_MOVED{background:#e3ffe3}td.v-AUDIT_FAILED,td.v-ERROR{background:#ffe9c9}
.whatdo{font-style:italic;color:#666;font-size:12px;margin:2px 0 4px}ul.what{margin:4px 0 8px 0;padding-left:18px;font-size:13px}ul.what li{margin:2px 0}
body{font:14px/1.45 system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}
.card{border:1px solid #ddd;border-radius:8px;padding:14px 18px;margin:16px 0;background:#fff}
.stage{border-left:3px solid #ccc;padding:6px 12px;margin:10px 0}.stagehead{font-weight:600}.stagestatus{color:#444;margin:2px 0 6px}
.meta,.small{color:#666;font-size:12px}.warn{color:#a40000}.findings{margin-top:10px;padding:8px;background:#f6f6f6;border-radius:6px}
table{border-collapse:collapse;font-size:12px;margin:6px 0}td,th{border:1px solid #ddd;padding:3px 6px;text-align:left}
.v{font-size:12px;padding:2px 8px;border-radius:10px;background:#eee;margin-left:8px}.v-POSSIBLE_CENSORSHIP{background:#ffd6d6}
.v-NOT_REMOVED{background:#e0f0ff}.v-MOVED,.v-LIKELY_MOVED{background:#e3ffe3}.v-FOUND_ONLINE{background:#eaffea}.v-LIKELY_LOW_PUBLIC_INTEREST{background:#f0f0f0}
.flag{font-size:11px;background:#a40000;color:#fff;padding:2px 6px;border-radius:8px;margin-left:6px}code{font-size:12px}"""
generated_at = data.get("generated_at") or ""
if BATCH:
    groups = {}
    for r in rows:
        groups.setdefault((r.get("list") or {}).get("publisher") or r.get("newspaper") or "?", []).append(r)
    cards_html = "".join(f"<h2 class=pub id='p{i}'>{E(k)} — {len(v)} audits</h2>" + "".join(card(r) for r in v) for i, (k, v) in enumerate(groups.items()))
    title = f"Step-wise audit batch — {E(RESULTS.parent.name)}"
    body_html = overview() + summary + cards_html
else:
    title = f"Step-wise audit run — {E(RESULTS.stem)}"
    body_html = summary + "".join(card(r) for r in rows)
html_out = f"<!doctype html><meta charset=utf-8><title>{title}</title><style>{CSS}</style><h1>{title} — {E(generated_at)}</h1>{body_html}"
RESULTS.with_suffix(".html").write_text(html_out)

md = [f"# Step-wise audit run — {len(rows)} cases ({generated_at[:19]})", "",
      f"Verdicts: {dict(verdicts)} · SOURCE_DEAD flags: {flag_n} · classified {cls_n}/{len(rows)}", ""]
if BATCH:
    V = lambda r: (r.get("response") or {}).get("verdict") or "ERROR"
    pubc = Counter(((r.get("list") or {}).get("publisher"), V(r)) for r in rows)
    md += ["## Batch overview", "", "| publisher | verdict | n |", "|---|---|---|"] + [f"| {k[0]} | {k[1]} | {v} |" for k, v in sorted(pubc.items())] + [""]
    md += ["| projected apply action | n |", "|---|---|"] + [f"| {k} | {v} |" for k, v in Counter(projection(r) for r in rows).most_common()] + [""]
    agr = agreement_rows()
    if agr:
        md += [f"Human-vs-bot tags on {len(agr)} pre-tagged audits: mean overlap {sum(a['overlap'] for a in agr)/len(agr):.0%}, identical sets {sum(1 for a in agr if set(a['human'])==set(a['bot']))}.", ""]
for r in rows:
    resp = r.get("response") or {}
    st = {s["stage"]: s for s in (resp.get("stages") or [])}
    c = r.get("classification") or {}
    md += [f"## #{r.get('audit_id')} — {resp.get('verdict') or 'ERROR'} {' '.join(str(f) for f in (resp.get('flags') or []))}", f"{r.get('title') or ''}  ", f"{r.get('url') or ''}  ", ""]
    md.append(f"1. **Classification** — tags: {' › '.join(str(t.get('label')) for t in (c.get('subject_tags') or [])) or '—'}; genre: {(c.get('genre') or {}).get('label') or '—'}; scope: {c.get('geographic_scope') or '—'}")
    if st.get("classification"):
        for line in EXPLAIN["classification"](st["classification"], LABELS):
            md.append(f"   - {line}")
    for k, n in (("similarity", 2), ("relevance", 3), ("source", 4), ("web_search", 5)):
        s = st.get(k)
        md.append(f"{n}. **{k}** — {'SKIPPED' if s and s.get('skipped') else (s.get('outcome') if s else 'n/a')}: {(s.get('explanation') or '') if s else ''}")
        if s:
            for line in EXPLAIN[k](s, LABELS):
                md.append(f"   - {line}")
    md += [f"   Findings: {resp.get('findings') or ''}", f"   time: {r.get('elapsed_s')}s {r.get('stage_times_s')}", ""]
    if not resp and r.get("error"):
        md += [f"   ERROR: {r['error']}", ""]
RESULTS.with_suffix(".md").write_text("\n".join(md))
print("wrote", RESULTS.with_suffix(".html"), "and", RESULTS.with_suffix(".md"))
