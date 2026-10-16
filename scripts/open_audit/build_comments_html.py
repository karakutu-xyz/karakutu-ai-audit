"""Build the QA page: one Markdown-formatted draft comment per processed open case.

Usage:  .venv311/bin/python scripts/open_audit/build_comments_html.py runs/<batch>/results.json
Input:  a results JSON (from run_open_cases.py), given as the first argument
Output: open_comments.html  — self-contained; per case: metadata bar, the comment EXACTLY as
        it would be posted (rendered), and the raw Markdown in a <details> for copy/QA.
        Also embeds machine-readable drafts: comments_draft.json (audit_id -> comment md).

NOTHING IS POSTED. This is the QA page an operator reviews before any POST /discussions.
"""
import html, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

if len(sys.argv) < 2:
    sys.exit("usage: build_comments_html.py <results.json>")
RESULTS = Path(sys.argv[1]).resolve()
OUT_HTML = RESULTS.with_name(RESULTS.stem.replace("results", "comments") + ".html")
OUT_JSON = RESULTS.with_name(RESULTS.stem.replace("results", "draft") + ".json")

VERDICT_LABEL = {
    "NOT_REMOVED": "the article is currently LIVE at its original URL — audit premise does not presently hold (possibly restored; human should re-check/close)",
    "MOVED": "measured republication — the original's text is contained in a live copy at a new URL",
    "LIKELY_MOVED": "the story appears to have been RELOCATED by the same publisher",
    "FOUND_ONLINE": "the story is still ONLINE elsewhere",
    "POSSIBLE_CENSORSHIP": "FLAGGED for human review",
    "INCONCLUSIVE": "IN SCOPE (public-interest screen passed) — but the web-evidence stage could not establish what happened to the story",
    "LIKELY_LOW_PUBLIC_INTEREST": "screened as likely OUT OF SCOPE (low public interest)",
    "AUDIT_FAILED": "the automated audit itself FAILED (do not read as a result)",
}
POSTABLE = {"NOT_REMOVED", "MOVED", "LIKELY_MOVED", "FOUND_ONLINE", "POSSIBLE_CENSORSHIP", "INCONCLUSIVE",
            "LIKELY_LOW_PUBLIC_INTEREST"}


# --- leads for the human reviewer -------------------------------------------------------
# The fetch layer logs every hit it could not turn into evidence and why. Those URLs are
# exactly what a human can check by hand (a bot-blocked page opens fine in a browser; a
# JS-shell page renders; a dead variant is context). Parse them out of the captured logs.
import unicodedata

_LEAD_RE = re.compile(r"web_fetch: (https?://\S+) (unreadable|DEAD|excluded) \((.+?)\)?$")

def _fold(text: str) -> set:
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return {w for w in re.findall(r"[a-z0-9]+", t) if len(w) >= 4 and not w.isdigit()}

_REASON_HUMAN = [
    (re.compile(r"HTTP 40[35].*blocked|HTTP 4\d\d — blocked|blocked or erroring"),
     "bot-blocked — likely opens fine in a browser"),
    (re.compile(r"200 but \d+ chars of prose"),
     "page loads but no article text could be extracted (likely JS-rendered) — open manually"),
    (re.compile(r"not HTML"), "not an HTML page"),
    (re.compile(r"unreachable"), "unreachable at audit time — retry manually"),
    (re.compile(r"non-article URL"), "not an article-shaped URL"),
    (re.compile(r"variant of the removed article"),
     "a URL variant of the removed article itself (not independent evidence)"),
]

def leads_for(r) -> list:
    """[(warn, url, note)] from the case's captured fetch logs; ⚠-first, deduped, capped by caller."""
    removed_tokens = _fold(r["url"]) | _fold(r.get("title") or "")
    findings = ((r.get("response") or {}).get("findings") or "")
    seen, out = set(), []
    for line in r.get("logs") or []:
        m = _LEAD_RE.search(line)
        if not m: continue
        url, kind, reason = m.groups()
        url = url.rstrip(").,")
        if url in seen or url in findings: continue   # already cited as evidence — not a lead
        seen.add(url)
        note = next((h for rx, h in _REASON_HUMAN if rx.search(reason)), reason)
        if kind == "DEAD": note = "returns HTTP 404/410 (dead) — " + ("kept out of evidence" if "variant" in reason else "see findings")
        shared = len(_fold(url) & removed_tokens)
        out.append((shared >= 3, url, note))
    out.sort(key=lambda x: not x[0])
    return out

