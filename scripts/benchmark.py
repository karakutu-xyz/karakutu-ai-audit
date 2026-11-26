"""Run local benchmark cases; never creates live records or writes to Karakutu."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

import httpx
from dotenv import load_dotenv

from benchmark_cases import demo_cases, verdict_quality_cases
from benchmark_report import api_case, case_metadata, summarize


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument(
        "cases", nargs="?", type=Path, help="Ignored JSONL corpus"
    )
    sources.add_argument(
        "--demo", action="store_true", help="Two fictional smoke cases"
    )
    sources.add_argument("--set", choices=["verdict-quality"], dest="case_set")
    parser.add_argument("--base-url", default="http://127.0.0.1:2200")
    parser.add_argument(
        "--timeout",
        type=float,
        default=900,
        help="Maximum seconds to wait per run",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    if args.demo:
        cases, source = demo_cases(), "demo"
    elif args.case_set:
        cases, source = verdict_quality_cases(), "set:" + args.case_set
    else:
        cases = [
            json.loads(line)
            for line in args.cases.read_text().splitlines()
            if line.strip()
        ]
        source = "file"
    token = os.getenv("API_AUTH_TOKEN", "")
    headers = {"Authorization": "Bearer " + token} if token else {}
    report = {
        "category": "benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "runs": [],
        "cases": [],
    }
    folder = root / "data" / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (
        "benchmark-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + ".json"
    )
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=30,
    ) as client:
        for case in cases:
            response = client.post("/api/benchmarks/runs", json=api_case(case))
            response.raise_for_status()
            run = response.json()["run"]
            print(f"Submitted benchmark {run['id']}", flush=True)
            deadline = time.monotonic() + args.timeout
            while run["status"] in {"queued", "running"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Benchmark polling timed out")
                time.sleep(1)
                response = client.get("/api/runs/" + run["id"] + "/details")
                response.raise_for_status()
                run = response.json()
            report["runs"].append(run)
            report["cases"].append(case_metadata(case))
            report["summary"] = summarize(report["runs"], report["cases"])
            path.write_text(
                json.dumps(
                    report, ensure_ascii=False, indent=2, allow_nan=False
                )
                + "\n",
                encoding="utf-8",
            )
            print(
                f"{run['id']}: {run['status']} / {run.get('verdict')}",
                flush=True,
            )
    report["summary"] = summarize(report["runs"], report["cases"])
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    print(f"Saved local report: {path}")
    return int(report["summary"]["failed"] > 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
        print(
            f"Benchmark did not finish ({type(exc).__name__}); "
            "inspect service status and saved runs.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
