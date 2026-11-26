"""Pure projections of saved benchmark reports; never inspect article text."""

API_FIELDS = (
    "label",
    "expected_verdict",
    "removed_article",
    "similar_articles",
)
CASE_FIELDS = ("label", "pattern", "expected_verdict", "expected_relevance")


def api_case(case):
    """Local measurement metadata must not enter the strict API request."""
    return {key: case[key] for key in API_FIELDS if key in case}


def case_metadata(case):
    return {key: case.get(key) for key in CASE_FIELDS}


def relevance_outcome(run):
    """True/False for a completed relevance decision, None for every other state."""
    for step in run.get("steps", []):
        if step.get("name") == "relevance":
            output = step.get("output") or {}
            if step.get("status") in ("failed", "skipped") or output.get(
                "skipped"
            ):
                return None
            outcome = output.get("outcome")
            return {"pass": True, "fail": False}.get(outcome)
    return None


def summarize(runs, cases):
    labelled = [run for run in runs if run.get("expected_verdict")]
    relevance_labelled = relevance_matching = 0
    patterns = {}
    for run, case in zip(runs, cases, strict=True):
        expected = case.get("expected_relevance")
        if expected is not None:
            relevance_labelled += 1
            relevance_matching += relevance_outcome(run) is expected
        pattern = case.get("pattern")
        if pattern is not None:
            counts = patterns.setdefault(
                pattern, {"total": 0, "matching_expected": 0}
            )
            counts["total"] += 1
            counts["matching_expected"] += bool(
                case.get("expected_verdict")
                and run.get("verdict") == case["expected_verdict"]
            )
    return {
        "total": len(runs),
        "failed": sum(run["status"] == "failed" for run in runs),
        "labelled": len(labelled),
        "matching_expected": sum(
            run.get("verdict") == run["expected_verdict"] for run in labelled
        ),
        "workflow_ids": sorted({r["workflow_id"] for r in runs}),
        "relevance_labelled": relevance_labelled,
        "relevance_matching": relevance_matching,
        "by_pattern": patterns,
    }
