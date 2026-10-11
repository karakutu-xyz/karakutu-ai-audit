"""Old-vs-new tag lists from reclassify.py: length, ancestor–descendant pairs, sibling pairs,
root-level fillers, and a per-audit side-by-side. READ-ONLY.
Usage: .venv311/bin/python scripts/open_audit/compare_tags.py runs/<b>/reclassify-<date>.json [--show 232821 ...]
"""
import json, sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from src.clients.concepts import ConceptCatalog  # noqa: E402

cat = ConceptCatalog.from_json(json.loads((REPO / ".cache" / "karakutu-concepts.json").read_text()))
data = json.loads(Path(sys.argv[1]).read_text())
show = [int(x) for x in sys.argv[sys.argv.index("--show") + 1:]] if "--show" in sys.argv else []


def paths(tags):
    return [(t["notation"], (cat.get(t["notation"]) or type("x", (), {"hierarchy_path": None, "parent_notation": None, "depth": 1})).hierarchy_path) for t in tags or []]


def metrics(lists):
    n = len(lists); lens = Counter(); anc = 0; anc_pairs = 0; sib = 0; roots = 0
    for tags in lists:
        tags = tags or []
        lens[len(tags)] += 1
        ps = paths(tags)
        pairs = sum(1 for i, (a, pa) in enumerate(ps) for j, (b, pb) in enumerate(ps) if i != j and pa and pb and pb.startswith(pa + "."))
        anc += pairs > 0; anc_pairs += pairs
        parents = Counter((cat.get(t["notation"]).parent_notation if cat.get(t["notation"]) else None) for t in tags)
        sib += any(v >= 2 for k, v in parents.items() if k)
        roots += any((cat.get(t["notation"]).depth if cat.get(t["notation"]) else 1) == 1 for t in tags) and len(tags) > 1
    return {"n": n, "mean_len": round(sum(k * v for k, v in lens.items()) / max(n, 1), 2), "len_dist": dict(sorted(lens.items())),
            "lists_with_ancestor_pair": anc, "ancestor_pairs": anc_pairs, "lists_with_siblings": sib, "lists_with_root_filler": roots}


old = [(r["old"] or {}).get("subject_tags") for r in data["rows"]]
new = [(r["new"] or {}).get("subject_tags") for r in data["rows"]]
print("OLD:", metrics(old)); print("NEW:", metrics(new))
cuts = Counter()
for r in data["rows"]:
    d = r.get("details") or {}
    cuts["collapsed"] += len(d.get("collapsed") or []); cuts["siblings_folded"] += len(d.get("siblings_folded") or [])
    cuts["roots_dropped"] += len(d.get("roots_dropped") or []); cuts["low_confidence"] += len(d.get("low_confidence_dropped") or [])
    cuts["model_gave_confidences"] += bool(d.get("low_confidence_dropped") is not None and any(True for _ in [0]))
print("code-rule cuts across the sample:", dict(cuts))
same_genre = sum(1 for r in data["rows"] if ((r["old"] or {}).get("genre") or {}).get("notation") == ((r["new"] or {}).get("genre") or {}).get("notation"))
first_same = sum(1 for r in data["rows"] if ((r["old"] or {}).get("subject_tags") or [{}])[0].get("notation") == ((r["new"] or {}).get("subject_tags") or [{}])[0].get("notation"))
print(f"genre unchanged {same_genre}/{len(data['rows'])} · first topic unchanged {first_same}/{len(data['rows'])}")
for r in data["rows"]:
    if show and r["audit_id"] not in show:
        continue
    print(f"\n{r['audit_id']} · {r['title'][:70]}")
    print("  old:", " › ".join(t["label"] for t in (r["old"] or {}).get("subject_tags") or []))
    print("  new:", " › ".join(t["label"] for t in (r["new"] or {}).get("subject_tags") or []), "|", ((r["new"] or {}).get("genre") or {}).get("label"), "|", (r["new"] or {}).get("geographic_scope"))
    d = r.get("details") or {}
    notes = []
    if d.get("collapsed"): notes.append("folded " + ", ".join(f"{cat.label(x['dropped'])}⊂{cat.label(x['covered_by'])}" for x in d["collapsed"]))
    if d.get("siblings_folded"): notes.append("siblings→parent " + ", ".join(cat.label(x["parent"]) for x in d["siblings_folded"]))
    if d.get("roots_dropped"): notes.append("root filler dropped " + ", ".join(cat.label(x) for x in d["roots_dropped"]))
    if d.get("low_confidence_dropped"): notes.append("low confidence " + ", ".join(f"{cat.label(x['notation'])} {x['confidence']:.2f}" for x in d["low_confidence_dropped"]))
    if notes: print("  rules:", "; ".join(notes))
