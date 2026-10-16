# src/config/settings.py
import json
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# No arguments: python-dotenv searches upward from THIS file's directory (src/config/), so the
# repository-root .env is found whatever the working directory.
load_dotenv()

# Minimum length of API_AUTH_TOKEN when it is set (validate()).
MIN_API_TOKEN_CHARS = 32

THINKING_CONTROLS = ("lmstudio", "reasoning_effort", "none")


def _env(name: str, alias: str, default: str) -> str:
    """`name`, else its legacy `alias`, else `default` (an empty value counts as unset)."""
    return os.getenv(name) or os.getenv(alias) or default


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"true", "1", "yes"}


class Settings:
    """Application settings loaded from environment variables."""

    # Provider configuration
    PROVIDER_TYPE: str = os.getenv("PROVIDER_TYPE", "local")

    # Web-search configuration
    WEB_SEARCH_PROVIDER: str = os.getenv("WEB_SEARCH_PROVIDER", "stub")

    # SearXNG (self-hosted metasearch) configuration. Used only when
    # WEB_SEARCH_PROVIDER=searxng. Inside Docker this must be the compose service name
    # (http://searxng:8080), not localhost — localhost is the container itself.
    SEARXNG_URL: str = os.getenv("SEARXNG_URL", "http://localhost:8888")
    SEARXNG_TIMEOUT: int = int(os.getenv("SEARXNG_TIMEOUT", "15"))
    # Comma-separated engine list passed straight through to SearXNG. Empty = its default
    # set. Aggregating several engines is the entire point: querying DuckDuckGo alone missed
    # relocations that plainly existed and were live.
    # Default = the reference profile (.env.lmstudio, the one the pipeline is calibrated against):
    # the image and the batch scripts must search the same engines or their results differ.
    SEARXNG_ENGINES: str = os.getenv("SEARXNG_ENGINES", "google,bing,duckduckgo")
    SEARXNG_LANGUAGE: str = os.getenv("SEARXNG_LANGUAGE", "tr")

    # Model server: any OpenAI-compatible endpoint (LM Studio locally, a hosted API such as
    # Scaleway Generative APIs). Every MODEL_* name also accepts its legacy LMSTUDIO_* alias;
    # the MODEL_* name wins when both are set.
    MODEL_ENABLED: bool = _env("MODEL_ENABLED", "LMSTUDIO_ENABLED", "false").strip().lower() in {"true", "1", "yes"}
    MODEL_BASE_URL: str = _env("MODEL_BASE_URL", "LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    MODEL_NAME: str = _env("MODEL_NAME", "LMSTUDIO_MODEL", "")  # empty string, never None
    MODEL_API_KEY: str = _env("MODEL_API_KEY", "LMSTUDIO_API_KEY", "lm-studio")
    MODEL_HEALTH_TIMEOUT: int = int(_env("MODEL_HEALTH_TIMEOUT", "LMSTUDIO_TIMEOUT", "30"))
    # Bounds on every MODEL call (MODEL_HEALTH_TIMEOUT above is only the health check). With no
    # output cap, a small local model (gemma-4-12b) can run away on stage-3 prompts — it skips the
    # required tool call, answers in free text, then REPEATS the same sentences until cut off
    # (NOT a reasoning trace — reasoning_content is empty). Each call then sits on the OpenAI
    # client's 600s default timeout, which the SDK retries twice, so one article can take most
    # of an hour. A doomed call must fail fast so the pipeline's own prompt
    # ladder (the only retry) and AUDIT_FAILED take over.
    MODEL_REQUEST_TIMEOUT: int = int(_env("MODEL_REQUEST_TIMEOUT", "LMSTUDIO_REQUEST_TIMEOUT", "180"))  # 3 retry rungs at 180s (+overhead) fit a 600s per-case budget
    MODEL_MAX_TOKENS: int = int(_env("MODEL_MAX_TOKENS", "LMSTUDIO_MAX_TOKENS", "4096"))
    # Empty = not sent: the server's own default applies. For LM Studio that is the model's
    # preset (gemma-4-12b-qat: temperature 1.0, top_p 0.95, top_k 64, read from its model.yaml);
    # hosted APIs differ (Scaleway: 0.7), so a hosted profile pins it.
    MODEL_TEMPERATURE: str = os.getenv("MODEL_TEMPERATURE", "").strip()
    # How thinking is switched off (both stages are bounded classification; a reasoning trace
    # adds latency, not accuracy):
    #   lmstudio          chat_template_kwargs.enable_thinking=false AND reasoning_effort="none"
    #   reasoning_effort  reasoning_effort="none" only (servers that reject chat_template_kwargs)
    #   none              send nothing (thinking left to the server)
    # Unset: `lmstudio`, or `none` when the legacy LMSTUDIO_THINKING=true is set.
    MODEL_THINKING_CONTROL: str = (os.getenv("MODEL_THINKING_CONTROL", "").strip().lower()
                                   or ("none" if _flag("LMSTUDIO_THINKING", "false") else "lmstudio"))

    # Karakutu Server API (the archive's own backend — article store + similarity source).
    # Auth is an Arweave wallet signature challenge that mints a 15-MINUTE JWT, so there is
    # no static token to configure: point KARAKUTU_WALLET_PATH at a whitelisted RSA-4096
    # keyfile and the client mints and refreshes its own tokens.
    # No keyfile (or a missing file) => the client is DISABLED, makes no calls, and the
    # similarity stage stays dormant — the service still boots and audits with zero config.
    # Access: reads, plus IPTC tag writes and discussion comments from article jobs when
    # ARCHIVE_WRITES_ENABLED=true. Nothing else is ever written.
    KARAKUTU_API_URL: str = os.getenv("KARAKUTU_API_URL", "https://k8s.karakutu.xyz")
    # Repo-relative by default and deliberately generic: a specific operator's wallet
    # filename must not be baked into shared code. Any `arweave-keyfile*.json` is
    # gitignored. A missing file is not an error — the client simply stays DISABLED.
    KARAKUTU_WALLET_PATH: str = os.getenv("KARAKUTU_WALLET_PATH", "arweave-keyfile.json")
    KARAKUTU_API_TIMEOUT: int = int(os.getenv("KARAKUTU_API_TIMEOUT", "15"))
    # Where the IPTC vocabulary pulled from GET /concepts is cached between restarts.
    KARAKUTU_CONCEPTS_CACHE: str = os.getenv("KARAKUTU_CONCEPTS_CACHE", ".cache/karakutu-concepts.json")

    # API configuration
    PORT: int = int(os.getenv("PORT", "2200"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Keyfile CONTENT (JSON) from a secret manager — an alternative to KARAKUTU_WALLET_PATH for
    # containers. Private key material: never logged, never echoed (see /ready, which reports
    # only present/absent).
    KARAKUTU_WALLET_JSON: str = os.getenv("KARAKUTU_WALLET_JSON", "")
    # Base of the `view_url` written into the apply ledger (was hard-coded in apply_results.py).
    KARAKUTU_FRONTEND_URL: str = os.getenv("KARAKUTU_FRONTEND_URL", "https://karakutu.xyz")

    # --- /api/audit hardening -------------------------------------------------------------
    # Bearer token for the /api/v1 article routes, /api/audit and the detailed /ready view; at
    # least MIN_API_TOKEN_CHARS (32) characters or boot refuses. Unset = the article routes answer
    # 404 and no job worker runs, /api/audit is open (local development, the test suite) and
    # /ready serves the status only. Set it in any deployment reachable from outside.
    API_AUTH_TOKEN: str = os.getenv("API_AUTH_TOKEN", "")
    # Fail closed: with API_AUTH_TOKEN unset, boot is refused as soon as the configuration is
    # real (a model, a non-stub search provider or a wallet), because an open /api/audit would
    # then spend the operator's model budget and read the archive with the operator's wallet
    # for anyone who reaches the port. The synthetic defaults stay open so the zero-config
    # quick start and the test suite keep working. ALLOW_UNAUTHENTICATED_AUDIT=true overrides
    # it for an isolated local run; never set it on a reachable host.
    ALLOW_UNAUTHENTICATED_AUDIT: bool = os.getenv(
        "ALLOW_UNAUTHENTICATED_AUDIT", "false").strip().lower() in {"true", "1", "yes"}
    # Comma-separated browser origins allowed to call /api/audit and /health. Empty (default) =
    # no CORS middleware at all. `*` is for local development only. Never applied to /api/v1/*
    # (server-to-server).
    CORS_ALLOW_ORIGINS: str = os.getenv("CORS_ALLOW_ORIGINS", "")
    # Global write switch for article jobs. false (default) = shadow mode: every job is a dry run
    # (nothing is PATCHed or POSTed to the archive; the rendered comment is stored in the job).
    ARCHIVE_WRITES_ENABLED: bool = _flag("ARCHIVE_WRITES_ENABLED", "false")

    # --- jobs (in-process queue + store) ------------------------------------------------------
    # Directory for the job store (and other service state). Must be a persistent volume.
    DATA_DIR: str = os.getenv("DATA_DIR", "data")
    # sqlite:///<path> | memory:// . Empty = sqlite:///<DATA_DIR>/karakutu-audit.sqlite3
    JOB_STORE_URL: str = os.getenv("JOB_STORE_URL", "")
    JOB_CONCURRENCY: int = int(os.getenv("JOB_CONCURRENCY", "1"))
    JOB_QUEUE_MAX: int = int(os.getenv("JOB_QUEUE_MAX", "500"))
    JOB_CASE_TIMEOUT_S: int = int(os.getenv("JOB_CASE_TIMEOUT_S", "600"))
    JOB_MAX_ATTEMPTS: int = int(os.getenv("JOB_MAX_ATTEMPTS", "3"))
    JOB_APPLY_MAX_ATTEMPTS: int = int(os.getenv("JOB_APPLY_MAX_ATTEMPTS", "3"))
    JOB_RETRY_BACKOFF_S: float = float(os.getenv("JOB_RETRY_BACKOFF_S", "60"))
    JOB_FAIL_STREAK: int = int(os.getenv("JOB_FAIL_STREAK", "3"))
    JOB_HEALTH_POLL_S: float = float(os.getenv("JOB_HEALTH_POLL_S", "60"))
    JOB_HEALTH_WAIT_MAX_S: float = float(os.getenv("JOB_HEALTH_WAIT_MAX_S", "14400"))
    JOB_RESULT_REUSE_S: float = float(os.getenv("JOB_RESULT_REUSE_S", "0"))
    JOB_RETENTION_DAYS: int = int(os.getenv("JOB_RETENTION_DAYS", "180"))
    JOB_SHUTDOWN_GRACE_S: float = float(os.getenv("JOB_SHUTDOWN_GRACE_S", "30"))
    ARCHIVE_WRITE_PACING_S: float = float(os.getenv("ARCHIVE_WRITE_PACING_S", "0.5"))

    # Service info
    SERVICE_NAME: str = "karakutu-audit"

    # --- derived / validation --------------------------------------------------------------

    @property
    def jobs_enabled(self) -> bool:
        """The article routes and the job worker exist only with API_AUTH_TOKEN set (fail closed)."""
        return bool(self.API_AUTH_TOKEN)

    @property
    def is_real_configuration(self) -> bool:
        """A real model, a real search provider or a wallet is configured (not the synthetic
        defaults). Drives the fail-closed check on an unset API_AUTH_TOKEN."""
        if self.MODEL_ENABLED or self.PROVIDER_TYPE != "local":
            return True
        if self.WEB_SEARCH_PROVIDER != "stub":
            return True
        if self.KARAKUTU_WALLET_JSON.strip():
            return True
        path = self.KARAKUTU_WALLET_PATH.strip()
        return bool(path) and Path(path).is_file()

    @property
    def model_temperature(self) -> Optional[float]:
        return float(self.MODEL_TEMPERATURE) if self.MODEL_TEMPERATURE else None

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.CORS_ALLOW_ORIGINS.split(",") if o.strip()]

    @property
    def job_store_url(self) -> str:
        return self.JOB_STORE_URL.strip() or f"sqlite:///{Path(self.DATA_DIR) / 'karakutu-audit.sqlite3'}"

    def validate(self) -> None:
        """Fail fast on configuration that would make the service unsafe. Called in lifespan.

        Raises `SettingsError` listing every problem (never echoing a secret's value).
        """
        problems: List[str] = []
        if self.API_AUTH_TOKEN and len(self.API_AUTH_TOKEN) < MIN_API_TOKEN_CHARS:
            # Never the value, nor its length.
            problems.append(f"API_AUTH_TOKEN is shorter than {MIN_API_TOKEN_CHARS} characters")
        if not self.API_AUTH_TOKEN and self.is_real_configuration and not self.ALLOW_UNAUTHENTICATED_AUDIT:
            problems.append(
                "API_AUTH_TOKEN is unset while a real model, search provider or wallet is configured, "
                "so /api/audit would be open to anyone who reaches the port; set API_AUTH_TOKEN "
                f"(at least {MIN_API_TOKEN_CHARS} characters) or, for an isolated local run only, "
                "ALLOW_UNAUTHENTICATED_AUDIT=true")
        url = self.job_store_url
        if not (url.startswith("sqlite:///") or url == "memory://"):
            problems.append("JOB_STORE_URL must be sqlite:///<path> or memory://")
        for name in ("JOB_CONCURRENCY", "JOB_QUEUE_MAX", "JOB_MAX_ATTEMPTS", "JOB_APPLY_MAX_ATTEMPTS",
                     "JOB_FAIL_STREAK", "JOB_CASE_TIMEOUT_S"):
            if getattr(self, name) < 1:
                problems.append(f"{name} must be >= 1")
        if self.MODEL_THINKING_CONTROL not in THINKING_CONTROLS:
            problems.append("MODEL_THINKING_CONTROL must be one of " + ", ".join(THINKING_CONTROLS))
        if self.MODEL_TEMPERATURE:
            try:
                float(self.MODEL_TEMPERATURE)
            except ValueError:
                problems.append("MODEL_TEMPERATURE must be a number")
        if self.KARAKUTU_WALLET_JSON.strip():
            try:
                parsed = json.loads(self.KARAKUTU_WALLET_JSON)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                problems.append("KARAKUTU_WALLET_JSON is set but is not a JSON object")
        if problems:
            raise SettingsError("; ".join(problems))


class SettingsError(RuntimeError):
    """Invalid configuration (raised by Settings.validate)."""


settings = Settings()


def get_settings() -> Settings:
    return settings