def md_comment(r) -> str:
    resp = r.get("response") or {}
    v = resp.get("verdict", "ERROR")
    stages = {s["stage"]: s for s in resp.get("stages", [])}
    def stage_line(name, label):
        s = stages.get(name)
        if not s: return f"- {label}: —"
        if s.get("skipped"): return f"- {label}: skipped"
        mark = {"pass": "✅ pass", "fail": "❌ fail"}.get(s.get("outcome"), "◻ inconclusive")
        conf = s.get("confidence")
        return f"- {label}: {mark}" + (f" (confidence {conf:g})" if conf is not None else "")
    lines = [
        "### 🤖 karakutu-audit — automated pre-screen (TEST)",
        "",
        f"**Verdict: `{v}`** — {VERDICT_LABEL.get(v, v)}.",
        "",
        stage_line("relevance", "Public-interest screen"),
        stage_line("similarity", "Archive similarity"),
        stage_line("web_search", "Web evidence"),
        "",
        "**Findings**",
        "",
    ]
    findings = (resp.get("findings") or "").strip()
    lines += ["> " + ln if ln else ">" for ln in (findings or "(none)").splitlines()]
    leads = leads_for(r)
    if leads:
        lines += ["", "**Leads for the reviewer** — surfaced by search but not automatically verifiable:", ""]
        for warn, url, note in leads[:6]:
            prefix = "⚠ possibly the same story: " if warn else ""
            lines.append(f"- {prefix}{url} — {note}")
        if len(leads) > 6:
            lines.append(f"- (+{len(leads) - 6} more not shown)")
    expl = (stages.get("web_search") or {}).get("explanation") or ""
    if expl and not (stages.get("web_search") or {}).get("skipped"):
        lines += ["", "<details><summary>Model reasoning (web evidence stage)</summary>", "", expl.strip(), "", "</details>"]
    lines += ["", "---",
              f"_Automated first-pass by the karakutu-audit pipeline "
              f"(model: local gemma-4-12b, search: SearXNG + archive similarity). "
              f"This is **not** a determination of censorship — that decision belongs to a human auditor. "
              f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')}._"]
    return "\n".join(lines)

# --- minimal md -> html (headings, bold, code, blockquote, list, hr, details passthrough)
def md_to_html(md: str) -> str:
    """Two pitfalls this handles: (1) blockquote lines were sliced AFTER html-escaping, so "> x"
    would become "&gt; x"[2:] = "t; x"; the RAW line is sliced first. (2) the _italic_ regex would
    eat the underscores inside `CODE_SPANS` and URLs; code spans and URLs are placeholder-protected
    before any inline formatting."""
    def inline(text: str) -> str:
        import html as _html
        stash: list = []
        def keep(html_frag: str) -> str:
            stash.append(html_frag); return f"\x00{len(stash)-1}\x00"
        text = re.sub(r"`([^`]+)`", lambda m: keep(f"<code>{_html.escape(m.group(1), quote=False)}</code>"), text)
        text = re.sub(r"(https?://[^\s<>\"]+)",
                      lambda m: keep(f'<a href="{_html.escape(m.group(1))}" target="_blank" rel="noopener">{_html.escape(m.group(1), quote=False)}</a>'),
                      text)
        text = _html.escape(text, quote=False)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"(?<![\w\\])_([^_]+)_(?![\w])", r"<i>\1</i>", text)
        return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)

    out, in_q, in_ul = [], False, False
    for ln in md.splitlines():
        if ln.startswith("### "): out.append(f"<h3>{inline(ln[4:])}</h3>"); continue
        if ln.startswith("<details") or ln.startswith("</details") or ln.startswith("<summary") or "</summary>" in ln:
            out.append(ln); continue
        if ln.startswith("> ") or ln == ">":
            if not in_q: out.append("<blockquote>"); in_q = True
            out.append((inline(ln[2:]) if ln.startswith("> ") else "") + "<br>"); continue
        if in_q: out.append("</blockquote>"); in_q = False
        if ln.startswith("- "):
            if not in_ul: out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline(ln[2:])}</li>"); continue
        if in_ul: out.append("</ul>"); in_ul = False
        if ln.strip() == "---": out.append("<hr>"); continue
        if ln.strip() == "": out.append("<div class=sp></div>"); continue
        out.append(f"<p>{inline(ln)}</p>")
    if in_q: out.append("</blockquote>")
    if in_ul: out.append("</ul>")
    return "\n".join(out)

