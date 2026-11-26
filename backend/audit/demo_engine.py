"""Offline test-mode engine: every result, model call and evidence event is simulated."""
import asyncio
import hashlib
import json
import time

from audit.db import normalized_url
from audit.domain import (
    AuditRequest, AuditResponse, Classification, GeographicScope,
    StageName, StageOutcome, StageResult, Verdict,
)
from audit.engine import NAMES, dump
from audit.step_inputs import article_ref
from audit.workflow.pipeline import _skipped


MODEL_NAME = "demo-model (offline)"
DEMO_NOTE = "Demo output from the offline test mode; no external services were contacted."
# Stable article-ref mapping, independent of Python's randomized hash and article contents.
VERDICTS = (
    Verdict.MOVED, Verdict.FOUND_ONLINE, Verdict.POSSIBLE_CENSORSHIP, Verdict.INCONCLUSIVE,
)
COPY_URL = "https://publisher.example.invalid/demo/copy"
OTHER_URL = "https://other.example.invalid/demo/copy"


class DemoEngine:
    """AuditEngine-compatible demo with no clients, credentials or network operations."""

    def __init__(self, step_delay: float = 0.4):
        self.step_delay = step_delay
        self.synthetic = True
        self.public_config = {
            "model_enabled": False,
            "model_name": MODEL_NAME,
            "model_base_url": "",
            "model_timeout": 120,
            "max_tokens": 1024,
            "temperature": 0,
            "thinking_control": "none",
            "search_provider": "disabled",
            "searxng_url": "",
            "archive_base_url": "",
            "archive_authenticated": False,
            "demo": True,
        }

    async def start(self):
        pass

    async def close(self):
        pass

    async def available_models(self):
        # Offline: no provider catalog exists, so no discovery call and no error.
        return [], None

    async def run(self, request: AuditRequest, on_step=None, prompts: dict[str, str] | None = None,
                  models: dict | None = None) -> dict:
        # Prompts are captured in the workflow snapshot but never sent anywhere in test mode.
        # The per-step model names only label the simulated calls, so step assignments are visible.
        step_models = (models or {}).get("steps", {})
        # Every article, including an unseeded case, selects an existing offline scenario.
        ref = "u-" + hashlib.sha256(normalized_url(request.removed_article.url).encode()).hexdigest()[:16]
        bucket = int(ref[2:], 16) % len(VERDICTS)
        verdict = VERDICTS[bucket]
        classification = Classification(
            geographic_scope=GeographicScope.LOCAL,
            gist="Offline demo classification of a fictional local report.",
        )
        stages = []
        steps = []
        stopped = False
        for name in NAMES:
            started = time.monotonic()
            # Yield even with a zero delay; cancellation must propagate to the API queue.
            await asyncio.sleep(self.step_delay)
            inp = ({'skipped_because': 'Simulated similarity relocation.'} if stopped
                   else {'article': dump(article_ref(request.removed_article))})
            result = (
                _skipped(StageName(name), f"{DEMO_NOTE} Simulated similarity relocation.")
                if stopped else self._result(name, verdict, classification)
            )
            output = result.model_dump(mode="json")
            calls_model = not result.skipped and name in {"classification", "relevance", "source", "web_search"}
            model_name = step_models.get(name) or MODEL_NAME
            step = {
                "name": name,
                "status": "skipped" if result.skipped else "completed",
                "model": model_name if calls_model else None,
                "input": inp,
                "output": output,
                "model_calls": [self._model_call(output, model_name)] if calls_model else [],
                "evidence": self._evidence(name, verdict) if not result.skipped else [],
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
            if on_step is not None:
                await on_step(step)
            steps.append(step)
            stages.append(result)
            if name == "similarity" and verdict == Verdict.MOVED:
                stopped = True

        findings = {
            Verdict.MOVED: "A fabricated same-publisher copy simulates a measured relocation.",
            Verdict.FOUND_ONLINE: (
                "A fabricated copy simulates the story surviving elsewhere. "
                "This says nothing about why the original was removed and is not exculpatory."
            ),
            Verdict.POSSIBLE_CENSORSHIP: (
                "A fabricated missing copy simulates a flag for human review, "
                "not a finding of censorship. A human must establish what happened."
            ),
            Verdict.INCONCLUSIVE: (
                "Fabricated search results simulate an inconclusive search. "
                "No inference of censorship can be made."
            ),
        }[verdict]
        response = AuditResponse(
            verdict=verdict,
            stages=stages,
            short_circuited_at=StageName.SIMILARITY if stopped else None,
            findings=f"{DEMO_NOTE} {findings} These are simulated results, not real evidence.",
            classification=classification,
            flags=[],
        )
        return {
            "response": response.model_dump(mode="json"),
            "steps": steps,
            "model": {"name": MODEL_NAME, "enabled": False},
            "synthetic": True,
        }

    @staticmethod
    def _result(name: str, verdict: Verdict, classification: Classification) -> StageResult:
        outcome = StageOutcome.INCONCLUSIVE
        details = {"demo": True}
        if name == "classification":
            explanation = "Simulated classification; no catalog was queried or tags assigned."
            details["classification"] = classification.model_dump(mode="json")
            outcome = StageOutcome.PASS
        elif name == "similarity":
            moved = verdict == Verdict.MOVED
            explanation = "Simulated measured relocation." if moved else "No simulated relocation."
            details.update({"relocated_to": COPY_URL if moved else None,
                            "old_in_new": 1.0 if moved else None,
                            "new_in_old": 1.0 if moved else None})
            outcome = StageOutcome.PASS if moved else StageOutcome.INCONCLUSIVE
        elif name == "relevance":
            explanation = "Simulated public-interest screen keeps this demo in scope."
            details["is_relevant"] = True
            outcome = StageOutcome.PASS
        elif name == "related_removals":
            explanation = "No simulated related-removal pattern."
            details.update({"members": [], "flag": False})
        elif name == "source":
            explanation = "Simulated source lookup; no real original URL was probed."
            details.update({"live_at_original_url": False, "source_status": "unknown"})
        else:
            explanation = "Simulated search and page matching; no pages were fetched."
            found = verdict == Verdict.FOUND_ONLINE
            missing = verdict == Verdict.POSSIBLE_CENSORSHIP
            details.update({
                "hits_searched": 1, "live_pages_read": 0 if missing else 1,
                "search_queries": 1, "search_queries_failed": 0,
                "web_hits": 1, "archive_candidates": 0, "cited_source_leads": 0,
                "matches": [OTHER_URL] if found else [],
                "dead_same_story": [{"url": OTHER_URL, "reason": "Demo HTTP 404",
                                     "source": "demo"}] if missing else [],
            })
            outcome = StageOutcome.PASS if found else StageOutcome.FAIL
        return StageResult(
            stage=StageName(name), outcome=outcome,
            explanation=f"{DEMO_NOTE} {explanation}", details=details,
        )

    @staticmethod
    def _model_call(output: dict, model_name: str = MODEL_NAME) -> dict:
        return {
            "type": "model", "demo": True, "note": DEMO_NOTE,
            "request": {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "Offline demo transcript; never sent."},
                    {"role": "user", "content": f"Simulate the {output['stage']} step."},
                ],
                "response_format": {"type": "json_object"}, "temperature": 0,
            },
            "raw_completion": {
                "model": model_name,
                "choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}],
            },
            "parsed_output": output, "response_model": model_name,
            "usage": None, "error_type": None,
        }

    @staticmethod
    def _evidence(name: str, verdict: Verdict) -> list[dict]:
        if name in {"similarity", "related_removals"}:
            return [{
                "type": "archive", "demo": True, "note": DEMO_NOTE,
                "url": f"https://archive.example.invalid/demo/{name}",
                "results": [{"url": COPY_URL}] if name == "similarity" and verdict == Verdict.MOVED else [],
            }]
        if name not in {"source", "web_search"}:
            return []
        missing = name == "web_search" and verdict == Verdict.POSSIBLE_CENSORSHIP
        url = OTHER_URL if name == "web_search" else "https://source.example.invalid/demo/report"
        return [
            {"type": "search", "demo": True, "note": DEMO_NOTE,
             "provider": "offline demo", "query": "fictional demo report",
             "results": [{"url": url, "title": "Fabricated demo candidate",
                          "snippet": "Simulated evidence for the offline inspector."}],
             "error_type": None},
            {"type": "fetch_candidates", "demo": True, "note": DEMO_NOTE,
             "input": {"urls": [url]},
             "output": {"url": url, "status_code": 404 if missing else 200,
                        "simulated": True}, "error_type": None},
        ]
