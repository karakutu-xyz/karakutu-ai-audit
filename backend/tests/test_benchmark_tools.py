"""Benchmark tools use generated Turkish cases and offline saved report dicts."""

from collections import Counter
import importlib.util
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
import pytest

from audit.domain import Verdict
from audit.schemas import RunInput
from audit.search.fetch import html_to_text


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
MARKER = "PRIVATE_FABRICATED_CONTENT_MARKER"


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))

    def load(name):
        spec = importlib.util.spec_from_file_location(
            name, SCRIPTS / (name + ".py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return {
        name: load(name)
        for name in (
            "benchmark_cases",
            "benchmark_report",
            "benchmark",
            "benchmark_compare",
        )
    }


def test_verdict_quality_set(scripts):
    module = scripts["benchmark_cases"]
    cases = module.verdict_quality_cases()
    assert len(cases) == 15
    for key in ("label",):
        assert len({c[key] for c in cases}) == len(cases)
    articles = [c["removed_article"] for c in cases]
    assert len({a["url"] for a in articles}) == len(cases)
    ids = [re.search(r"/haber/(\d+)/", a["url"])[1] for a in articles]
    assert len(set(ids)) == len(cases)
    patterns = Counter(c["pattern"] for c in cases)
    assert patterns == {
        "public-affairs-no-allegation": 4,
        "accountability-control": 3,
        "private-matter-influential": 3,
        "low-interest-control": 3,
        "html-wrapped": 2,
    }
    assert all(count >= 2 for count in patterns.values())
    for case in cases:
        assert case["label"].startswith("Fabricated / ")
        assert isinstance(case["expected_relevance"], bool)
        assert Verdict(case["expected_verdict"])
        payload = scripts["benchmark_report"].api_case(case)
        assert set(payload) == {"label", "expected_verdict", "removed_article"}
        RunInput.model_validate(payload)
        assert urlsplit(case["removed_article"]["url"]).hostname.endswith(
            (".invalid", ".example"),
        )
        content = case["removed_article"]["content"]
        if case["pattern"] == "html-wrapped":
            assert content.startswith("<html")
            assert (
                "<script>" in content
                and "<style>" in content
                and "<nav>" in content
            )
            assert content.index("<p>") >= 2200
            assert "<p>" not in content[:2000]
            assert re.search(r"<p>(.*?)</p>", content)[1].startswith(
                module.INVENTED
            )
            content = html_to_text(content)
        else:
            assert content.startswith(module.INVENTED)
        assert 600 <= len(content) <= 1500


@pytest.mark.parametrize(
    "outcome, status, expected",
    [
        ("pass", "completed", True),
        ("fail", "completed", False),
        ("inconclusive", "failed", None),
        ("pass", "failed", None),
        ("fail", "skipped", None),
        ("inconclusive", "completed", None),
    ],
)
def test_relevance_outcome(scripts, outcome, status, expected):
    run = {
        "steps": [
            {
                "name": "relevance",
                "status": status,
                "output": {"outcome": outcome},
            }
        ]
    }
    assert scripts["benchmark_report"].relevance_outcome(run) is expected


def test_missing_relevance(scripts):
    helper = scripts["benchmark_report"].relevance_outcome
    assert helper({}) is None
    assert (
        helper({"steps": [{"name": "source", "output": {"outcome": "pass"}}]})
        is None
    )


def run(
    label,
    verdict,
    *,
    workflow="before",
    date="2031-03-12T10:00:00Z",
    expected="INCONCLUSIVE",
    relevant="pass",
    status="succeeded",
):
    return {
        "label": label,
        "workflow_id": workflow,
        "created_at": date,
        "verdict": verdict,
        "expected_verdict": expected,
        "status": status,
        "steps": [
            {
                "name": "relevance",
                "status": "completed",
                "output": {"stage": "relevance", "outcome": relevant},
            }
        ],
        "input": {"removed_article": {"content": MARKER}},
    }


def report(runs, *, metadata=True):
    result = {"runs": runs}
    if metadata:
        result["cases"] = [
            {
                "label": r["label"],
                "pattern": "invented-pattern",
                "expected_verdict": r["expected_verdict"],
                "expected_relevance": True,
            }
            for r in runs
        ]
    return result


def write_report(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_compare_matches_labels_latest_and_change_markers(
    scripts, tmp_path, capsys
):
    before = report(
        [
            run("fixed", "AUDIT_FAILED", status="failed"),
            run("broke", "INCONCLUSIVE"),
            run("changed", "MOVED"),
            run("unchanged", "INCONCLUSIVE"),
            run("only before", "INCONCLUSIVE"),
            run("fixed", "INCONCLUSIVE", date="2031-03-11T10:00:00Z"),
        ]
    )
    after = report(
        [
            run("changed", "FOUND_ONLINE", workflow="after"),
            run("fixed", "INCONCLUSIVE", workflow="after"),
            run(
                "broke",
                "AUDIT_FAILED",
                workflow="after",
                relevant="fail",
                status="failed",
            ),
            run("unchanged", "INCONCLUSIVE", workflow="after"),
            run("only after", "INCONCLUSIVE", workflow="after"),
        ]
    )
    old_path = write_report(tmp_path, "old.json", before)
    new_path = write_report(tmp_path, "new.json", after)
    module = scripts["benchmark_compare"]
    assert module.main([str(old_path), str(new_path)]) == 0
    output = capsys.readouterr().out
    assert "Before workflow: before | After workflow: after" in output
    assert MARKER not in output
    rows = [
        line.split("|") for line in output.splitlines() if line.count("|") == 8
    ]
    assert [row[0].strip() for row in rows[1:]] == [
        "fixed",
        "broke",
        "changed",
        "unchanged",
        "only before",
        "only after",
    ]
    markers = {row[0].strip(): row[-1].strip() for row in rows[1:]}
    assert markers["fixed"] == "fixed"
    assert markers["broke"] == "broke"
    assert markers["changed"] == "changed"
    assert markers["unchanged"] == ""
    assert rows[1][3].strip() == "AUDIT_FAILED"
    assert rows[5][4].strip() == "-"
    assert rows[6][3].strip() == "-"
    assert "Before: total=5, matching expected verdict=3" in output
    assert "After: total=5, matching expected verdict=3" in output
    assert "matching expected relevance=4, failed runs=1" in output


def test_compare_falls_back_to_run_metadata(scripts, tmp_path, capsys):
    path = write_report(
        tmp_path,
        "legacy.json",
        report(
            [
                run("legacy", "INCONCLUSIVE", relevant="inconclusive"),
            ],
            metadata=False,
        ),
    )
    assert scripts["benchmark_compare"].main([str(path), str(path)]) == 0
    output = capsys.readouterr().out
    assert "unknown" in output
    assert "INCONCLUSIVE" in output
    assert "matching expected verdict=1" in output
    assert "matching expected relevance=0" in output


@pytest.mark.parametrize("side", ["before", "after"])
def test_mixed_workflows_require_selection(scripts, tmp_path, capsys, side):
    mixed = write_report(
        tmp_path,
        "mixed.json",
        report(
            [
                run("case", "INCONCLUSIVE", workflow="alpha"),
                run("case", "MOVED", workflow="beta"),
            ]
        ),
    )
    single = write_report(
        tmp_path, "single.json", report([run("case", "MOVED")])
    )
    paths = [mixed, single] if side == "before" else [single, mixed]
    assert scripts["benchmark_compare"].main([str(p) for p in paths]) == 2
    error = capsys.readouterr().err
    assert "alpha, beta" in error and f"--{side}-workflow" in error
    assert MARKER not in error


def test_same_file_two_workflows_and_markdown(scripts, tmp_path, capsys):
    path = write_report(
        tmp_path,
        "mixed.json",
        report(
            [
                run("case", "MOVED", workflow="alpha"),
                run("case", "INCONCLUSIVE", workflow="beta"),
            ]
        ),
    )
    before_files = set(tmp_path.iterdir())
    markdown = tmp_path / "chosen.md"
    assert (
        scripts["benchmark_compare"].main(
            [
                str(path),
                str(path),
                "--before-workflow",
                "alpha",
                "--after-workflow",
                "beta",
                "--markdown",
                str(markdown),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "fixed=1, broke=0" in output
    text = markdown.read_text()
    assert "| label | pattern |" in text and "| fixed |" in text
    assert MARKER not in output + text
    assert set(tmp_path.iterdir()) == before_files | {markdown}


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"runs": []},
        {"runs": [None]},
        {"runs": [{"label": "incomplete"}]},
        {"runs": [run("case", "MOVED", date="invalid")]},
        {"runs": [run("case", "MOVED", date=None)]},
        {
            "runs": [run("case", "MOVED")],
            "cases": [{"expected_relevance": "yes"}],
        },
    ],
)
def test_comparison_data_errors(scripts, tmp_path, capsys, value):
    path = write_report(tmp_path, "bad.json", value)
    assert scripts["benchmark_compare"].main([str(path), str(path)]) == 2
    assert "Benchmark comparison:" in capsys.readouterr().err


def test_unreadable_report_and_empty_filter(scripts, tmp_path, capsys):
    module = scripts["benchmark_compare"]
    missing = tmp_path / "missing.json"
    assert module.main([str(missing), str(missing)]) == 2
    assert "Cannot read" in capsys.readouterr().err
    path = write_report(tmp_path, "one.json", report([run("case", "MOVED")]))
    assert (
        module.main([str(path), str(path), "--before-workflow", "absent"]) == 2
    )
    assert "No runs left" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--demo", "--set", "verdict-quality"],
        ["cases.jsonl", "--demo"],
        ["cases.jsonl", "--set", "verdict-quality"],
        ["--set", "unknown"],
    ],
)
def test_benchmark_rejects_invalid_source_arguments(scripts, args):
    with pytest.raises(SystemExit) as error:
        scripts["benchmark"].parse_args(args)
    assert error.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["--demo"],
        ["--set", "verdict-quality"],
        ["cases.jsonl"],
    ],
)
def test_benchmark_accepts_exactly_one_source(scripts, args):
    assert scripts["benchmark"].parse_args(args)


