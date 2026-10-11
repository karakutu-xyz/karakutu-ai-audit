"""results.json (run_open_cases.py, batch mode: records carry `list`/`probe`) -> a designed, theme-aware
self-contained BATCH report page (HTML). Overview first (verdict x publisher, verdict x status, apply
projection under the standing rules, human-vs-bot tags, human-attention list), then one collapsible
step-by-step trace per audit grouped by publisher (attention verdicts open). Same visual system as the
10-case run report (Archivo / Source Sans 3 / IBM Plex Mono, recorder-orange accent).

No doctype/html/head/body tags: the artifact host wraps the fragment; <title> + <style> go first.

Usage: .venv311/bin/python scripts/open_audit/build_batch_artifact.py <results.json> <out.html> "<Page Name>" "<lede sentence>"
"""
import html, json, statistics, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stepwise_explain import EXPLAIN, STEP_WHAT, load_labels  # noqa: E402
from apply_rules import ELIGIBLE_STATUSES, STATUS_NAMES, result_usable  # noqa: E402
from batch_summary import batch_clusters, iptc_histograms, outside_members, step4_stats  # noqa: E402

LABELS = load_labels()
RESULTS = Path(sys.argv[1]).resolve(); OUT = Path(sys.argv[2]).resolve()
TITLE = sys.argv[3] if len(sys.argv) > 3 else "Kara Kutu Batch Report"
LEDE = sys.argv[4] if len(sys.argv) > 4 else ""
LIVE_NOTE = sys.argv[5] if len(sys.argv) > 5 else ""   # optional: what a live dry run of the apply found (drift since the probe)
data = json.loads(RESULTS.read_text()); rows = data["results"]
PROBE = {}
pp = RESULTS.with_name("probe.json")
if pp.exists():
    PROBE = {int(k): v.get("audit") or {} for k, v in json.loads(pp.read_text()).get("audits", {}).items() if v.get("http") == 200}

ORDER = ["classification", "similarity", "relevance", "related_removals", "source", "web_search"]
STEP = {"classification": ("1", "Classification", "IPTC media topics · genre · geographic scope"),
        "similarity": ("2", "Find similar link", "archive similarity index → fetched → measured"),
        "relevance": ("3", "Out of scope?", "public-interest screen"),
        "related_removals": ("4", "Related removals", "same subject vanished within ±14 days? → pattern flag"),
        "source": ("5", "Find the source", "itself (premise probe) · cited outlet or agency"),
        "web_search": ("6", "Web search", "elsewhere · publisher's own site")}
VC = {"POSSIBLE_CENSORSHIP": "crit", "NOT_REMOVED": "info", "MOVED": "good", "LIKELY_MOVED": "good", "FOUND_ONLINE": "good",
      "LIKELY_LOW_PUBLIC_INTEREST": "mute", "INCONCLUSIVE": "mute", "AUDIT_FAILED": "crit", "ERROR": "crit"}
VT = {"POSSIBLE_CENSORSHIP": "review flag", "NOT_REMOVED": "live at its own URL today", "MOVED": "measured republication",
      "LIKELY_MOVED": "relocated, partial fit", "FOUND_ONLINE": "story survives elsewhere", "LIKELY_LOW_PUBLIC_INTEREST": "out of scope",
      "INCONCLUSIVE": "nothing established", "AUDIT_FAILED": "audit did not run", "ERROR": "runner error"}
ATTENTION = {"POSSIBLE_CENSORSHIP", "MOVED", "LIKELY_MOVED", "FOUND_ONLINE", "AUDIT_FAILED", "ERROR"}


def E(x): return html.escape("" if x is None else str(x))
def pct(x): return "—" if x is None else f"{x:.0%}"
def secs(x): return "—" if x is None else f"{x:.0f}s"
def chip(text, cls="mute"): return f'<span class="chip {cls}">{E(text)}</span>'
def V(r): return (r.get("response") or {}).get("verdict") or "ERROR"
def PUB(r): return (r.get("list") or {}).get("publisher") or r.get("newspaper") or "?"
def ST(r): return (r.get("probe") or {}).get("status")


