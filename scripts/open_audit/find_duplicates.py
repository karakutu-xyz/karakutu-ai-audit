"""Which audits in a fixture are IN EFFECT the same article? (A publisher posts a headline-only stub,
later fills it, and the archive opens two audits on two URLs.) READ-ONLY, offline — uses the archived
bodies in cases.json.

Signals per pair: same numeric article id in the URL (t24: ",<id>" / "/<id>/"), title similarity
(folded token Jaccard), and body text containment both ways (5-token shingles, src/search/textmatch).
A pair is linked when: same id, or title Jaccard >= TITLE_MIN, or either containment >= BODY_MIN.
Links are grouped into clusters (connected components) and printed with the evidence.

Usage: .venv311/bin/python scripts/open_audit/find_duplicates.py runs/<b>/cases.json [--results runs/<b>/results.json] [--json out.json]
"""
import json, re, sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from src.search.fetch import prose_of  # noqa: E402
from src.search.textmatch import containment, shingles  # noqa: E402
from src.search.urls import same_article_variant  # noqa: E402

TITLE_MIN, BODY_MIN = 0.5, 0.5
args = sys.argv[1:]
cases = json.loads(Path(args[0]).read_text())["cases"]
results = {}
if "--results" in args:
    results = {r["audit_id"]: r for r in json.loads(Path(args[args.index("--results") + 1]).read_text())["results"]}
out_json = Path(args[args.index("--json") + 1]) if "--json" in args else None

FOLD = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iisSgGuUoOcC")


def url_id(u):
    m = re.search(r",(\d{4,})(?:[/?#]|$)", u) or re.search(r"/(\d{5,})(?:[/?,#-]|$)", u)
    return m.group(1) if m else None


def title_tokens(t):
    return set(re.findall(r"[a-z0-9]{3,}", (t or "").translate(FOLD).lower()))


docs = []
for c in cases:
    prose = prose_of(c.get("content") or "") or c.get("content") or ""
    docs.append({"audit_id": c["audit_id"], "title": c["title"], "url": c["url"], "publish_date": (c.get("publish_date") or "")[:10],
                 "chars": len(prose), "sh": shingles(prose), "tt": title_tokens(c["title"]), "id": url_id(c["url"])})
links = []
for a, b in combinations(docs, 2):
    same_id = (a["id"] and a["id"] == b["id"]) or same_article_variant(a["url"], b["url"])
    tj = len(a["tt"] & b["tt"]) / max(1, len(a["tt"] | b["tt"]))
    ab = containment(a["sh"], b["sh"]) if a["sh"] and b["sh"] else 0.0   # share of a's text present in b
    ba = containment(b["sh"], a["sh"]) if a["sh"] and b["sh"] else 0.0
    if same_id or tj >= TITLE_MIN or max(ab, ba) >= BODY_MIN:
        links.append({"a": a["audit_id"], "b": b["audit_id"], "same_id": bool(same_id), "title_jaccard": round(tj, 2),
                      "a_in_b": round(ab, 2), "b_in_a": round(ba, 2)})
adj = defaultdict(set)
for l in links:
    adj[l["a"]].add(l["b"]); adj[l["b"]].add(l["a"])
seen, clusters = set(), []
for d in docs:
    if d["audit_id"] in seen or d["audit_id"] not in adj:
        continue
    comp, stack = [], [d["audit_id"]]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x); comp.append(x); stack.extend(adj[x] - seen)
    clusters.append(sorted(comp))
byid = {d["audit_id"]: d for d in docs}
print(f"{len(docs)} audits · {len(links)} linked pairs · {len(clusters)} clusters covering {sum(len(c) for c in clusters)} audits")
for i, comp in enumerate(sorted(clusters, key=len, reverse=True), 1):
    print(f"\n== cluster {i} ({len(comp)})")
    for aid in comp:
        d = byid[aid]; r = results.get(aid) or {}
        print(f"   {aid} · {d['publish_date']} · {d['chars']:5d} chars · {r.get('verdict', '')} · {d['title'][:70]}\n        {d['url']}")
    for l in links:
        if l["a"] in comp and l["b"] in comp:
            why = ("same url id; " if l["same_id"] else "") + f"title {l['title_jaccard']:.0%}, body {l['a_in_b']:.0%}/{l['b_in_a']:.0%}"
            print(f"   link {l['a']}–{l['b']}: {why}")
if out_json:
    out_json.write_text(json.dumps({"links": links, "clusters": clusters}, ensure_ascii=False, indent=1))
    print("wrote", out_json)
