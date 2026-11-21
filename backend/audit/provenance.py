"""Content-addressed workflow snapshots, retained with runs instead of a mutable version tag.

Hash the implementation, prompts, locked dependencies and behavior settings. Exclude runtime
paths, API keys and timestamps. The stored source snapshot remains readable after later edits.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from audit.config import MODEL_STEPS
from audit.workflow.prompt_registry import DEFAULTS

STEPS = [
    {"name": "classification", "title": "Classify", "description": "Assign catalog-validated IPTC topics, genre, scope and a short gist. Metadata only."},
    {"name": "similarity", "title": "Find a moved copy", "description": "Fetch archive similarity candidates and measure text containment. A measured relocation ends the audit."},
    {"name": "relevance", "title": "Screen public interest", "description": "Assess public-interest scope with the model. Failure means the audit did not complete."},
    {"name": "related_removals", "title": "Check related removals", "description": "Look for same-subject removals within the time window. A pattern can keep an otherwise low-interest article in scope."},
    {"name": "source", "title": "Verify the source", "description": "Probe the original article and any cited source using guarded fetching and measured text matching."},
    {"name": "web_search", "title": "Find independent evidence", "description": "Search and fetch independent copies, ground model claims in measured evidence, then choose a verdict in code."},
]

SAFE_CONFIG = {
    "model_provider", "response_format",
    "model_enabled", "model_base_url", "model_name", "model_timeout", "max_tokens",
    "temperature", "thinking_control", "search_provider", "searxng_url", "archive_base_url",
    "requested_model", "resolved_model", "archive_reads_enabled", "archive_authenticated",
}


def build_base(public_config: dict, *, source_root: Path | None = None) -> dict:
    root = source_root or Path(__file__).resolve().parent
    project = root.parent.parent
    sources = {str(path.relative_to(root)): path.read_text(encoding="utf-8")
               for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts}
    dependencies = {}
    for name in ("pyproject.toml", "uv.lock", "searxng/settings.yml", "compose.yaml", "compose.search.yaml",
                 "compose.test.yaml"):
        path = project / name
        if path.is_file():
            dependencies[name] = path.read_text(encoding="utf-8")
    settings = {key: value for key, value in public_config.items() if key in SAFE_CONFIG}
    return {
        "format_version": 3,
        "models": models_from_config(public_config),
        "steps": STEPS,
        "edges": [[a["name"], b["name"]] for a, b in zip(STEPS, STEPS[1:])],
        "settings": settings,
        "sources": sources,
        "dependencies": dependencies,
        "decision": "Deterministic evidence rules; models never select a verdict.",
    }


def models_from_config(config: dict) -> dict:
    default = config.get("model_name", "auto")
    overrides = config.get("model_steps", {})
    return {"provider": config.get("model_provider", "lmstudio"),
            "response_format": config.get("response_format", "json_schema"),
            "default": default,
            "steps": {step: overrides.get(step) or default for step in MODEL_STEPS}}


def with_prompts(base: dict, prompts: dict[str, str], models: dict | None = None) -> dict:
    definition = deepcopy(base)
    definition["prompts"] = dict(prompts)
    if models is not None:
        definition["models"] = deepcopy(models)
    canonical = json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "id": "wf_" + hashlib.sha256(canonical.encode()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "definition": definition,
    }


def build_workflow(public_config: dict, *, prompts: dict[str, str] | None = None, source_root: Path | None = None) -> dict:
    return with_prompts(build_base(public_config, source_root=source_root), prompts or DEFAULTS)