def outcome_chip(s):
    if s.get("skipped"): return chip("skipped", "mute")
    return chip(s.get("outcome", ""), {"pass": "good"}.get(s.get("outcome"), "mute"))


def projection(r):
    pr = r.get("probe") or {}
    if not result_usable(r).write: return "nothing — failed audit, re-run"
    if pr.get("status") not in ELIGIBLE_STATUSES: return f"nothing — status {pr.get('status')} ({STATUS_NAMES.get(pr.get('status'), '?')}) is outside the rule"
    tags = bool(r.get("classification")) and not pr.get("existing_tags"); com = pr.get("discussions") == 0
    if tags and com: return "tags + comment"
    if tags: return "tags only (has discussions)"
    if com: return "comment, IPTC proposed only" if pr.get("existing_tags") else "comment only (no classification)"
    return "nothing — has discussions"


def step_body(name, s):
    d = s.get("details") or {}; out = []
    if name == "classification":
        tags = d.get("subject_tags") or []
        if tags: out.append('<ol class="tags">' + "".join(f'<li><code>{E(t["notation"])}</code><span>{E(t["label"])}</span></li>' for t in tags) + "</ol>")
        facts = []
        if d.get("genre"): facts.append(f'Genre: <b>{E(d["genre"]["label"])}</b> <code>{E(d["genre"]["notation"])}</code>')
        if d.get("geographic_scope"): facts.append(f'Geographic scope: <b>{E(d["geographic_scope"])}</b>')
        if facts: out.append('<p class="facts">' + " &nbsp;·&nbsp; ".join(facts) + "</p>")
    elif name == "similarity" and d.get("candidates"):
        out.append('<div class="tablewrap"><table><thead><tr><th>archive candidate</th><th>score</th><th>same publisher</th><th>status</th><th>old→new</th><th>new→old</th><th>fit</th></tr></thead><tbody>'
                   + "".join(f'<tr><td class="url"><a href="{E(c["url"])}">{E(c["url"])}</a></td><td class="num">{(c.get("score") or 0):.2f}</td><td>{"yes" if c.get("same_publisher") else ""}</td>'
                             f'<td>{chip(c.get("status"), "good" if c.get("status")=="live" else ("crit" if c.get("status")=="dead" else "mute"))}</td><td class="num">{pct(c.get("old_in_new"))}</td><td class="num">{pct(c.get("new_in_old"))}</td><td>{E(c.get("fit") or "")}</td></tr>' for c in d["candidates"]) + "</tbody></table></div>")
    elif name == "source" and d:
        if d.get("source_url"): out.append(f'<p class="facts">cited URL: <a href="{E(d["source_url"])}">{E(d["source_url"])}</a></p>')
        if d.get("extracted_phrases"): out.append('<p class="note">attribution phrases: ' + " | ".join(E(p_) for p_ in d["extracted_phrases"][:4]) + "</p>")
    elif name == "web_search" and d:
        if d.get("matches"): out.append('<p class="facts">same-story matches: ' + ", ".join(f'<a href="{E(u)}">{E(u[:80])}</a>' for u in d["matches"]) + "</p>")
        if d.get("relocated_to"): out.append(f'<p class="facts">relocation: <a href="{E(d["relocated_to"])}">{E(d["relocated_to"][:90])}</a> (old→new {pct(d.get("old_in_new"))}, new→old {pct(d.get("new_in_old"))})</p>')
        if d.get("dead_same_story"): out.append('<p class="note warn">gone: ' + "; ".join(f'{E(x.get("url"))} ({E(x.get("reason"))})' for x in d["dead_same_story"]) + "</p>")
        if d.get("related_same_publisher"): out.append('<p class="note">set aside as siblings: ' + ", ".join(f'<a href="{E(u)}">{E(u[:80])}</a>' for u in d["related_same_publisher"]) + "</p>")
    return "".join(out)


