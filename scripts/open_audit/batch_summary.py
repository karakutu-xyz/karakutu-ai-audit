"""Batch-level view of a results.json: what the six steps said across the whole set — verdicts,
flags, the IPTC picture (topics / genres / scopes histograms, by publisher), and the SUBJECT
CLUSTERS step 4 found WITHIN the batch (connected components over "audit A's confirmed related
removals include audit B", restricted to audits in the batch). Pure functions + a CLI rundown.

Usage: .venv311/bin/python scripts/open_audit/batch_summary.py runs/<batch>/results.json [--top 15]
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text())["results"]


def stage(r, name):
    return next((s for s in (r.get("response") or {}).get("stages") or [] if s["stage"] == name), None)


def related_details(r):
    s = stage(r, "related_removals")
    return (s or {}).get("details") or {}


def iptc_histograms(rows):
    topics, genres, scopes = Counter(), Counter(), Counter()
    first = Counter()
    for r in rows:
        c = r.get("classification") or {}
        tags = c.get("subject_tags") or []
        for t in tags:
            topics[t["label"]] += 1
        if tags:
            first[tags[0]["label"]] += 1
        if c.get("genre"):
            genres[c["genre"]["label"]] += 1
        if c.get("geographic_scope"):
            scopes[c["geographic_scope"]] += 1
    return {"topics": topics, "first_topic": first, "genres": genres, "scopes": scopes,
            "classified": sum(1 for r in rows if r.get("classification")), "n": len(rows)}


def batch_clusters(rows):
    """Connected components of the 'confirmed related removal' relation restricted to the batch.
    Returns (clusters sorted by size desc, edges) where a cluster is a list of audit ids and
    edges maps audit_id -> set of batch audit ids it was confirmed related to."""
    ids = {r["audit_id"] for r in rows}
    edges = defaultdict(set)
    for r in rows:
        for m in related_details(r).get("members") or []:
            mid = int(m["audit_id"])
            if mid in ids and mid != r["audit_id"]:
                edges[r["audit_id"]].add(mid)
                edges[mid].add(r["audit_id"])
    seen, clusters = set(), []
    for aid in sorted(ids):
        if aid in seen or aid not in edges:
            continue
        comp, stack = [], [aid]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x); comp.append(x)
            stack.extend(edges[x] - seen)
        clusters.append(sorted(comp))
    clusters.sort(key=len, reverse=True)
    return clusters, edges


def outside_members(rows):
    """Confirmed related removals that are NOT in the batch (other publishers / earlier removals),
    counted by (newspaper, title)."""
    ids = {r["audit_id"] for r in rows}
    out = Counter()
    for r in rows:
        for m in related_details(r).get("members") or []:
            if int(m["audit_id"]) not in ids:
                out[(m.get("newspaper"), m.get("title", "")[:90], m.get("decision"))] += 1
    return out


def step4_stats(rows):
    c = Counter()
    for r in rows:
        d = related_details(r)
        s = stage(r, "related_removals") or {}
        if s.get("skipped"):
            c["skipped"] += 1; continue
        if not d.get("available"):
            c["unavailable"] += 1; continue
        c["ran"] += 1
        c["with_prefiltered"] += bool(d.get("prefiltered"))
        c["with_confirmed"] += bool(d.get("members"))
        c["flagged"] += bool(d.get("flag"))
        c["kept_in_scope"] += bool(d.get("kept_in_scope"))
        c["model_failed"] += bool(d.get("model_failed"))
    return c


def rundown(rows, top=15):
    out = []
    V = Counter(r["verdict"] for r in rows); F = Counter(f for r in rows for f in r.get("flags") or [])
    out.append(f"{len(rows)} audits · verdicts {dict(V.most_common())} · flags {dict(F)}")
    s4 = step4_stats(rows)
    out.append(f"step 4: ran {s4['ran']} · with candidates {s4['with_prefiltered']} · with ≥1 confirmed related removal {s4['with_confirmed']} · flagged {s4['flagged']} · kept in scope by the flag {s4['kept_in_scope']} · model failed {s4['model_failed']}")
    h = iptc_histograms(rows)
    out.append(f"IPTC: classified {h['classified']}/{h['n']} · genres {dict(h['genres'].most_common())} · scopes {dict(h['scopes'].most_common())}")
    out.append("top topics (any rank): " + ", ".join(f"{k} ×{v}" for k, v in h["topics"].most_common(top)))
    out.append("top FIRST topics: " + ", ".join(f"{k} ×{v}" for k, v in h["first_topic"].most_common(top)))
    clusters, edges = batch_clusters(rows)
    byid = {r["audit_id"]: r for r in rows}
    linked = sum(len(c) for c in clusters)
    out.append(f"clusters within the batch: {len(clusters)} (covering {linked} audits; {len(rows) - linked} audits linked to nothing in the batch)")
    for i, comp in enumerate(clusters, 1):
        titles = "; ".join(f"{a}: {byid[a]['title'][:60]}" for a in comp[:4]) + (" …" if len(comp) > 4 else "")
        out.append(f"  cluster {i} ({len(comp)}): {titles}")
    om = outside_members(rows)
    if om:
        out.append("related removals OUTSIDE the batch (other publishers / earlier): " + "; ".join(f"{n or '?'}: {t} [{d}] ×{k}" for (n, t, d), k in om.most_common(10)))
    return "\n".join(out)


if __name__ == "__main__":
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 15
    print(rundown(load(sys.argv[1]), top=top))
