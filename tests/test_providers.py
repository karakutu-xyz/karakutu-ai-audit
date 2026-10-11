import httpx
import pytest
from pydantic_ai.models.test import TestModel

from src.config import settings as settings_module
from src.providers import (
    PROVIDER_REGISTRY,
    check_provider_health,
    get_provider,
    list_available_providers,
)
from src.providers import local as local_module
from src.providers.local import LocalProvider


@pytest.fixture
def lmstudio_on(monkeypatch):
    """Flip the model toggle on the settings singleton (env vars are read at import)."""
    monkeypatch.setattr(settings_module.settings, "MODEL_ENABLED", True)
    monkeypatch.setattr(settings_module.settings, "MODEL_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setattr(settings_module.settings, "MODEL_NAME", "some-local-model")
    monkeypatch.setattr(settings_module.settings, "MODEL_API_KEY", "lm-studio")
    monkeypatch.setattr(settings_module.settings, "MODEL_THINKING_CONTROL", "lmstudio")
    monkeypatch.setattr(settings_module.settings, "MODEL_TEMPERATURE", "")
    return settings_module.settings


_LAST: dict = {}


def _stub_httpx_get(monkeypatch, *, status_code: int = 200, raises: Exception | None = None):
    """Replace httpx.AsyncClient so check_health never opens a real socket."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.requested_urls: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url: str, headers=None):
            calls.append(url)
            sent_headers.append(dict(headers or {}))
            if raises is not None:
                raise raises
            return httpx.Response(status_code, request=httpx.Request("GET", url))

    calls: list[str] = []
    sent_headers: list[dict] = []
    monkeypatch.setattr(local_module.httpx, "AsyncClient", _FakeAsyncClient)
    _LAST["headers"] = sent_headers
    return calls


def test_get_provider_local():
    assert isinstance(get_provider("local"), LocalProvider)


def test_get_provider_unknown_raises_valueerror():
    with pytest.raises(ValueError) as exc:
        get_provider("bogus")
    msg = str(exc.value)
    assert "Available" in msg
    assert "local" in msg


def test_list_available_providers_sorted():
    assert list_available_providers() == ["local"]


def test_registry_contains_only_local():
    assert set(PROVIDER_REGISTRY.keys()) == {"local"}


async def test_local_provider_health_true():
    provider = LocalProvider()
    assert await provider.check_health() is True


async def test_check_provider_health_handles_unknown():
    # Unknown name raises in get_provider; check_provider_health swallows it.
    assert await check_provider_health("bogus") is False


# --- LM Studio toggle (LocalProvider) ---------------------------------------


def test_local_provider_disabled_returns_testmodel():
    # Default (toggle off): no network, synthetic model.
    assert isinstance(LocalProvider().get_model(), TestModel)


def test_local_provider_enabled_returns_openai_model_at_base_url(lmstudio_on):
    model = LocalProvider().get_model()

    from pydantic_ai.models.openai import OpenAIModel

    assert isinstance(model, OpenAIModel)
    assert model.model_name == "some-local-model"
    # OpenAIProvider normalizes base_url with a trailing slash.
    assert model.client.base_url.__str__().rstrip("/") == lmstudio_on.MODEL_BASE_URL


def test_local_provider_enabled_with_empty_model_still_constructs(lmstudio_on, monkeypatch):
    # LM Studio resolves whatever model is loaded; an empty name must not raise.
    monkeypatch.setattr(settings_module.settings, "MODEL_NAME", "")
    from pydantic_ai.models.openai import OpenAIModel

    assert isinstance(LocalProvider().get_model(), OpenAIModel)


def test_local_module_does_not_import_openai_at_top_level():
    # Lazy import: `import src.providers.local` must work without `openai` installed,
    # so the OpenAI symbols must not be module globals.
    assert not hasattr(local_module, "OpenAIModel")
    assert not hasattr(local_module, "OpenAIProvider")


async def test_local_provider_enabled_health_pings_models_endpoint(lmstudio_on, monkeypatch):
    calls = _stub_httpx_get(monkeypatch, status_code=200)

    assert await LocalProvider().check_health() is True
    assert calls == ["http://localhost:1234/v1/models"]


async def test_local_provider_enabled_health_false_on_non_2xx(lmstudio_on, monkeypatch):
    _stub_httpx_get(monkeypatch, status_code=503)

    assert await LocalProvider().check_health() is False


async def test_local_provider_enabled_health_false_on_connect_error(lmstudio_on, monkeypatch):
    _stub_httpx_get(monkeypatch, raises=httpx.ConnectError("refused"))

    # Must swallow, never propagate — lifespan awaits this at startup.
    assert await LocalProvider().check_health() is False


async def test_local_provider_disabled_health_does_not_touch_network(monkeypatch):
    calls = _stub_httpx_get(monkeypatch, raises=AssertionError("must not be called"))

    assert await LocalProvider().check_health() is True
    assert calls == []


# --- LM Studio call bounds -------------------------------------------------------------
# The runaway-generation guard (see test_settings.test_lmstudio_call_bounds_defaults).
# Everything below must hold for the model LocalProvider hands the agents, because the
# agents never set model settings themselves.

def _params():
    from pydantic_ai.models import ModelRequestParameters
    return ModelRequestParameters(function_tools=[], allow_text_result=True, result_tools=[])


def test_lmstudio_client_never_retries_and_has_a_bounded_timeout(lmstudio_on, monkeypatch):
    # The pipeline's prompt ladder is the ONLY retry. The SDK's default max_retries=2 turned
    # one runaway call into three, and its 600s default timeout let each run for 10 minutes.
    monkeypatch.setattr(settings_module.settings, "MODEL_REQUEST_TIMEOUT", 123)
    model = LocalProvider().get_model()
    assert model.client.max_retries == 0
    assert model.client.timeout == 123


async def test_lmstudio_model_caps_output_tokens_by_default(lmstudio_on, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "MODEL_MAX_TOKENS", 777)
    model = LocalProvider().get_model()
    seen: dict = {}

    async def fake_create(messages, stream, model_settings, params):
        seen.update(model_settings)
        raise RuntimeError("stop before any HTTP")

    monkeypatch.setattr(model, "_completions_create", fake_create)
    with pytest.raises(RuntimeError):
        await model.request([], None, _params())
    assert seen["max_tokens"] == 777


async def test_lmstudio_model_lets_a_caller_override_the_cap(lmstudio_on, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "MODEL_MAX_TOKENS", 777)
    model = LocalProvider().get_model()
    seen: dict = {}

    async def fake_create(messages, stream, model_settings, params):
        seen.update(model_settings)
        raise RuntimeError("stop before any HTTP")

    monkeypatch.setattr(model, "_completions_create", fake_create)
    with pytest.raises(RuntimeError):
        await model.request([], {"max_tokens": 50}, _params())
    assert seen["max_tokens"] == 50


async def test_without_thinking_also_sends_reasoning_effort_none():
    # Measured 2026-09-09 on LM Studio 0.4.19+2 / gemma-4-12b-qat: `chat_template_kwargs.
    # enable_thinking=false` is no longer honoured (a classification call spent 397 of 400
    # tokens reasoning and ran into the 180 s timeout), while the OpenAI-style
    # `reasoning_effort: "none"` switches thinking off (reasoning_tokens 0, tool call in 22 s).
    # Both are sent: the old flag for builds that read it, the new field for builds that don't.
    from src.providers.lmstudio import without_thinking

    seen = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return "ok"

    await without_thinking(fake_create)(model="m", messages=[], extra_body={"other": 1})
    assert seen["extra_body"]["reasoning_effort"] == "none"
    assert seen["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert seen["extra_body"]["other"] == 1


async def test_without_thinking_adds_enable_thinking_false_to_the_request_body():
    # LM Studio / llama.cpp read `chat_template_kwargs.enable_thinking` from the request body;
    # pydantic-ai 0.0.49 has no extra_body hook, so the client's create() is wrapped.
    from src.providers.lmstudio import without_thinking

    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return "response"

    wrapped = without_thinking(fake_create)
    assert await wrapped(model="m", messages=[], extra_body={"keep": 1}) == "response"
    assert seen["model"] == "m"
    assert seen["extra_body"]["keep"] == 1                       # existing extras preserved
    assert seen["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_thinking_control_selects_the_request_wrapper(lmstudio_on, monkeypatch):
    from src.providers.lmstudio import reasoning_effort_none, without_thinking

    def wrapper_of(control):
        monkeypatch.setattr(settings_module.settings, "MODEL_THINKING_CONTROL", control)
        return LocalProvider().get_model().client.chat.completions.create

    assert wrapper_of("lmstudio").__name__ == without_thinking(None).__name__
    assert wrapper_of("reasoning_effort").__name__ == reasoning_effort_none(None).__name__
    assert getattr(wrapper_of("none"), "thinking_disabled", False) is False


async def test_reasoning_effort_control_sends_only_reasoning_effort():
    # Scaleway documents `chat_template_kwargs` as unsupported: the hosted body carries none.
    from src.providers.lmstudio import reasoning_effort_none

    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return "ok"

    await reasoning_effort_none(fake_create)(model="m", messages=[], extra_body={"other": 1})
    assert seen["extra_body"] == {"other": 1, "reasoning_effort": "none"}


async def test_lmstudio_body_is_unchanged_by_the_default_control(lmstudio_on):
    # The LM Studio reference profile must send exactly the pre-MODEL_* request body.
    model = LocalProvider().get_model()
    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return "ok"

    from src.providers.lmstudio import THINKING_WRAPPERS
    await THINKING_WRAPPERS["lmstudio"](fake_create)(model="m", messages=[])
    assert seen["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}
    assert "temperature" not in model.default_settings          # unset = the server's own default
    assert model.default_settings == {"max_tokens": settings_module.settings.MODEL_MAX_TOKENS}


async def test_temperature_is_sent_only_when_set(lmstudio_on, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "MODEL_TEMPERATURE", "0.25")
    model = LocalProvider().get_model()
    seen: dict = {}

    async def fake_create(messages, stream, model_settings, params):
        seen.update(model_settings)
        raise RuntimeError("stop before any HTTP")

    monkeypatch.setattr(model, "_completions_create", fake_create)
    with pytest.raises(RuntimeError):
        await model.request([], None, _params())
    assert seen["temperature"] == 0.25
    assert model.describe() == {"requested": "some-local-model", "thinking_control": "lmstudio",
                                "max_tokens": settings_module.settings.MODEL_MAX_TOKENS, "temperature": 0.25}


async def test_health_check_sends_the_api_key_as_bearer(lmstudio_on, monkeypatch):
    # Scaleway answers /v1/models with 401 without it, so /ready and the worker's pause would
    # see a healthy hosted model as down.
    monkeypatch.setattr(settings_module.settings, "MODEL_API_KEY", "scw-test-key")
    _stub_httpx_get(monkeypatch, status_code=200)
    assert await LocalProvider().check_health() is True
    assert _LAST["headers"] == [{"Authorization": "Bearer scw-test-key"}]


async def test_served_model_ids_are_collected_per_context(lmstudio_on, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.openai import OpenAIModel
    from pydantic_ai.usage import Usage

    from src.providers import served

    async def fake_request(self, messages, model_settings, params):
        return ModelResponse([TextPart("x")], model_name="served-id-2506"), Usage()

    monkeypatch.setattr(OpenAIModel, "request", fake_request)
    model = LocalProvider().get_model()
    ids, token = served.start()
    try:
        await model.request([], None, _params())
    finally:
        served.stop(token)
    assert ids == {"served-id-2506"}
    await model.request([], None, _params())       # outside a collection: dropped, no error
    assert ids == {"served-id-2506"}