def trace(r):
    resp = r.get("response") or {}; stages = {s["stage"]: s for s in resp.get("stages", [])}; times = r.get("stage_times_s") or {}
    if not stages: return f'<p class="note warn">runner error: {E(r.get("error"))}</p>'
    items = []
    for k in ORDER:
        s = stages.get(k)
        if not s: continue
        n, title, sub = STEP[k]
        items.append(f'<li class="step"><div class="rail"><span class="n">{n}</span></div><div class="stepbody"><div class="stephead"><h4>{E(title)}</h4><span class="sub">{E(sub)}</span>{outcome_chip(s)}<span class="dur">{secs(times.get(k))}</span></div>'
                     f'<p class="what">{E(STEP_WHAT[k])}</p><ul class="happened">' + "".join(f"<li>{E(line)}</li>" for line in EXPLAIN[k](s, LABELS)) + f'</ul>{step_body(k, s)}</div></li>')
    return '<ol class="trace">' + "".join(items) + "</ol>"


def case(r):
    resp = r.get("response") or {}; v = V(r); pr = r.get("probe") or {}; li = r.get("list") or {}
    flags = "".join(chip(f, "crit") for f in (resp.get("flags") or []))
    opened = " open" if v in ATTENTION or resp.get("flags") else ""
    human = f'human tags: {E(", ".join(LABELS.get(n, n) for n in (pr.get("subject_tag_notations") or [])))} · ' if pr.get("existing_tags") else ""
    return (f'<details class="case" id="a{r["audit_id"]}"{opened}><summary><span class="caseid">audit {r["audit_id"]}</span>{chip(v, VC.get(v, "mute"))}{flags}<span class="ttl">{E(r.get("title"))}</span>'
            f'<span class="smeta">{E(STATUS_NAMES.get(ST(r), ST(r)))} · {E(projection(r))}</span></summary>'
            f'<div class="casebody"><p class="meta"><a href="{E(r["url"])}">{E(r["url"])}</a></p>'
            f'<p class="meta">published {E(str(r.get("publish_date") or "?")[:10])} · listed as {E(li.get("list_state"))}, now {E(STATUS_NAMES.get(ST(r), ST(r)))}'
            f'{" · decision " + E(pr.get("decision")) if pr.get("decision") else ""} · {human}{pr.get("discussions") or 0} discussion(s) · detection {E(li.get("detection_window"))} · {secs(r.get("elapsed_s"))}</p>'
            f'<div class="verdictline">{chip(v, VC.get(v, "mute"))}<span class="vt">{E(VT.get(v, ""))}</span></div>{trace(r)}'
            f'<div class="findings"><span class="lab">Findings</span><p>{E(resp.get("findings"))}</p></div></div></details>')


def xtab(key_fn, key_title):
    keys = Counter(key_fn(r) for r in rows); cols = [k for k, _ in Counter(V(r) for r in rows).most_common()]
    cells = Counter((key_fn(r), V(r)) for r in rows)
    h = f'<div class="tablewrap"><table><thead><tr><th>{E(key_title)}</th>' + "".join(f'<th>{E(c)}</th>' for c in cols) + "<th>total</th></tr></thead><tbody>"
    for k, n in keys.most_common():
        h += f'<tr><td>{E(k)}</td>' + "".join(f'<td class="num">{cells.get((k, c)) or ""}</td>' for c in cols) + f'<td class="num"><b>{n}</b></td></tr>'
    h += '<tr class="tot"><td>all</td>' + "".join(f'<td class="num"><b>{sum(v for (kk, cc), v in cells.items() if cc == c)}</b></td>' for c in cols) + f'<td class="num"><b>{len(rows)}</b></td></tr></tbody></table></div>'
    return h


def agreement():
    out = []
    for r in rows:
        pr = r.get("probe") or {}; human = pr.get("subject_tag_notations") or []; c = r.get("classification") or {}
        if not human or not c: continue
        bot = [t["notation"] for t in (c.get("subject_tags") or [])]; inter = [n for n in human if n in bot]
        pa = PROBE.get(r["audit_id"], {})
        out.append({"id": r["audit_id"], "human": human, "bot": bot, "inter": inter, "overlap": len(inter) / len(human), "top1": human[0] in bot,
                    "genre": (pa.get("content_type_notation"), (c.get("genre") or {}).get("notation")), "scope": (pa.get("geographic_scope"), c.get("geographic_scope"))})
    return out


