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


def demo_cases():
    """Generate fictional text in memory; no recorded article fixtures are checked in."""
    return [
        {"label": "Fictional smoke / low public interest", "expected_verdict": "LIKELY_LOW_PUBLIC_INTEREST",
         "removed_article": {"url": "https://example.invalid/synthetic-weekend-colors",
                             "title": "Fictional lifestyle exercise: choosing weekend colors",
                             "content": "This is an invented benchmark, not a real news article. "
                             "A fictional lifestyle column offers lighthearted suggestions about "
                             "matching socks with a favorite sweater and choosing flowers for a table. "
                             "Readers are encouraged to pick the colors they personally enjoy. "
                             "There is no allegation, public authority, policy, safety issue or political event. "
                             "The column closes with a playful suggestion to try a blue scarf next weekend."}},
        {"label": "Fictional smoke / public-interest investigation",
         "removed_article": {"url": "https://example.invalid/synthetic-procurement-investigation",
                             "title": "Council opens inquiry into missing water-treatment procurement records",
                             "content": "The municipal council has opened an inquiry into alleged conflicts "
                             "of interest in its water-treatment procurement process. Inspectors requested "
                             "tender records after finding payments for work without completion certificates. "
                             "The procurement committee has suspended new payments pending independent review. "
                             "Residents have asked the council to publish the contract and the water-quality "
                             "monitoring reports. Council members say the inquiry will examine public spending, "
                             "contractor selection and whether inspection requirements were followed. "
                             "The council has asked all departments to preserve relevant records."}},
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="?", type=Path, help="Ignored JSONL corpus")
    parser.add_argument("--demo", action="store_true", help="Generate two fictional smoke cases")
    parser.add_argument("--base-url", default="http://127.0.0.1:2200")
    parser.add_argument("--timeout", type=float, default=900, help="Maximum seconds to wait per run")
    args = parser.parse_args()
    if bool(args.cases) == args.demo:
        parser.error("Choose a cases file or --demo")
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    if args.demo:
        cases = demo_cases()
    else:
        cases = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
    token = os.getenv("API_AUTH_TOKEN", "")
    headers = {"Authorization": "Bearer " + token} if token else {}
    report = {"category": "benchmark", "created_at": datetime.now(timezone.utc).isoformat(), "runs": []}
    folder = root / "data" / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("benchmark-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    with httpx.Client(base_url=args.base_url.rstrip("/"), headers=headers, timeout=30) as client:
        for case in cases:
            response = client.post("/api/benchmarks/runs", json=case)
            response.raise_for_status()
            run = response.json()
            print(f"Submitted benchmark {run['id']}", flush=True)
            deadline = time.monotonic() + args.timeout
            while run["status"] in {"queued", "running"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Benchmark polling timed out; the run remains inspectable in the UI")
                time.sleep(1)
                response = client.get("/api/runs/" + run["id"])
                response.raise_for_status()
                run = response.json()
            report["runs"].append(run)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            print(f"{run['id']}: {run['status']} / {run.get('verdict')}", flush=True)
    labelled = [run for run in report["runs"] if run.get("expected_verdict")]
    report["summary"] = {"total": len(report["runs"]), "failed": sum(r["status"] == "failed" for r in report["runs"]),
                         "labelled": len(labelled), "matching_expected": sum(r["verdict"] == r["expected_verdict"] for r in labelled)}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(f"Saved local report: {path}")
    return int(report["summary"]["failed"] > 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
        print(f"Benchmark did not finish ({type(exc).__name__}); inspect service status and saved runs.", file=sys.stderr)
        raise SystemExit(1) from None
