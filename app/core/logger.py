"""
Centralised structured logging for MumzWorld AI.

Supports two output modes, controlled by config.log_format:
  - "text"  — human-readable for local development
  - "json"  — structured JSON for production log aggregators (Datadog, ELK, etc.)

Enterprise hardening:
  - Idempotent configuration (safe to call multiple times)
  - PII scrubbing processor (redacts sensitive fields before emission)
  - Request-ID / trace-ID injection via contextvars
  - Sampling for high-volume DEBUG logs (avoids log-flood in production)
  - Uncaught-exception hook (last-resort structured log before crash)
  - Thread-safe, import-time guard against double-configuration
  - Noisy third-party logger suppression list
  - Validates log_level / log_format at startup — fail-fast on misconfiguration

Usage:
    from app.core.logger import get_logger, configure_logging
    configure_logging(log_level="INFO", log_format="json")

    log = get_logger(__name__)
    log.info("pipeline_complete", intent="postpartum_care", confidence=0.93)

    # Bind request-scoped fields for the lifetime of one request:
    from app.core.logger import bind_request_context, clear_request_context
    bind_request_context(request_id="req-abc123", user_id="u-999")
    ...
    clear_request_context()
"""

from __future__ import annotations

import logging
import random
import sys
import threading
import traceback
from typing import Any, Final

import structlog
from structlog.types import EventDict, WrappedLogger

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_VALID_FORMATS: Final = frozenset({"text", "json"})
_VALID_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# Fields to redact in every log record before emission.
# Extend this set — never shrink it.
_PII_FIELDS: Final = frozenset({
    "password", "passwd", "secret", "token", "api_key", "authorization",
    "access_token", "refresh_token", "credit_card", "card_number", "cvv",
    "ssn", "national_id", "phone", "email", "dob", "date_of_birth",
    "mother_name", "baby_name",           # domain-specific PII for MumCare
})

_REDACTED: Final = "[REDACTED]"

# Third-party loggers that spam at INFO/DEBUG and should be muted in prod.
_NOISY_LOGGERS: Final = (
    "uvicorn.access",
    "uvicorn.error",
    "httpx",
    "httpcore",
    "openai",
    "anthropic",
    "boto3",
    "botocore",
    "urllib3",
    "multipart",
)

# ─────────────────────────────────────────────────────────────────────────────
# Module-level state
# ─────────────────────────────────────────────────────────────────────────────

_configure_lock = threading.Lock()
_configured = False          # idempotency guard


# ─────────────────────────────────────────────────────────────────────────────
# Processors
# ─────────────────────────────────────────────────────────────────────────────