verdicts = Counter(V(r) for r in rows); flagged = [r for r in rows if (r.get("response") or {}).get("flags")]
classified = [r for r in rows if r.get("classification")]
total_s = sum(r.get("elapsed_s") or 0 for r in rows); med = statistics.median(r.get("elapsed_s") or 0 for r in rows) if rows else 0
tag_freq = Counter(t["label"] for r in rows for t in ((r.get("classification") or {}).get("subject_tags") or []))
genre_freq = Counter(((r.get("classification") or {}).get("genre") or {}).get("label") for r in classified)
scope_freq = Counter((r.get("classification") or {}).get("geographic_scope") for r in classified)
proj = Counter(projection(r) for r in rows)
agr = agreement()
att = [r for r in rows if V(r) in ATTENTION or (r.get("response") or {}).get("flags")]
cfg = data.get("config", {})
bar = "".join(f'<span class="seg {VC.get(k, "mute")}" style="flex:{v}" title="{E(k)} ×{v}"></span>' for k, v in verdicts.most_common())
legend = "".join(f'<li>{chip(k, VC.get(k, "mute"))}<span class="num">{v}</span><span class="vt">{E(VT.get(k, ""))}</span></li>' for k, v in verdicts.most_common())
groups = {}
for r in rows: groups.setdefault(PUB(r), []).append(r)
groups = dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))

agr_html = ""
if agr:
    g_ok = [a for a in agr if a["genre"][0] and a["genre"][1]]; s_ok = [a for a in agr if a["scope"][0] and a["scope"][1]]
    agr_html = (f'<h2>Human vs bot — {len(agr)} audits a human had already tagged</h2>'
                f'<p class="lede">On average the bot chose <b>{sum(a["overlap"] for a in agr)/len(agr):.0%}</b> of the human\'s topics; the human\'s first topic is among the bot\'s in {sum(a["top1"] for a in agr)}/{len(agr)}; identical sets {sum(1 for a in agr if set(a["human"])==set(a["bot"]))}'
                + (f'; genre agrees {sum(1 for a in g_ok if a["genre"][0]==a["genre"][1])}/{len(g_ok)}' if g_ok else "") + (f'; scope agrees {sum(1 for a in s_ok if a["scope"][0]==a["scope"][1])}/{len(s_ok)}' if s_ok else "")
                + '. Humans tend to pick broader topics; the bot goes one level more specific. These audits get no tag write — their comment carries the bot\'s codes as PROPOSED ONLY.</p>'
                '<div class="tablewrap"><table><thead><tr><th>audit</th><th>human topics</th><th>bot topics (ranked)</th><th>shared</th><th>genre h / b</th><th>scope h / b</th></tr></thead><tbody>'
                + "".join(f'<tr><td><a href="#a{a["id"]}">{a["id"]}</a></td><td>{E(", ".join(LABELS.get(n, n) for n in a["human"]))}</td><td>{E(" › ".join(LABELS.get(n, n) for n in a["bot"]))}</td><td class="num">{len(a["inter"])}/{len(a["human"])}</td>'
                          f'<td>{E((a["genre"][0] or "—").replace("genre:", ""))} / {E((a["genre"][1] or "—").replace("genre:", ""))}</td><td>{E(a["scope"][0] or "—")} / {E(a["scope"][1] or "—")}</td></tr>' for a in agr) + "</tbody></table></div>")

