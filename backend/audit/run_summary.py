"""Deterministic, read-time explanations of saved execution records."""
from audit.provenance import STEPS
from audit.workflow.pipeline import STOPPED

VERDICT_TEXT = {
    "POSSIBLE_CENSORSHIP": ("Flagged for human review", "The audit found signals that need a person's judgment."),
    "NOT_REMOVED": ("Not removed", "The article is still live at its original address."),
    "MOVED": ("Moved", "The publisher has the same story at a new address."),
    "LIKELY_MOVED": ("Likely moved", "A similar story from the same publisher was found, but the match is partial."),
    "FOUND_ONLINE": ("Found online", "The story is still available from other outlets."),
    "INCONCLUSIVE": ("Inconclusive", "The audit could not establish what happened."),
    "LIKELY_LOW_PUBLIC_INTEREST": ("Likely low public interest", "The article looks like low public-interest content."),
    "AUDIT_FAILED": ("Audit failed", "The audit did not complete; this is not a result."),
}
FLAG_TEXT = {
    "SOURCE_DEAD": "The source the article cites no longer answers.",
    "RELATED_REMOVALS": "Other articles on the same subject disappeared around the same time.",
}


def summarize_run(run: dict) -> dict:
    active = run["status"] in {"queued", "running"}
    cancelled = run["status"] == "cancelled"
    verdict = None if cancelled else run.get("verdict")
    if run["status"] == "failed" and not verdict:
        verdict = "AUDIT_FAILED"
    label, headline = (("Audit in progress", "The audit is running.") if active else
                       VERDICT_TEXT.get(verdict, VERDICT_TEXT["INCONCLUSIVE"]))
    if cancelled:
        label = "Stopped by a person"
        headline = "A person stopped this audit before it finished. This is not a result."
    recorded = {step["name"]: step for step in run.get("steps", [])}
    stages = {} if cancelled else {
        stage["stage"]: stage for stage in (run.get("output") or {}).get("stages", [])
    }
    steps, next_running = [], run["status"] == "running"
    for spec in STEPS:
        record = recorded.get(spec["name"])
        stage = record.get("output", {}) if record else stages.get(spec["name"], {})
        status = record.get("status", "completed") if record else ("skipped" if stage.get("skipped") else "completed") if stage else "pending"
        interrupted = (record and status == "failed"
                       and (stage.get("details") or {}).get("error_type") == "CancelledError")
        if cancelled and interrupted:
            status = "stopped"
        if status == "pending" and next_running:
            status, next_running = "running", False
        line = stage.get("explanation")
        outcome = stage.get("outcome")
        if interrupted:
            # The step was cut off, not judged. Runs saved before this wording existed still
            # say the model failed; the story must not blame the model for a stop.
            line, outcome = f"Stage did not complete: {STOPPED}.", None
        if line:
            line = line.strip()
            if status == "skipped" and not line.lower().startswith("skipped"):
                line = "Skipped: " + line
            if len(line) > 500:
                line = line[:499] + "…"
        steps.append({"name": spec["name"], "title": spec["title"], "status": status,
                      "outcome": outcome, "line": line or None})
    flags = (run.get("output") or {}).get("flags", [])
    return {"verdict": verdict, "verdict_label": label, "headline": headline,
            "sentences": [] if cancelled else [text for flag, text in FLAG_TEXT.items() if flag in flags],
            "steps": steps, "failed_steps": [s["title"] for s in steps if s["status"] == "failed"],
            "skipped_steps": [s["title"] for s in steps if s["status"] == "skipped"],
            "human_review_note": None if active or cancelled or verdict == "AUDIT_FAILED" else
            "This is not a finding of censorship; a person decides."}
