"""The model for any OpenAI-compatible server (LM Studio, a hosted API): an OpenAIModel whose
every call is bounded.

Imported LAZILY by `LocalProvider.get_model` (this module imports `openai`; `src.providers.local`
must stay importable without it).

Why this exists — the failure mode of a small local model (gemma-4-12b-qat) under the defaults
pydantic-ai and the OpenAI SDK ship:

* With no output cap, the model can run away on stage-3 prompts: many thousands of tokens per
  call, never reaching an answer. The tokens are NOT a reasoning trace (`reasoning_content` is
  empty): the model skips the required tool call, writes its answer as free text — correct, even —
  then falls into a repetition loop, re-emitting the same sentences until cut off.
* Each runaway call sits on the SDK's **600s default timeout**, and the SDK's **default
  max_retries=2** re-sends the identical call twice more. Stage 3's own prompt ladder then
  shrinks the prompt and tries again, so one article can take most of an hour to answer.

Three bounds, all from settings:

* `MODEL_MAX_TOKENS` -> `max_tokens` on every call unless the caller sets its own. A runaway now
  ends (`finish_reason=length`) well before the timeout, and pydantic-ai raises so the ladder moves on.
* `MODEL_REQUEST_TIMEOUT` -> the client timeout, and `max_retries=0`: **the pipeline's prompt
  ladder is the only retry.** A transport retry of a runaway is just another runaway.
* `MODEL_THINKING_CONTROL=lmstudio` (default) -> `chat_template_kwargs.enable_thinking=false` AND
  `reasoning_effort="none"` in the request body (the llama.cpp template switch and the
  OpenAI-style field; only the latter still works on LM Studio 0.4.19+2). pydantic-ai 0.0.49 has no extra_body hook, so the client's
  `chat.completions.create` is wrapped. Both stages are bounded classification with a structured
  answer; a reasoning trace adds latency, not accuracy. On gemma-4-12b in
  LM Studio 0.4.19, with the flag absent most responses carry a reasoning trace and stage 1 is
  several times slower; with the flag sent, none do. The flag is honoured. It is NOT what stops the runaway above — that loop is
  plain text with an empty reasoning trace — the cap and the timeout are.
  `reasoning_effort` sends only `reasoning_effort="none"`, for hosted servers that reject
  `chat_template_kwargs` (Scaleway documents it as unsupported); `none` sends neither.

`MODEL_TEMPERATURE` is sent only when set. Every response's model id is noted in
`src.providers.served`, and `describe()` reports the settings, for the run record.
"""
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from openai import AsyncOpenAI
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings, merge_model_settings
from pydantic_ai.usage import Usage

from src.providers import served

Create = Callable[..., Awaitable[Any]]


class LMStudioModel(OpenAIModel):
    """OpenAIModel with default per-call settings (merged UNDER whatever the caller passes)."""

    def __init__(self, model_name: str, *, provider: OpenAIProvider, default_settings: ModelSettings,
                 thinking_control: str = "lmstudio"):
        super().__init__(model_name, provider=provider)
        self.default_settings: ModelSettings = default_settings
        self.thinking_control = thinking_control

    def _settings(self, model_settings: ModelSettings | None) -> ModelSettings | None:
        return merge_model_settings(self.default_settings, model_settings)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ModelResponse, Usage]:
        response, usage = await super().request(messages, self._settings(model_settings), model_request_parameters)
        served.note(response.model_name)
        return response, usage

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> AsyncIterator[StreamedResponse]:
        async with super().request_stream(
            messages, self._settings(model_settings), model_request_parameters,
        ) as stream:
            yield stream
        served.note(getattr(stream, "model_name", None))

    def describe(self) -> dict:
        """The requested model and the settings every call carries (no secrets)."""
        return {"requested": self.model_name, "thinking_control": self.thinking_control,
                "max_tokens": self.default_settings.get("max_tokens"),
                "temperature": self.default_settings.get("temperature")}


def without_thinking(create: Create) -> Create:
    """Wrap `client.chat.completions.create` so every request carries
    `extra_body.chat_template_kwargs.enable_thinking = false` and
    `extra_body.reasoning_effort = "none"`. Other extras are preserved."""

    async def create_without_thinking(*args: Any, **kwargs: Any) -> Any:
        extra = dict(kwargs.get("extra_body") or {})
        template_kwargs = dict(extra.get("chat_template_kwargs") or {})
        template_kwargs["enable_thinking"] = False
        extra["chat_template_kwargs"] = template_kwargs
        # On LM Studio 0.4.19+2 with gemma-4-12b-qat the template flag above is NO LONGER
        # honoured — a classification prompt spends nearly every generated token reasoning and
        # runs into the timeout — while the OpenAI-style `reasoning_effort: "none"` switches
        # thinking off. Both are sent so either build behaves.
        extra.setdefault("reasoning_effort", "none")
        kwargs["extra_body"] = extra
        return await create(*args, **kwargs)

    create_without_thinking.thinking_disabled = True  # type: ignore[attr-defined]  — inspectable in tests
    return create_without_thinking


def reasoning_effort_none(create: Create) -> Create:
    """Wrap `client.chat.completions.create` so every request carries only
    `extra_body.reasoning_effort = "none"` (no `chat_template_kwargs`). Other extras are preserved."""

    async def create_reasoning_effort_none(*args: Any, **kwargs: Any) -> Any:
        extra = dict(kwargs.get("extra_body") or {})
        extra.setdefault("reasoning_effort", "none")
        kwargs["extra_body"] = extra
        return await create(*args, **kwargs)

    create_reasoning_effort_none.thinking_disabled = True  # type: ignore[attr-defined]
    return create_reasoning_effort_none


THINKING_WRAPPERS = {"lmstudio": without_thinking, "reasoning_effort": reasoning_effort_none}


def build_lmstudio_model(settings) -> LMStudioModel:
    """The model `LocalProvider` hands to the agents when MODEL_ENABLED is on."""
    client = AsyncOpenAI(
        base_url=settings.MODEL_BASE_URL,
        api_key=settings.MODEL_API_KEY,
        timeout=settings.MODEL_REQUEST_TIMEOUT,
        max_retries=0,  # the pipeline's prompt ladder is the only retry
    )
    wrap = THINKING_WRAPPERS.get(settings.MODEL_THINKING_CONTROL)
    if wrap is not None:
        client.chat.completions.create = wrap(client.chat.completions.create)
    defaults: ModelSettings = {"max_tokens": settings.MODEL_MAX_TOKENS}
    if settings.model_temperature is not None:
        defaults["temperature"] = settings.model_temperature
    return LMStudioModel(
        settings.MODEL_NAME,
        provider=OpenAIProvider(openai_client=client),
        default_settings=defaults,
        thinking_control=settings.MODEL_THINKING_CONTROL,
    )