# --- IPTC picture + subject clusters within the batch (step 4) ---------------------------------
H = iptc_histograms(rows)
iptc_html = (f'<h2>The IPTC picture — {H["classified"]} of {H["n"]} classified</h2>'
             '<div class="band"><div class="tile"><span class="lab">Topics (any rank)</span><div class="tablewrap"><table><tbody>'
             + "".join(f'<tr><td>{E(k)}</td><td class="num">{v}</td></tr>' for k, v in H["topics"].most_common(20)) + '</tbody></table></div></div>'
             '<div class="tile"><span class="lab">First topic (most central)</span><div class="tablewrap"><table><tbody>'
             + "".join(f'<tr><td>{E(k)}</td><td class="num">{v}</td></tr>' for k, v in H["first_topic"].most_common(20)) + '</tbody></table></div></div>'
             '<div class="tile"><span class="lab">Genres</span><div class="tablewrap"><table><tbody>'
             + "".join(f'<tr><td>{E(k)}</td><td class="num">{v}</td></tr>' for k, v in H["genres"].most_common()) + '</tbody></table></div>'
             '<span class="lab" style="margin-top:12px">Scopes</span><div class="tablewrap"><table><tbody>'
             + "".join(f'<tr><td>{E(k)}</td><td class="num">{v}</td></tr>' for k, v in H["scopes"].most_common()) + '</tbody></table></div></div></div>')
S4 = step4_stats(rows)
clusters, edges = batch_clusters(rows)
byid = {r["audit_id"]: r for r in rows}
om = outside_members(rows)
cluster_html = ""
if S4.get("ran"):
    cluster_html = (f'<h2>Step 4 across the batch — subject clusters</h2>'
                    f'<p class="lede">Step 4 ran on {S4["ran"]} audits: {S4["with_prefiltered"]} had candidates sharing key terms, {S4["with_confirmed"]} had at least one related removal confirmed by the model, '
                    f'{S4["flagged"]} carry the RELATED_REMOVALS flag, {S4["kept_in_scope"]} were kept in the audit by the flag after the scope screen said out of scope'
                    + (f', the model failed on {S4["model_failed"]}' if S4.get("model_failed") else "") + '. '
                    f'Linking every audit to the batch audits it was confirmed related to gives <b>{len(clusters)} clusters</b> covering {sum(len(c) for c in clusters)} audits; {len(rows) - sum(len(c) for c in clusters)} are linked to nothing in the batch.</p>')
    for i, comp in enumerate(clusters, 1):
        cluster_html += (f'<details class="case"{" open" if i <= 3 else ""}><summary><b>Cluster {i}</b> · {len(comp)} audits · {E(", ".join(sorted({(byid[a].get("list") or {}).get("publisher") or "" for a in comp})))}'
                         f' <span class="smeta">top topics: {E(", ".join(k for k, _ in Counter(t["label"] for a in comp for t in ((byid[a].get("classification") or {}).get("subject_tags") or [])).most_common(4)))}</span></summary>'
                         '<div class="casebody"><div class="tablewrap"><table><thead><tr><th>audit</th><th>verdict</th><th>flag</th><th>linked to</th><th>title</th></tr></thead><tbody>'
                         + "".join(f'<tr><td><a href="#a{a}">{a}</a></td><td>{chip(V(byid[a]), VC.get(V(byid[a]), "mute"))}</td><td>{"✓" if "RELATED_REMOVALS" in (byid[a].get("flags") or []) else ""}</td><td class="num">{len(edges.get(a, ()))}</td><td class="title">{E(byid[a]["title"])}</td></tr>' for a in comp)
                         + '</tbody></table></div></div></details>')
    if om:
        cluster_html += ('<h3>Confirmed related removals outside this batch</h3><div class="tablewrap"><table><thead><tr><th>publisher</th><th>title</th><th>human decision</th><th>linked from</th></tr></thead><tbody>'
                         + "".join(f'<tr><td>{E(n or "?")}</td><td>{E(t)}</td><td>{E(d or "—")}</td><td class="num">{k}</td></tr>' for (n, t, d), k in om.most_common(25)) + '</tbody></table></div>')

att_html = ""
if att:
    att_html = (f'<h2>Human attention — {len(att)} audits</h2><div class="tablewrap"><table><thead><tr><th>audit</th><th>publisher</th><th>verdict</th><th>flags</th><th>title</th><th>status</th></tr></thead><tbody>'
                + "".join(f'<tr><td><a href="#a{r["audit_id"]}">{r["audit_id"]}</a></td><td>{E(PUB(r))}</td><td>{chip(V(r), VC.get(V(r), "mute"))}</td><td>{"".join(chip(f, "crit") for f in ((r.get("response") or {}).get("flags") or []))}</td><td class="title">{E(r.get("title"))}</td><td>{E(STATUS_NAMES.get(ST(r), ST(r)))}</td></tr>' for r in att) + "</tbody></table></div>")

