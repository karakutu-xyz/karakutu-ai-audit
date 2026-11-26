"""Compare saved benchmark reports offline, by label and workflow."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from benchmark_report import relevance_outcome


class ReportError(ValueError):
    """A safe diagnostic about report structure, never article payloads."""


def load_report(path):
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReportError("Cannot read a valid benchmark report") from None
    if not isinstance(report, dict) or not isinstance(report.get("runs"), list):
        raise ReportError("Report must contain a runs list")
    cases = report.get("cases", [])
    if not isinstance(cases, list) or any(
        not isinstance(c, dict) for c in cases
    ):
        raise ReportError("Report cases must be a list of objects")
    for case in cases:
        validate_metadata(case)
    for run in report["runs"]:
        if not isinstance(run, dict):
            raise ReportError("Report runs must be objects")
        for key in ("label", "workflow_id", "created_at", "verdict", "status"):
            value = run.get(key)
            if value is not None and not isinstance(value, str):
                raise ReportError("Run identifiers and outcomes must be text")
        if not run.get("label") or not run.get("workflow_id"):
            raise ReportError("Every run needs a label and workflow_id")
        validate_metadata(run)
        steps = run.get("steps", [])
        if not isinstance(steps, list) or any(
            not isinstance(s, dict)
            or not isinstance(s.get("output") or {}, dict)
            for s in steps
        ):
            raise ReportError(
                "Run steps must contain objects with object outputs"
            )
        for step in steps:
            outcome = (step.get("output") or {}).get("outcome")
            if outcome is not None and not isinstance(outcome, str):
                raise ReportError("Step outcomes must be text")
    return report


def validate_metadata(case):
    for key in ("label", "pattern", "expected_verdict"):
        if case.get(key) is not None and not isinstance(case[key], str):
            raise ReportError("Case labels, patterns and verdicts must be text")
    value = case.get("expected_relevance")
    if value is not None and not isinstance(value, bool):
        raise ReportError("Expected relevance must be a boolean or null")


def created_at(run):
    try:
        value = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError
        return value.timestamp()
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise ReportError(
            "Every run needs an ISO created_at with timezone"
        ) from None


def select_runs(report, workflow, side):
    ids = sorted({run["workflow_id"] for run in report["runs"]})
    if workflow is None and len(ids) > 1:
        raise ReportError(
            f"{side} report contains workflow ids: {', '.join(ids)}; "
            f"choose --{side}-workflow",
        )
    selected = workflow or (ids[0] if ids else None)
    latest = {}
    metadata = {}
    cases = report.get("cases", [])
    # Dict insertion order preserves the first occurrence of each label.
    for index, run in enumerate(report["runs"]):
        if run["workflow_id"] != selected:
            continue
        label = run["label"]
        stamp = created_at(run)
        if label not in latest or stamp >= created_at(latest[label]):
            latest[label] = run
            # Reports append case metadata alongside each run, including repeats.
            case = cases[index] if index < len(cases) else {}
            if case.get("label") != label:
                case = {}
            metadata[label] = {
                key: case.get(key, run.get(key))
                for key in ("pattern", "expected_verdict", "expected_relevance")
            }
    if not latest:
        raise ReportError(f"No runs left in {side} after workflow selection")
    return selected, latest, metadata


def change_marker(before, after, expected):
    if before == after:
        return ""
    if expected is not None:
        if after == expected:
            return "fixed"
        if before == expected:
            return "broke"
    return "changed"


def display(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "pass" if value else "fail"
    return str(value).replace("\n", " ").replace("\r", " ").replace("\t", " ")


def run_relevance(run):
    """Distinguish an unknown outcome from an entirely missing run."""
    if run is None:
        return None
    outcome = relevance_outcome(run)
    return "unknown" if outcome is None else outcome


def side_summary(runs, metadata):
    verdict_matching = relevance_matching = 0
    for label, run in runs.items():
        expected = metadata[label]
        verdict_matching += bool(
            expected["expected_verdict"]
            and run.get("verdict") == expected["expected_verdict"]
        )
        relevance_matching += (
            expected["expected_relevance"] is not None
            and relevance_outcome(run) is expected["expected_relevance"]
        )
    failed = sum(run.get("status") == "failed" for run in runs.values())
    return (
        f"total={len(runs)}, matching expected verdict={verdict_matching}, "
        f"matching expected relevance={relevance_matching}, failed runs={failed}"
    )


def compare(before, after):
    before_id, old, old_meta = before
    after_id, new, new_meta = after
    columns = [
        "label",
        "pattern",
        "expected verdict",
        "before verdict",
        "after verdict",
        "expected relevance",
        "before relevance",
        "after relevance",
        "change",
    ]
    rows = []
    metadata = {**new_meta, **old_meta}
    conflicts = [
        label for label in old if label in new and old_meta[label] != new_meta[label]
    ]
    for label in dict.fromkeys([*old, *new]):
        expected = metadata[label]
        old_verdict = old.get(label, {}).get("verdict")
        new_verdict = new.get(label, {}).get("verdict")
        if label not in old:
            marker = "added"
        elif label not in new:
            marker = "missing"
        else:
            marker = change_marker(
                old_verdict, new_verdict, expected["expected_verdict"]
            )
        rows.append(
            [
                label,
                expected["pattern"],
                expected["expected_verdict"],
                old_verdict,
                new_verdict,
                expected["expected_relevance"],
                run_relevance(old.get(label)),
                run_relevance(new.get(label)),
                marker,
            ]
        )
    fixed = sum(row[-1] == "fixed" for row in rows)
    broke = sum(row[-1] == "broke" for row in rows)
    added = sum(row[-1] == "added" for row in rows)
    missing = sum(row[-1] == "missing" for row in rows)
    header = f"Before workflow: {before_id} | After workflow: {after_id}"
    summary = [
        *(
            f"Warning: metadata conflict for label {display(label)}; "
            "using BEFORE metadata"
            for label in conflicts
        ),
        "Before: " + side_summary(old, metadata),
        "After: " + side_summary(new, metadata),
        f"Changes: fixed={fixed}, broke={broke}, added={added}, "
        f"missing={missing}, metadata_conflicts={len(conflicts)}",
    ]
    return header, columns, [[display(v) for v in row] for row in rows], summary


def render(comparison, *, markdown=False):
    header, columns, rows, summary = comparison
    if markdown:

        def line(row):
            return "| " + " | ".join(v.replace("|", "\\|") for v in row) + " |"

        table = [line(columns), line(["---"] * len(columns))]
        table.extend(line(row) for row in rows)
    else:
        widths = [
            max(len(row[i]) for row in [columns, *rows])
            for i in range(len(columns))
        ]
        table = [
            " | ".join(value.ljust(width) for value, width in zip(row, widths))
            for row in [columns, *rows]
        ]
    return "\n".join([header, "", *table, "", *summary]) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--before-workflow")
    parser.add_argument("--after-workflow")
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    try:
        comparison = compare(
            select_runs(
                load_report(args.before), args.before_workflow, "before"
            ),
            select_runs(load_report(args.after), args.after_workflow, "after"),
        )
        if args.markdown:
            args.markdown.write_text(
                render(comparison, markdown=True), encoding="utf-8"
            )
    except ReportError as exc:
        print(f"Benchmark comparison: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "Benchmark comparison: cannot write Markdown output",
            file=sys.stderr,
        )
        return 2
    print(render(comparison), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