def main():
    data = json.loads(RESULTS.read_text())
    results = sorted(data["results"], key=lambda r: -(r.get("created_at") or 0))
    drafts, cards, counts = {}, [], {}
    for r in results:
        v = (r.get("response") or {}).get("verdict", "ERROR")
        counts[v] = counts.get(v, 0) + 1
        md = md_comment(r)
        postable = v in POSTABLE and r.get("error") is None
        drafts[str(r["audit_id"])] = {"comment": md, "verdict": v, "postable": postable,
                                       "chars": len(md), "url": r["url"]}
        badge = f"<span class='badge v-{v}'>{v}</span>"
        posty = "" if postable else "<span class='badge nopost'>DO NOT POST</span>"
        created = datetime.fromtimestamp(r.get("created_at") or 0, tz=timezone.utc).strftime("%Y-%m-%d")
        cards.append(f"""
<article class=card data-verdict="{v}">
 <header>#{r['audit_id']} {badge} {posty}
  <a href="{html.escape(r['url'])}" target="_blank" rel="noopener">{html.escape(r['title'][:110])}</a>
  <span class=meta>{html.escape(str(r.get('newspaper') or ''))} · audit created {created} · {r['elapsed_s']:.0f}s · comment {len(md)} chars</span>
 </header>
 <div class=comment>{md_to_html(md)}</div>
 <details><summary>raw markdown (what would be POSTed)</summary><pre>{html.escape(md)}</pre></details>
</article>""")
    OUT_JSON.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                                     "note": "DRAFTS ONLY — nothing posted", "drafts": drafts},
                                    indent=1, ensure_ascii=False))
    kpis = " ".join(f"<button class=fbtn data-v='{k}'>{k} <b>{n}</b></button>" for k, n in sorted(counts.items()))
    page = f"""<!doctype html><meta charset=utf-8><title>karakutu-audit — draft comments QA ({len(results)} open cases)</title>
<style>body{{font:15px/1.5 -apple-system,Segoe UI,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fafafa}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;margin:1rem 0;padding:1rem 1.2rem}}
.card header{{font-weight:600;margin-bottom:.6rem}} .card header a{{color:#0b57d0;text-decoration:none}}
.meta{{display:block;font-weight:400;color:#777;font-size:12.5px;margin-top:.15rem}}
.badge{{display:inline-block;font:600 11px/1.6 monospace;padding:0 .45em;border-radius:4px;background:#eee;margin-right:.3rem}}
.v-MOVED,.v-LIKELY_MOVED,.v-FOUND_ONLINE{{background:#d8efe4}} .v-NOT_REMOVED{{background:#fff3cd}} .v-POSSIBLE_CENSORSHIP{{background:#fde3d3}}
.v-INCONCLUSIVE{{background:#e8e8e8}} .v-LIKELY_LOW_PUBLIC_INTEREST{{background:#e5e9f8}}
.v-AUDIT_FAILED,.v-ERROR,.nopost{{background:#f8d7da}}
.comment{{border-left:3px solid #cbd; padding:.2rem 1rem;background:#fcfcff}}
.comment blockquote{{border-left:3px solid #bbb;margin:.4rem 0;padding:.2rem .8rem;color:#444;background:#f6f6f6}}
.comment .sp{{height:.35rem}} pre{{white-space:pre-wrap;background:#f4f4f4;padding:.8rem;border-radius:6px;font-size:12.5px}}
.fbtn{{font:13px monospace;margin:.15rem;padding:.25rem .6rem;border:1px solid #bbb;border-radius:6px;background:#fff;cursor:pointer}}
.fbtn.on{{background:#0b57d0;color:#fff}} h1{{font-size:1.4rem}}</style>
<h1>Draft discussion comments — {len(results)} most recent open audits</h1>
<p>QA view only. <b>Nothing has been posted.</b> Each card shows the comment exactly as it would go to
<code>POST /audits/{{audit_id}}/discussions</code> (limit 10,000 chars). Machine-readable drafts: <code>comments_draft.json</code>.</p>
<p>{kpis} <button class=fbtn id=all>show all</button></p>
<div id=cards>{''.join(cards)}</div>
<script>
document.querySelectorAll('.fbtn[data-v]').forEach(b=>b.onclick=()=>{{
  b.classList.toggle('on');
  const on=[...document.querySelectorAll('.fbtn.on')].map(x=>x.dataset.v);
  document.querySelectorAll('.card').forEach(c=>c.style.display=(!on.length||on.includes(c.dataset.verdict))?'':'none');
}});
document.getElementById('all').onclick=()=>{{document.querySelectorAll('.fbtn.on').forEach(b=>b.classList.remove('on'));document.querySelectorAll('.card').forEach(c=>c.style.display='');}};
</script>"""
    OUT_HTML.write_text(page)
    print(f"wrote {OUT_HTML.name} + {OUT_JSON.name}: {len(results)} cases, verdicts {counts}")

if __name__ == "__main__":
    main()
