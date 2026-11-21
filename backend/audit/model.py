"""Small structured OpenAI-compatible adapter and per-task audit trace."""
import json
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

from openai import BadRequestError, UnprocessableEntityError

from audit.jsonsafe import json_safe

trace: ContextVar[list | None] = ContextVar("audit_trace", default=None)
Model = Any

# Fixed instruction for providers that only support JSON-object output. Validation is identical.
JSON_SCHEMA_INSTRUCTION = "\n\nReturn only a JSON object that conforms to the following JSON schema:\n"

class Agent:
    def __class_getitem__(cls, key):
        return cls

    def __init__(self, model, *, result_type, system_prompt):
        self.model, self.result_type, self.system_prompt = model, result_type, system_prompt

    async def run(self, prompt, model_settings=None):
        mode = getattr(self.model, "response_format", "json_schema")
        fallback_models = getattr(self.model, "fallback_models", None)
        if fallback_models is None:
            fallback_models = self.model.fallback_models = set()
        if mode == "auto":
            mode = "json_object" if self.model.name in fallback_models else "json_schema"
        try:
            return await self._attempt(prompt, model_settings, mode)
        except (BadRequestError, UnprocessableEntityError) as exc:
            if (getattr(self.model, "response_format", "json_schema") != "auto"
                    or mode != "json_schema" or self.result_type is str
                    or exc.status_code not in {400, 422}):
                raise
            fallback_models.add(self.model.name)
            return await self._attempt(prompt, model_settings, "json_object")

    async def _attempt(self, prompt, model_settings, mode):
        settings = model_settings or {}
        system = self.system_prompt
        if self.result_type is not str and mode == "json_object":
            system += JSON_SCHEMA_INSTRUCTION + json.dumps(self.result_type.model_json_schema(), sort_keys=True)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        params = {"model": self.model.name, "messages": messages,
                  "max_tokens": min(settings.get("max_tokens", self.model.max_tokens), self.model.max_tokens),
                  "temperature": settings.get("temperature", self.model.temperature)}
        if self.result_type is not str:
            params["response_format"] = ({"type": "json_object"} if mode == "json_object" else
                {"type": "json_schema", "json_schema": {
                    "name": self.result_type.__name__, "schema": self.result_type.model_json_schema()}})
        if self.model.thinking_control == "reasoning_effort":
            params["extra_body"] = {"reasoning_effort": "none"}
        elif (getattr(self.model, "provider", "lmstudio") == "lmstudio"
              and self.model.thinking_control in ("lmstudio", "chat_template_kwargs")):
            params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}
        call = {"request": params, "response_format": mode if self.result_type is not str else None,
                "raw_completion": None, "parsed_output": None,
                "response_model": None, "usage": None, "error_type": None}
        current = trace.get()
        if current is not None:
            current.append({"type": "model", **call})
            call = current[-1]
        try:
            response = await self.model.client.chat.completions.create(**params)
            call["raw_completion"] = json_safe(response.model_dump(mode="json"))
            call["response_model"] = response.model
            call["usage"] = json_safe(response.usage.model_dump(mode="json")) if response.usage else None
            raw = response.choices[0].message.content
            if raw is None:
                raise ValueError("empty completion")
            parsed = raw if self.result_type is str else self.result_type.model_validate_json(raw)
            parsed_output = parsed if isinstance(parsed, str) else parsed.model_dump(mode="json")
            # Pydantic accepts NaN/Infinity for floats; these cannot cross the JSON API.
            json.dumps(parsed_output, allow_nan=False)
            call["parsed_output"] = parsed_output
            return SimpleNamespace(data=parsed)
        except Exception as exc:
            call["error_type"] = type(exc).__name__
            raise
