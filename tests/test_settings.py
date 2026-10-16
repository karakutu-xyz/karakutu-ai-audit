import importlib

import dotenv
import pytest

from src.config import settings as settings_module
from src.config.settings import get_settings

_MODEL_KEYS = ["ENABLED", "BASE_URL", "NAME", "API_KEY", "HEALTH_TIMEOUT", "REQUEST_TIMEOUT",
               "MAX_TOKENS", "TEMPERATURE", "THINKING_CONTROL"]
_LEGACY_KEYS = ["ENABLED", "BASE_URL", "MODEL", "API_KEY", "TIMEOUT", "REQUEST_TIMEOUT",
                "MAX_TOKENS", "THINKING"]
MODEL_ENV_KEYS = [f"MODEL_{k}" for k in _MODEL_KEYS] + [f"LMSTUDIO_{k}" for k in _LEGACY_KEYS]


@pytest.fixture
def reload_settings(monkeypatch):
    """Re-evaluate the Settings class body against a controlled environment.

    Settings fields are class-level and evaluated once at import, so exercising
    them means clearing every model key (MODEL_* and the legacy LMSTUDIO_*) and reloading the module. load_dotenv
    is neutralized so a developer's local .env cannot leak into the assertions.
    The module is reloaded once more on teardown to restore the real singleton.
    """

    def _reload(**env):
        for key in MODEL_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
        return importlib.reload(settings_module).get_settings()

    yield _reload
    monkeypatch.undo()
    importlib.reload(settings_module)


def test_model_defaults(reload_settings):
    s = reload_settings()
    assert s.MODEL_ENABLED is False
    assert s.MODEL_BASE_URL == "http://localhost:1234/v1"
    assert s.MODEL_NAME == ""
    assert s.MODEL_API_KEY == "lm-studio"
    assert s.MODEL_HEALTH_TIMEOUT == 30
    assert s.MODEL_TEMPERATURE == "" and s.model_temperature is None   # not sent: server default


def test_model_call_bounds_defaults(reload_settings):
    """Every model call is bounded. With no cap a small local model can run away (skip
    the required tool call, answer in free text, then loop on the same sentences for
    thousands of tokens — not a reasoning trace), each call sitting on the OpenAI client's
    600s default timeout while the SDK's retries re-send it — most of an hour for ONE
    article. These defaults make a doomed call fail fast so the pipeline's own prompt ladder
    (the only retry) and AUDIT_FAILED take over."""
    s = reload_settings()
    assert s.MODEL_REQUEST_TIMEOUT == 180   # per model call; 3 retry rungs fit a 600s case budget (240 did not)
    assert s.MODEL_MAX_TOKENS == 4096       # output cap per call; a runaway ends in ~165s at 25 tok/s
    assert s.MODEL_THINKING_CONTROL == "lmstudio"   # both stages are bounded classification — no reasoning needed


@pytest.mark.parametrize("raw", ["true", "1", "YES"])
def test_legacy_lmstudio_thinking_true_means_no_thinking_control(reload_settings, raw):
    assert reload_settings(LMSTUDIO_THINKING=raw).MODEL_THINKING_CONTROL == "none"


@pytest.mark.parametrize("raw", ["lmstudio", "reasoning_effort", "none", " Reasoning_Effort "])
def test_thinking_control_from_env(reload_settings, raw):
    s = reload_settings(MODEL_THINKING_CONTROL=raw, LMSTUDIO_THINKING="true")
    assert s.MODEL_THINKING_CONTROL == raw.strip().lower()   # the explicit setting wins


def test_model_call_bounds_from_env(reload_settings):
    s = reload_settings(MODEL_REQUEST_TIMEOUT="120", MODEL_MAX_TOKENS="2048", MODEL_TEMPERATURE="0.3")
    assert s.MODEL_REQUEST_TIMEOUT == 120
    assert s.MODEL_MAX_TOKENS == 2048
    assert s.model_temperature == 0.3


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "Yes", " true "])
def test_model_enabled_truthy_values(reload_settings, raw):
    assert reload_settings(MODEL_ENABLED=raw).MODEL_ENABLED is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "", "maybe"])