CSS = """
:root{--bg:#F2F4F6;--surface:#FFFFFF;--surface2:#E9EDF1;--ink:#171B21;--ink2:#4A5560;--ink3:#79848F;--line:#D5DBE1;
--accent:#D9530A;--accent-ink:#8C3505;--good:#1F7A4D;--good-bg:#DDF2E6;--crit:#B42318;--crit-bg:#FBE1DE;--info:#2B5FA6;--info-bg:#DDE8F7;--mute:#5B6570;--mute-bg:#E5E9ED;
--serif:"Archivo",system-ui,sans-serif;--sans:"Source Sans 3","Helvetica Neue",Arial,sans-serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#111417;--surface:#191E23;--surface2:#222930;--ink:#E8ECF0;--ink2:#B4BEC8;--ink3:#7F8A95;--line:#2E363E;
--accent:#F2793A;--accent-ink:#FFB088;--good:#5FD39A;--good-bg:#173626;--crit:#FF8A80;--crit-bg:#3E1A17;--info:#8BB4F0;--info-bg:#1A2A44;--mute:#AAB4BE;--mute-bg:#252C33;}}
:root[data-theme="dark"]{--bg:#111417;--surface:#191E23;--surface2:#222930;--ink:#E8ECF0;--ink2:#B4BEC8;--ink3:#7F8A95;--line:#2E363E;
--accent:#F2793A;--accent-ink:#FFB088;--good:#5FD39A;--good-bg:#173626;--crit:#FF8A80;--crit-bg:#3E1A17;--info:#8BB4F0;--info-bg:#1A2A44;--mute:#AAB4BE;--mute-bg:#252C33;}
body{background:var(--bg);color:var(--ink);font:16px/1.5 var(--sans);margin:0;}
a{color:var(--info);text-decoration:none;} a:hover{text-decoration:underline;} a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px;}
h1,h2,h3,h4{font-family:var(--serif);text-wrap:balance;margin:0;}
.masthead{border-bottom:3px solid var(--accent);padding-bottom:18px;margin-bottom:22px;display:grid;grid-template-columns:1fr auto;gap:12px 24px;align-items:end;}
.masthead h1{font-size:30px;font-weight:700;letter-spacing:-0.01em;}
.kicker{font-family:var(--mono);font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:var(--accent-ink);margin-bottom:6px;}
.when{font-family:var(--mono);font-size:12px;color:var(--ink3);text-align:right;}
.lede{max-width:72ch;color:var(--ink2);margin:0 0 22px;}
.band{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-bottom:10px;}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px 16px;}
.tile .lab,.findings .lab{font-family:var(--mono);font-size:11px;letter-spacing:0.08em;text-transform:uppercase;color:var(--ink3);display:block;margin-bottom:8px;}
.tile .big{font-family:var(--serif);font-size:26px;font-weight:600;font-variant-numeric:tabular-nums;}
.tile .small{font-size:14px;color:var(--ink2);}
.bar{display:flex;height:12px;border-radius:3px;overflow:hidden;gap:2px;margin:6px 0 10px;}
.seg{display:block;min-width:4px;} .seg.good{background:var(--good);} .seg.crit{background:var(--crit);} .seg.info{background:var(--info);} .seg.mute{background:var(--mute);}
ul.legend{list-style:none;margin:0;padding:0;display:grid;gap:6px;} ul.legend li{display:flex;align-items:center;gap:8px;font-size:13px;} ul.legend .vt{color:var(--ink3);}
.chip{display:inline-block;font-family:var(--mono);font-size:11px;letter-spacing:0.04em;padding:2px 8px;border-radius:3px;white-space:nowrap;}
.chip.good{color:var(--good);background:var(--good-bg);} .chip.crit{color:var(--crit);background:var(--crit-bg);} .chip.info{color:var(--info);background:var(--info-bg);} .chip.mute{color:var(--mute);background:var(--mute-bg);}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;}
h2{font-size:20px;margin:34px 0 10px;}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--surface);}
table{border-collapse:collapse;width:100%;font-size:14px;} th,td{text-align:left;padding:7px 10px;border-top:1px solid var(--line);vertical-align:top;} thead th{border-top:0;font-family:var(--mono);font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:var(--ink3);font-weight:500;}
tr.tot td{background:var(--surface2);} td.title{max-width:40ch;} td.url{font-family:var(--mono);font-size:12px;max-width:44ch;overflow-wrap:anywhere;}
h2.pub{margin-top:40px;border-bottom:1px solid var(--line);padding-bottom:6px;display:flex;align-items:baseline;gap:12px;} h2.pub .cnt{font-family:var(--mono);font-size:13px;color:var(--ink3);font-weight:400;}
.case{background:var(--surface);border:1px solid var(--line);border-radius:8px;margin:10px 0;}
.case>summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:auto auto 1fr auto;gap:6px 12px;align-items:center;padding:12px 16px;} .case>summary::-webkit-details-marker{display:none;}
.case>summary .ttl{font-family:var(--serif);font-weight:600;font-size:15px;min-width:0;overflow-wrap:anywhere;} .case>summary .smeta{font-size:12px;color:var(--ink3);white-space:nowrap;}
.caseid{font-family:var(--mono);font-size:12px;letter-spacing:0.06em;text-transform:uppercase;color:var(--accent-ink);}
.casebody{padding:4px 22px 20px;border-top:1px solid var(--line);}
.meta{margin:6px 0 0;color:var(--ink2);font-size:14px;overflow-wrap:anywhere;}
.verdictline{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0 4px;} .vt{color:var(--ink2);font-size:14px;}
ol.trace{list-style:none;margin:18px 0 0;padding:0;}
.step{display:grid;grid-template-columns:36px 1fr;gap:0 14px;}
.rail{position:relative;} .rail::before{content:"";position:absolute;left:17px;top:30px;bottom:-8px;width:2px;background:var(--line);} .step:last-child .rail::before{display:none;}
.rail .n{display:inline-flex;width:34px;height:34px;align-items:center;justify-content:center;border-radius:50%;border:2px solid var(--accent);color:var(--accent-ink);font-family:var(--serif);font-weight:700;font-size:14px;background:var(--surface);}
.stepbody{padding:5px 0 20px;min-width:0;}
.stephead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;} .stephead h4{font-size:16px;font-weight:600;} .stephead .sub{color:var(--ink3);font-size:13px;} .stephead .dur{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--ink3);}
.what{margin:4px 0 6px;font-size:13px;color:var(--ink3);max-width:80ch;font-style:italic;}
ul.happened{margin:0 0 10px;padding-left:18px;max-width:84ch;} ul.happened li{margin:3px 0;font-size:15px;}
ol.tags{list-style:none;margin:4px 0 8px;padding:0;display:flex;flex-wrap:wrap;gap:8px;counter-reset:tag;}
ol.tags li{display:flex;align-items:center;gap:6px;background:var(--surface2);border-radius:4px;padding:4px 8px 4px 6px;font-size:14px;counter-increment:tag;}
ol.tags li::before{content:counter(tag);font-family:var(--mono);font-size:11px;color:var(--accent-ink);}
code{font-family:var(--mono);font-size:12px;color:var(--ink2);}
.facts{margin:4px 0;font-size:14px;overflow-wrap:anywhere;} .note{margin:4px 0;font-size:13px;color:var(--ink3);overflow-wrap:anywhere;} .warn{color:var(--crit);}
.findings{margin-top:6px;background:var(--surface2);border-left:3px solid var(--accent);border-radius:0 6px 6px 0;padding:10px 14px;} .findings p{margin:4px 0 0;font-size:14px;}
.rules{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px 18px;margin:0 0 6px;font-size:14px;color:var(--ink2);} .rules b{color:var(--ink);}
.caveat{max-width:72ch;color:var(--ink2);font-size:14px;border-top:1px solid var(--line);padding-top:14px;margin-top:30px;}
@media (max-width:700px){.case>summary{grid-template-columns:1fr;} .masthead{grid-template-columns:1fr;} .when{text-align:left;}}
@media (prefers-reduced-motion: reduce){*{scroll-behavior:auto;}}
"""
elig = "/".join(str(x) for x in sorted(ELIGIBLE_STATUSES))
page = f'''<title>{E(TITLE)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=Source+Sans+3:ital,wght@0,400;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header class="masthead"><div><div class="kicker">karakutu-audit · five-step flow · batch run</div><h1>{E(TITLE)}</h1></div>
<div class="when">{E(data.get("generated_at", "")[:16].replace("T", " "))} UTC · {E(cfg.get("model"))} · search {E(cfg.get("search"))}</div></header>
<p class="lede">{E(LEDE)} Every trace below is what the pipeline recorded; verdicts are review signals for a human auditor, never findings of censorship.</p>
<div class="band">
<div class="tile"><span class="lab">Verdicts · {len(rows)} audits</span><div class="bar">{bar}</div><ul class="legend">{legend}</ul></div>
<div class="tile"><span class="lab">Red flags · SOURCE_DEAD</span><div class="big">{len(flagged)}</div><div class="small">the source an article itself credits answers 404/410</div>
<span class="lab" style="margin-top:14px">Classified</span><div class="big">{len(classified)}<span class="small"> / {len(rows)}</span></div><div class="small">genres {E(", ".join(f"{k} {v}" for k, v in genre_freq.most_common(4)))}<br>scopes {E(", ".join(f"{k} {v}" for k, v in scope_freq.most_common()))}</div></div>
<div class="tile"><span class="lab">Time</span><div class="big">{total_s/3600:.1f} h</div><div class="small">median {med:.0f}s per audit, sequential on the local model</div>
<span class="lab" style="margin-top:14px">Top topics</span><div class="small">{E(", ".join(f"{k} ×{v}" for k, v in tag_freq.most_common(8)))}</div></div>
</div>
<h2>Verdict × publisher</h2>{xtab(PUB, "publisher")}
<h2>Verdict × audit status today</h2>{xtab(lambda r: STATUS_NAMES.get(ST(r), str(ST(r))), "status")}
<h2>What the apply would do</h2>
<div class="rules">Standing rules: only audits in status <b>{elig}</b> are touched; IPTC tags are written only where <b>none</b> of the three tag fields is set; where they are not written the comment lists the bot's codes as <b>PROPOSED ONLY</b>; one comment per audit and only where there are <b>no discussions</b>; a failed audit drives nothing. Projected from the probe snapshot — the apply script re-checks every audit live and the archive drifts daily.</div>
<div class="tablewrap"><table><thead><tr><th>projected action (probe snapshot)</th><th>audits</th></tr></thead><tbody>{"".join(f'<tr><td>{E(k)}</td><td class="num">{v}</td></tr>' for k, v in proj.most_common())}</tbody></table></div>
{f'<div class="rules" style="margin-top:10px"><b>Live re-check:</b> {E(LIVE_NOTE)}</div>' if LIVE_NOTE else ""}
{iptc_html}
{cluster_html}
{att_html}
{agr_html}
<h2>Step-by-step traces, by publisher</h2>
<p class="lede">Click an audit to expand its five steps. Audits with a relocation, a review flag or a failure are expanded already.</p>
{"".join(f'<h2 class="pub">{E(k)}<span class="cnt">{len(v)} audits · {E(", ".join(f"{vv} {n}" for vv, n in Counter(V(r) for r in v).most_common()))}</span></h2>' + "".join(case(r) for r in v) for k, v in groups.items())}
<p class="caveat">Config: max_tokens {E(cfg.get("max_tokens"))}, request timeout {E(cfg.get("request_timeout"))}s, per-case cap {E(cfg.get("case_timeout"))}s, thinking {E(cfg.get("thinking"))}. Tag quality is only measured against the audits humans had already tagged (table above); source-finding recall and the SOURCE_DEAD false-positive rate remain unmeasured.</p>
</div>
'''
OUT.write_text(page); print("wrote", OUT, f"{len(page)/1e6:.1f} MB")
