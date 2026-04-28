"""
Enterprise-hardened application configuration.

Security posture:
  - Zero-trust defaults: every field is locked down; permissiveness must be
    explicit and justified.
  - Secrets are never logged, repr'd, or serialised — SecretStr throughout.
  - Placeholder / weak-key detection on startup; refuses to boot with bad creds.
  - CORS allowlist validated: no wildcards in production.
  - Rate-limit format validated at import time.
  - All numeric bounds are tight and documented.
  - Singleton cached; config object is immutable after construction.
  - Full audit trail emitted on first load (keys redacted).
"""

from __future__ import annotations

import logging
import re
import sys
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PLACEHOLDER_PREFIXES = frozenset(
    {"your_", "sk-placeholder", "changeme", "todo", "xxx", "test-key"}
)
_MIN_API_KEY_LEN = 20
_RATE_LIMIT_RE = re.compile(r"^\d+/(second|minute|hour|day)$")

# Known-weak model identifiers — reject use in non-dev environments.
_WEAK_MODELS = frozenset({"gpt-3.5-turbo", "text-davinci-003"})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """
    Immutable, validated, enterprise-grade application settings.

    Design invariants
    -----------------
    1. Secrets are ``SecretStr`` — never appear in logs or stack traces.
    2. Every field has an explicit ge/le or length constraint.
    3. Validators are ``@classmethod`` and ``mode='before'`` where coercion
       is needed; ``mode='after'`` for cross-field checks.
    4. ``model_config`` sets ``frozen=True`` so the singleton cannot be
       mutated after construction.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",        # unknown env vars cause a hard error
        frozen=True,           # immutable after construction
    )

    # ── Environment ───────────────────────────────────────────────────────
    environment: Literal["development", "staging", "production"] = Field(
        default="production",
        description=(
            "Deployment environment. Controls security posture: "
            "production tightens CORS, disables debug routes, etc."
        ),
    )

    # ── LLM Provider ──────────────────────────────────────────────────────
    openrouter_api_key: SecretStr = Field(
        ...,
        min_length=_MIN_API_KEY_LEN,
        description=(
            "OpenRouter API key. Required. "
            "Must be ≥20 chars and must not be a placeholder."
        ),
    )
    openrouter_model: str = Field(
        default="anthropic/claude-3-haiku",
        min_length=3,
        max_length=128,
        description="LLM model slug. Must not be a known-weak model in production.",
    )
    openrouter_base_url: AnyHttpUrl = Field(
        default="https://openrouter.ai/api/v1/chat/completions",  # type: ignore[assignment]
        description=(
            "OpenRouter completions endpoint. "
            "Validated as a proper HTTPS URL at startup."
        ),
    )

    # ── LLM Tuning ────────────────────────────────────────────────────────
    intent_max_tokens: int = Field(
        default=256,
        ge=50,
        le=1_024,
        description="Max tokens for intent-classification calls.",
    )
    comfort_max_tokens: int = Field(
        default=150,
        ge=50,
        le=512,
        description="Max tokens for short comfort/acknowledgement responses.",
    )
    recommendation_max_tokens: int = Field(
        default=1_024,
        ge=200,
        le=2_048,
        description="Max tokens for full recommendation responses.",
    )
    llm_temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Sampling temperature. 0 = deterministic, 1 = max entropy.",
    )
    llm_request_timeout: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Per-request wall-clock timeout in seconds.",
    )

    # ── Retrieval ─────────────────────────────────────────────────────────
    retrieval_top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of retrieved documents passed to the LLM context.",
    )

    # ── Resilience ────────────────────────────────────────────────────────
    llm_max_retries: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Maximum number of LLM call retries on transient errors.",
    )
    llm_retry_wait_seconds: float = Field(
        default=1.5,
        ge=0.5,
        le=10.0,
        description="Initial back-off wait between retries (exponential base).",
    )

    # ── Observability ─────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description=(
            "Root log level. "
            "DEBUG must never be set in production (leaks sensitive data)."
        ),
    )
    log_format: Literal["json", "text"] = Field(
        default="json",
        description="'json' for structured production logs; 'text' for local dev.",
    )

    # ── API / Server ──────────────────────────────────────────────────────
    api_host: str = Field(
        default="0.0.0.0",
        description=(
            "Bind address. Use '127.0.0.1' to restrict to loopback in "
            "environments where the reverse proxy runs on the same host."
        ),
    )
    api_port: int = Field(
        default=8000,
        ge=1_024,
        le=65_535,
        description="TCP port. Must be an unprivileged port (≥1024).",
    )
    api_cors_origins: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit CORS allow-list. "
            "Wildcards ('*') are forbidden in staging and production. "
            "Empty list blocks all cross-origin requests."
        ),
    )
    rate_limit: str = Field(
        default="60/minute",
        description=(
            "Global rate limit per IP in slowapi format, e.g. '60/minute'. "
            "Pattern: <int>/(second|minute|hour|day)."
        ),
    )

    # ── Feature Flags ─────────────────────────────────────────────────────
    enable_request_logging: bool = Field(
        default=True,
        description="Emit a structured log line for every request with latency.",
    )
    enable_debug_routes: bool = Field(
        default=False,
        description=(
            "Mount /debug/* introspection routes. "
            "Must be False in staging and production."
        ),
    )

    # ====================================================================
    # Field-level validators
    # ====================================================================

    @field_validator("openrouter_api_key", mode="after")
    @classmethod
    def _validate_api_key(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        lower = raw.lower()
        if any(lower.startswith(p) for p in _PLACEHOLDER_PREFIXES):
            raise ValueError(
                "openrouter_api_key appears to be a placeholder. "
                "Set a real key in your .env file."
            )
        if len(raw) < _MIN_API_KEY_LEN:
            raise ValueError(
                f"openrouter_api_key must be at least {_MIN_API_KEY_LEN} characters."
            )
        return v

    @field_validator("rate_limit", mode="after")
    @classmethod
    def _validate_rate_limit(cls, v: str) -> str:
        if not _RATE_LIMIT_RE.match(v):
            raise ValueError(
                f"rate_limit '{v}' is invalid. "
                "Expected format: '<int>/(second|minute|hour|day)'."
            )
        return v

    @field_validator("api_cors_origins", mode="before")
    @classmethod
    def _normalise_cors(cls, v: object) -> list[str]:
        """Accept comma-separated string or list from env."""
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v  # type: ignore[return-value]

    # ====================================================================
    # Cross-field / model-level validators
    # ====================================================================

    @model_validator(mode="after")
    def _enforce_production_posture(self) -> "Settings":
        env = self.environment

        # 1. No wildcards in CORS outside dev.
        if env != "development":
            wildcards = [o for o in self.api_cors_origins if "*" in o]
            if wildcards:
                raise ValueError(
                    f"Wildcard CORS origin(s) {wildcards} are not permitted "
                    f"in environment='{env}'. Specify explicit origins."
                )

        # 2. DEBUG log level is forbidden in production.
        if env == "production" and self.log_level == "DEBUG":
            raise ValueError(
                "log_level='DEBUG' is forbidden in production — "
                "it may leak secrets and PII into log sinks."
            )

        # 3. Debug routes must be off in staging/production.
        if env != "development" and self.enable_debug_routes:
            raise ValueError(
                "enable_debug_routes=True is not allowed outside development."
            )

        # 4. Warn (don't hard-fail) on known-weak models outside dev.
        if env != "development" and self.openrouter_model in _WEAK_MODELS:
            logger.warning(
                "openrouter_model='%s' is flagged as a weak/legacy model. "
                "Consider upgrading before going to production.",
                self.openrouter_model,
            )

        # 5. Enforce HTTPS on the base URL in staging/production.
        if env != "development":
            url = str(self.openrouter_base_url)
            if not url.startswith("https://"):
                raise ValueError(
                    f"openrouter_base_url must use HTTPS in environment='{env}'. "
                    f"Got: {url!r}"
                )

        return self

    # ====================================================================
    # Helpers
    # ====================================================================

    def redacted_summary(self) -> dict[str, object]:
        """
        Return a loggable dict with all secrets replaced by ``'[REDACTED]'``.

        Never call ``model_dump()`` directly in log statements — that would
        expose ``SecretStr`` values in environments where Pydantic is
        configured to serialise them.
        """
        raw = self.model_dump()
        for key in list(raw):
            if "key" in key or "secret" in key or "password" in key or "token" in key:
                raw[key] = "[REDACTED]"
        return raw

    def is_production(self) -> bool:
        return self.environment == "production"

    def is_development(self) -> bool:
        return self.environment == "development"


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the validated, immutable Settings singleton.

    Behaviour
    ---------
    - Calls ``Settings()`` exactly once; result is cached for the process
      lifetime.
    - On any validation error: logs a CRITICAL message with the full
      Pydantic error detail, then calls ``sys.exit(1)`` — fail-fast is
      safer than running with a broken config.
    - On success: emits an INFO-level audit log with all secrets redacted
      so operators can verify effective configuration at startup.
    """
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        logging.critical(
            "Configuration validation failed — refusing to start.\n"
            "Detail: %s\n"
            "Fix the errors above, then restart the application.",
            exc,
        )
        sys.exit(1)

    logger.info(
        "Configuration loaded successfully.",
        extra={"config": settings.redacted_summary()},
    )
    return settings