@pytest.mark.parametrize("source", ["demo", "set:verdict-quality", "file"])
def test_benchmark_api_fields_report_and_summary(
    scripts,
    monkeypatch,
    tmp_path,
    capsys,
    source,
):
    module = scripts["benchmark"]
    # Keep reports, settings lookup and generated input entirely in the temp tree.
    monkeypatch.setattr(
        module, "__file__", str(tmp_path / "scripts" / "benchmark.py")
    )
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(module, "load_dotenv", lambda *a, **kw: None)
    submitted = []

    def handler(request):
        assert request.method == "POST"
        case = json.loads(request.content)
        submitted.append(case)
        RunInput.model_validate(case)
        return httpx.Response(
            200,
            json={
                **run(
                    case["label"],
                    case.get("expected_verdict", "INCONCLUSIVE"),
                    workflow="measured",
                    expected=case.get("expected_verdict"),
                    relevant="fail"
                    if case.get("expected_verdict")
                    == "LIKELY_LOW_PUBLIC_INTEREST"
                    else "pass",
                ),
                "id": str(len(submitted)),
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kw: real_client(
            **kw,
            transport=httpx.MockTransport(handler),
        ),
    )
    if source == "file":
        case = scripts["benchmark_cases"].verdict_quality_cases()[0]
        case.pop("pattern")
        case.pop("expected_relevance")
        path = tmp_path / "private-corpus.jsonl"
        path.write_text(json.dumps(case) + "\n")
        args = [str(path)]
    else:
        args = ["--demo"] if source == "demo" else ["--set", "verdict-quality"]
    assert module.main(args) == 0
    files = list((tmp_path / "data" / "reports").iterdir())
    assert len(files) == 1
    result = json.loads(files[0].read_text())
    assert result["source"] == source
    assert len(result["runs"]) == len(result["cases"]) == len(submitted)
    assert result["summary"]["workflow_ids"] == ["measured"]
    assert result["summary"]["failed"] == 0
    assert (
        result["summary"]["matching_expected"] == result["summary"]["labelled"]
    )
    if source == "set:verdict-quality":
        assert len(submitted) == 15
        assert result["summary"]["relevance_labelled"] == 15
        assert result["summary"]["relevance_matching"] == 15
        assert len(result["summary"]["by_pattern"]) == 5
        assert all(
            v["matching_expected"] == v["total"]
            for v in result["summary"]["by_pattern"].values()
        )
    else:
        assert all(
            case["pattern"] is None and case["expected_relevance"] is None
            for case in result["cases"]
        )
    if source == "demo":
        assert submitted == module.demo_cases()
    assert MARKER not in capsys.readouterr().out


def test_summary_unknown_relevance_never_matches(scripts):
    runs = [run("one", "INCONCLUSIVE", relevant="inconclusive")]
    summary = scripts["benchmark_report"].summarize(runs, report(runs)["cases"])
    assert summary["relevance_labelled"] == 1
    assert summary["relevance_matching"] == 0
