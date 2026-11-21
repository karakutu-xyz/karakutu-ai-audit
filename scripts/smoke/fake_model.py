"""Offline fake OpenAI-compatible model for the Docker smoke suite. Standard library only.

It behaves like a provider without strict `json_schema` support: every `json_schema` request
gets HTTP 400, so the service's `MODEL_RESPONSE_FORMAT=auto` must fall back to `json_object`.
In `json_object` mode it reads the schema the service appends to the system prompt and
returns a fixed, clearly fictional answer that validates against it. A user prompt that
contains `SMOKE-SLOW` waits `FAKE_MODEL_SLOW_SECONDS` before answering, so a run can be
caught mid-flight. It logs nothing about request content.
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ("fake-default", "fake-relevance", "fake-embedding")
SCHEMA_MARKER = "conforms to the following JSON schema:\n"
SLOW_SECONDS = float(os.getenv("FAKE_MODEL_SLOW_SECONDS", "60"))
CANNED = {
    "RelevanceResult": {"is_relevant": True, "confidence": 0.8,
                        "reasoning": "Fake model: the fictional story concerns a public body."},
    "WebSearchResult": {"matches": [], "dead_same_story": [],
                        "reasoning": "Fake model: no independent copy among the fetched pages."},
    "SourceResult": {"source_kind": "own_reporting", "source_name": "", "source_url": "",
                     "attribution_quote": ""},
}


def fill(schema, defs):
    """A minimal instance of a JSON schema: required properties only."""
    if "$ref" in schema:
        return fill(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in schema:
        return fill(schema["anyOf"][0], defs)
    kind = schema.get("type")
    if kind == "object":
        return {key: fill(value, defs) for key, value in schema.get("properties", {}).items()
                if key in schema.get("required", [])}
    return {"string": "fake", "number": 0.5, "integer": 1, "boolean": False, "array": []}.get(kind)


def answer(messages):
    system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    if SCHEMA_MARKER not in system:
        return "fake"
    schema = json.loads(system.split(SCHEMA_MARKER, 1)[1])
    return json.dumps(CANNED.get(schema.get("title")) or fill(schema, schema.get("$defs", {})))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok"})
        if self.path.rstrip("/").endswith("/models"):
            return self.reply(200, {"object": "list", "data": [
                {"id": name, "object": "model"} for name in MODELS]})
        self.reply(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self.reply(404, {"error": {"message": "not found"}})
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if (request.get("response_format") or {}).get("type") == "json_schema":
            return self.reply(400, {"error": {"message": "json_schema is not supported",
                                              "type": "invalid_request_error"}})
        messages = request.get("messages", [])
        if any("SMOKE-SLOW" in str(m.get("content", "")) for m in messages if m.get("role") == "user"):
            time.sleep(SLOW_SECONDS)
        self.reply(200, {
            "id": "fake-completion", "object": "chat.completion", "created": int(time.time()),
            "model": request.get("model", MODELS[0]),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer(messages)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("FAKE_MODEL_PORT", "8000"))), Handler).serve_forever()