def _add_severity(
    _logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """
    Map structlog level names → Google Cloud / Datadog / OTel severity labels.

    Kept as a standalone function (not a lambda) so it is pickle-safe and
    shows up with a meaningful name in tracebacks.
    """
    _SEVERITY_MAP: dict[str, str] = {
        "debug":    "DEBUG",
        "info":     "INFO",
        "warning":  "WARNING",
        "error":    "ERROR",
        "critical": "CRITICAL",
    }
    event_dict["severity"] = _SEVERITY_MAP.get(method_name, "DEFAULT")
    return event_dict


def _scrub_pii(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """
    Redact known PII fields anywhere in the top-level event dict.

    Case-insensitive key matching.  Nested dicts are intentionally *not*
    recursed into — log callers must not embed PII in nested structures.
    If they do, add a recursive variant here and update this docstring.
    """
    for key in list(event_dict.keys()):
        if key.lower() in _PII_FIELDS:
            event_dict[key] = _REDACTED
    return event_dict


class _DebugSampler:
    """
    Drop a configurable fraction of DEBUG records to prevent log floods.

    All non-DEBUG records pass through unconditionally.
    """

    def __init__(self, rate: float = 1.0) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"sample_rate must be in [0.0, 1.0], got {rate!r}")
        self._rate = rate

    def __call__(
        self, _logger: WrappedLogger, method_name: str, event_dict: EventDict
    ) -> EventDict:
        if method_name == "debug" and random.random() > self._rate:
            raise structlog.DropEvent()
        return event_dict


def _add_caller_info(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """
    Inject ``module`` into the event dict for JSON log aggregators that do
    not surface Python's ``name`` field natively (e.g., some Datadog parsers).
    """
    # structlog already passes the logger name; surface it explicitly.
    event_dict.setdefault("module", event_dict.get("_record", {}).get("name", ""))
    return event_dict


# ─────────────────────────────────────────────────────────────────────────────
# Request-context helpers (contextvars-based — async-safe)
# ─────────────────────────────────────────────────────────────────────────────

def bind_request_context(**fields: Any) -> None:
    """
    Bind key/value pairs to the current async/thread context.

    All subsequent log calls in the same task/thread will include these fields
    automatically.  Typical usage in a FastAPI middleware::

        bind_request_context(request_id=req.headers.get("X-Request-ID"))
    """
    structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    """Clear all context-var bindings for the current task/thread."""
    structlog.contextvars.clear_contextvars()


# ─────────────────────────────────────────────────────────────────────────────
# Uncaught-exception hook
# ─────────────────────────────────────────────────────────────────────────────

def _uncaught_exception_handler(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: Any,
) -> None:
    """
    Replace the default ``sys.excepthook`` so unhandled exceptions are emitted
    as structured JSON rather than plain tracebacks.

    KeyboardInterrupt is re-raised normally (don't swallow Ctrl-C).
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    _emergency = structlog.get_logger("app.core.logger.uncaught")
    _emergency.critical(
        "uncaught_exception",
        exc_type=exc_type.__name__,
        exc_value=str(exc_value),
        traceback="".join(traceback.format_tb(exc_tb)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public configuration entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def configure_logging(
    log_level: str = "INFO",
    log_format: str = "text",
    debug_sample_rate: float = 1.0,
) -> None:
    """
    Configure structlog once at application startup.  Idempotent.

    Call this exactly once from ``app/main.py`` before any other code uses the
    logger.  Subsequent calls are silently ignored (thread-safe).

    Args:
        log_level:          Minimum level to emit. One of DEBUG / INFO / WARNING /
                            ERROR / CRITICAL.  Invalid values raise ``ValueError``
                            immediately (fail-fast).
        log_format:         ``"text"`` for coloured dev output, ``"json"`` for
                            production aggregators.  Invalid values raise
                            ``ValueError``.
        debug_sample_rate:  Fraction of DEBUG records to keep (0.0–1.0).
                            Useful in high-throughput services to cut log volume
                            without losing signal at higher levels.
    """
    global _configured  # noqa: PLW0603

    # ── Fail-fast validation ───────────────────────────────────────────────
    _level_upper = log_level.upper()
    if _level_upper not in _VALID_LEVELS:
        raise ValueError(
            f"Invalid log_level {log_level!r}. Must be one of {sorted(_VALID_LEVELS)}."
        )
    if log_format not in _VALID_FORMATS:
        raise ValueError(
            f"Invalid log_format {log_format!r}. Must be one of {sorted(_VALID_FORMATS)}."
        )

    # ── Idempotency guard ─────────────────────────────────────────────────
    with _configure_lock:
        if _configured:
            return
        _configured = True

    # ── Processor chain ────────────────────────────────────────────────────
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,        # request-scoped fields
        structlog.processors.add_log_level,             # level field
        _add_severity,                                  # severity field (GCP/DD)
        _scrub_pii,                                     # redact PII before anything else
        _DebugSampler(rate=debug_sample_rate),          # drop excess DEBUG records
        structlog.processors.TimeStamper(fmt="iso"),    # ISO-8601 timestamp
        structlog.processors.StackInfoRenderer(),       # stack_info kwarg support
        structlog.processors.ExceptionRenderer(),       # exc_info kwarg support
        structlog.processors.UnicodeDecoder(),          # safe bytes → str
    ]

    if log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, _level_upper, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # ── Suppress noisy third-party loggers ────────────────────────────────
    _suppress_level = logging.WARNING if log_format == "json" else logging.INFO
    for _name in _NOISY_LOGGERS:
        logging.getLogger(_name).setLevel(_suppress_level)

    # ── Install uncaught-exception hook ───────────────────────────────────
    sys.excepthook = _uncaught_exception_handler

    # Emit a startup record so operators can confirm the log config in use.
    _boot_logger = get_logger("app.core.logger")
    _boot_logger.info(
        "logging_configured",
        log_level=_level_upper,
        log_format=log_format,
        debug_sample_rate=debug_sample_rate,
        pii_fields_scrubbed=len(_PII_FIELDS),
        noisy_loggers_suppressed=len(_NOISY_LOGGERS),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public factory
# ─────────────────────────────────────────────────────────────────────────────

def get_logger(name: str) -> structlog.BoundLogger:
    """
    Return a bound structlog logger for the given module name.

    Typical usage::

        log = get_logger(__name__)
        log.info("event_name", key="value")
    """
    return structlog.get_logger(name)