def test_model_enabled_falsy_values(reload_settings, raw):
    assert reload_settings(MODEL_ENABLED=raw).MODEL_ENABLED is False


def test_model_overrides_from_env(reload_settings):
    s = reload_settings(
        MODEL_BASE_URL="http://127.0.0.1:9999/v1",
        MODEL_NAME="qwen2.5-7b-instruct",
        MODEL_API_KEY="secret",
        MODEL_HEALTH_TIMEOUT="90",
    )
    assert s.MODEL_BASE_URL == "http://127.0.0.1:9999/v1"
    assert s.MODEL_NAME == "qwen2.5-7b-instruct"
    assert s.MODEL_API_KEY == "secret"
    assert s.MODEL_HEALTH_TIMEOUT == 90


def test_legacy_lmstudio_names_are_aliases(reload_settings):
    s = reload_settings(LMSTUDIO_ENABLED="true", LMSTUDIO_BASE_URL="http://h/v1", LMSTUDIO_MODEL="m",
                        LMSTUDIO_API_KEY="k", LMSTUDIO_TIMEOUT="9", LMSTUDIO_REQUEST_TIMEOUT="99",
                        LMSTUDIO_MAX_TOKENS="999")
    assert (s.MODEL_ENABLED, s.MODEL_BASE_URL, s.MODEL_NAME, s.MODEL_API_KEY) == (True, "http://h/v1", "m", "k")
    assert (s.MODEL_HEALTH_TIMEOUT, s.MODEL_REQUEST_TIMEOUT, s.MODEL_MAX_TOKENS) == (9, 99, 999)


def test_model_name_wins_over_its_legacy_alias(reload_settings):
    s = reload_settings(MODEL_NAME="new", LMSTUDIO_MODEL="old", MODEL_MAX_TOKENS="10", LMSTUDIO_MAX_TOKENS="20")
    assert (s.MODEL_NAME, s.MODEL_MAX_TOKENS) == ("new", 10)


@pytest.mark.parametrize("env, problem", [
    ({"MODEL_THINKING_CONTROL": "off"}, "MODEL_THINKING_CONTROL"),
    ({"MODEL_TEMPERATURE": "warm"}, "MODEL_TEMPERATURE"),
])
def test_validate_rejects_bad_model_settings(reload_settings, env, problem):
    s = reload_settings(**env)
    with pytest.raises(settings_module.SettingsError, match=problem):
        s.validate()


def test_existing_settings_unchanged():
    s = get_settings()
    assert s.PROVIDER_TYPE == "local"
    assert s.WEB_SEARCH_PROVIDER == "stub"
    assert s.PORT == 2200
    assert s.LOG_LEVEL == "INFO"
    assert s.SERVICE_NAME == "karakutu-audit"


def test_karakutu_api_defaults(reload_settings):
    """Auth is wallet-based (the API's JWTs last 15 min), so there is no static token to
    configure — only a keyfile path. A missing keyfile disables the client entirely."""
    s = reload_settings()

    assert s.KARAKUTU_API_URL == "https://k8s.karakutu.xyz"
    assert s.KARAKUTU_WALLET_PATH.endswith(".json")
    assert s.KARAKUTU_API_TIMEOUT == 15
    assert not hasattr(s, "KARAKUTU_API_TOKEN"), "static tokens are obsolete — wallet auth only"


def test_karakutu_wallet_path_is_read_from_env(reload_settings, monkeypatch):
    monkeypatch.setenv("KARAKUTU_WALLET_PATH", "/secure/wallet.json")
    monkeypatch.setenv("KARAKUTU_API_URL", "https://archive.example")

    s = reload_settings()

    assert s.KARAKUTU_WALLET_PATH == "/secure/wallet.json"
    assert s.KARAKUTU_API_URL == "https://archive.example"
