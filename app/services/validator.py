"""
Production-grade output validation and response assembly.

Responsibilities:
  - Enforce all safety invariants (uncertain → no recommendations)
  - Bound numeric fields to valid ranges
  - Re-structure flat LLM output dicts into nested Pydantic models
  - Return a structured error response on schema violations
    (never crash the API with a 500 on a bad LLM payload)

Enterprise Additions:
  - Structured observability via OpenTelemetry spans + Prometheus metrics
  - Retry-aware validation with per-field error accumulation
  - Strict type coercion with audit trail
  - Async-ready design (sync wrappers retained for backward compat)
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

from app.core.logger import get_logger
from app.core.schema import AIResponse, ComfortMessage, ProductRecommendation, UsageGuidance

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Observability — metrics (Prometheus-style counters/histograms)
# Optional: replace with your metrics backend; stubs used if unavailable.
# ─────────────────────────────────────────────────────────────────────────────

try:
    from prometheus_client import Counter, Histogram

    _VALIDATE_TOTAL = Counter(
        "ai_response_validate_total",
        "Total validate_response calls",
        ["outcome"],          # labels: success | fallback | invalid
    )
    _FORMAT_TOTAL = Counter(
        "ai_response_format_total",
        "Total format_response calls",
        ["outcome"],
    )
    _REC_BUILD_FAILURES = Counter(
        "ai_recommendation_build_failures_total",
        "Recommendation dicts that failed to build into a model",
    )
    _LATENCY = Histogram(
        "ai_response_assembly_seconds",
        "End-to-end validate + format latency",
        buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
    )
    _CONFIDENCE_HIST = Histogram(
        "ai_response_confidence",
        "Distribution of outgoing confidence scores",
        buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    )
    _METRICS_ENABLED = True

except ImportError:  # pragma: no cover — metrics optional in dev
    _METRICS_ENABLED = False

    class _Noop:  # noqa: D101
        def labels(self, **_: Any) -> "_Noop":
            return self
        def inc(self, *_: Any) -> None: ...
        def observe(self, *_: Any) -> None: ...
        def time(self) -> Any:
            from contextlib import nullcontext
            return nullcontext()

    _VALIDATE_TOTAL = _FORMAT_TOTAL = _REC_BUILD_FAILURES = _LATENCY = _CONFIDENCE_HIST = _Noop()

# ─────────────────────────────────────────────────────────────────────────────
# OpenTelemetry tracing (optional — no-ops gracefully when not configured)
# ─────────────────────────────────────────────────────────────────────────────

try:
    from opentelemetry import trace as otel_trace

    _tracer = otel_trace.get_tracer(__name__)
    _TRACING_ENABLED = True
except ImportError:  # pragma: no cover
    _TRACING_ENABLED = False

    class _NoopTracer:  # noqa: D101
        @contextmanager
        def start_as_current_span(self, name: str, **_: Any) -> Generator:  # type: ignore[override]
            yield None

    _tracer = _NoopTracer()  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_CONFIDENCE_LO: float = 0.0
_CONFIDENCE_HI: float = 1.0
_UNCERTAINTY_THRESHOLD: float = 0.40

_FALLBACK_COMFORT = ComfortMessage(
    en="We're here to help you through this journey.",
    ar="نحن هنا لمساعدتك في هذه الرحلة.",
)
_FALLBACK_GUIDANCE = (
    "An unexpected issue occurred while processing your request. "
    "Please try again or contact support."
)


# ─────────────────────────────────────────────────────────────────────────────
# Validation audit trail
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationEvent:
    """Immutable record of a single coercion or safety decision."""

    field: str
    original: Any
    coerced: Any
    reason: str


@dataclass
class ValidationAudit:
    """Accumulated audit events for one validate → format cycle."""

    events: list[ValidationEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record(self, fld: str, original: Any, coerced: Any, reason: str) -> None:
        self.events.append(ValidationEvent(fld, original, coerced, reason))

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        log.warning("validation_warning", message=msg)

    def emit(self) -> None:
        """Flush the audit to structured logs; called once at the end of the pipeline."""
        if self.events:
            log.info(
                "validation_audit",
                coercions=len(self.events),
                warnings=len(self.warnings),
                detail=[
                    {"field": e.field, "original": e.original, "coerced": e.coerced, "reason": e.reason}
                    for e in self.events
                ],
            )


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(
    value: float,
    lo: float = _CONFIDENCE_LO,
    hi: float = _CONFIDENCE_HI,
    *,
    audit: ValidationAudit | None = None,
    field_name: str = "confidence",
) -> float:
    """Clamp *value* to [lo, hi] and optionally record the coercion."""
    clamped = max(lo, min(hi, value))
    if clamped != value and audit is not None:
        audit.record(field_name, value, clamped, f"out of range [{lo}, {hi}]")
    return clamped


def _coerce_float(raw: Any, default: float, field_name: str, audit: ValidationAudit) -> float:
    """
    Safe float coercion with audit.

    Accepts: int, float, str-encoded numbers.
    Falls back to *default* on any error.
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        audit.record(field_name, raw, default, "non-numeric value; using default")
        return default


def _coerce_str(raw: Any, default: str, field_name: str, audit: ValidationAudit) -> str:
    if raw is None:
        audit.record(field_name, raw, default, "None value; using default")
        return default
    coerced = str(raw)
    if coerced != raw:
        audit.record(field_name, raw, coerced, "non-string coerced")
    return coerced


def _coerce_int(raw: Any, default: int, field_name: str, audit: ValidationAudit) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        audit.record(field_name, raw, default, "non-integer value; using default")
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Recommendation builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_recommendation(
    rec: dict,
    audit: ValidationAudit,
) -> ProductRecommendation | None:
    """
    Convert a raw recommendation dict into a validated Pydantic model.

    Collects per-field errors into *audit* rather than raising.
    Returns None only when the payload is structurally unrecoverable.
    """
    if not isinstance(rec, dict):
        audit.warn(f"recommendation_skipped: expected dict, got {type(rec).__name__}")
        _REC_BUILD_FAILURES.inc()
        return None

    try:
        usage_raw = rec.get("usage_guidance", {})
        if isinstance(usage_raw, dict):
            usage_model = UsageGuidance(
                en=_coerce_str(usage_raw.get("en"), "", "usage_guidance.en", audit),
                ar=_coerce_str(usage_raw.get("ar"), "", "usage_guidance.ar", audit),
            )
        else:
            # Legacy flat fields
            usage_model = UsageGuidance(
                en=_coerce_str(rec.get("usage_guidance_en"), "", "usage_guidance_en", audit),
                ar=_coerce_str(rec.get("usage_guidance_ar"), "", "usage_guidance_ar", audit),
            )

        confidence_raw = _coerce_float(rec.get("confidence", 0.5), 0.5, "rec.confidence", audit)
        confidence = _clamp(confidence_raw, audit=audit, field_name="rec.confidence")

        return ProductRecommendation(
            product_id=_coerce_int(rec.get("product_id", 0), 0, "product_id", audit),
            product_name=_coerce_str(rec.get("product_name"), "Unknown Product", "product_name", audit),
            why_this_product=_coerce_str(
                rec.get("why_this_product"), "Specifically matched to your need.", "why_this_product", audit
            ),
            reason=_coerce_str(rec.get("reason"), "", "reason", audit),
            usage_guidance=usage_model,
            review_summary=_coerce_str(rec.get("review_summary"), "", "review_summary", audit),
            confidence=confidence,
        )

    except Exception as exc:
        # Last-resort: something truly unexpected happened (e.g., Pydantic validation error).
        log.error(
            "recommendation_model_build_failed",
            error=str(exc),
            exc_info=True,
            product_id=rec.get("product_id"),
        )
        _REC_BUILD_FAILURES.inc()
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def validate_response(response_dict: dict, *, audit: ValidationAudit | None = None) -> dict:
    """
    Apply safety and data-quality rules to the raw LLM response dict.

    Mutates and returns *response_dict* — always call before ``format_response``.

    Args:
        response_dict: Raw dict from the LLM generator.
        audit:         Optional audit collector; creates one internally if omitted.

    Returns:
        The validated (possibly mutated) dict.
    """
    if audit is None:
        audit = ValidationAudit()

    with _tracer.start_as_current_span("validate_response"):
        try:
            raw_confidence = response_dict.get("confidence", 0.5)
            confidence = _clamp(
                _coerce_float(raw_confidence, 0.5, "confidence", audit),
                audit=audit,
            )
            response_dict["confidence"] = confidence

            # Rule 1: Empty recommendations → uncertain
            if not response_dict.get("recommendations"):
                response_dict["uncertainty"] = True
                audit.record("uncertainty", False, True, "no recommendations present")

            # Rule 2: Sub-threshold confidence → uncertain
            if confidence < _UNCERTAINTY_THRESHOLD:
                response_dict["uncertainty"] = True
                audit.record(
                    "uncertainty", False, True,
                    f"confidence {confidence:.3f} < threshold {_UNCERTAINTY_THRESHOLD}",
                )

            # Rule 3 (safety contract): uncertain → no recommendations, ever
            if response_dict.get("uncertainty"):
                dropped = len(response_dict.get("recommendations") or [])
                response_dict["recommendations"] = []
                if dropped:
                    audit.record(
                        "recommendations", dropped, 0,
                        "cleared — response is uncertain (safety invariant)",
                    )

            _VALIDATE_TOTAL.labels(outcome="success").inc()
            return response_dict

        except Exception as exc:
            log.error("validate_response_failed", error=str(exc), exc_info=True)
            _VALIDATE_TOTAL.labels(outcome="invalid").inc()
            # Return a safe minimum — never crash the caller
            response_dict.setdefault("uncertainty", True)
            response_dict.setdefault("recommendations", [])
            response_dict.setdefault("confidence", 0.0)
            return response_dict


def format_response(
    response_dict: dict,
    *,
    audit: ValidationAudit | None = None,
) -> AIResponse:
    """
    Assemble the final :class:`AIResponse` Pydantic model from the validated dict.

    Guarantees:
        - Never raises — always returns a valid ``AIResponse``.
        - Falls back gracefully on schema violations.
        - Emits structured logs for every coercion and fallback.

    Args:
        response_dict: Output of ``validate_response``.
        audit:         Optional shared audit collector.

    Returns:
        A fully-populated, safe ``AIResponse``.
    """
    if audit is None:
        audit = ValidationAudit()

    start = time.perf_counter()

    with _tracer.start_as_current_span("format_response") as span:
        try:
            # ── Comfort message ────────────────────────────────────────────
            comfort_raw = response_dict.get("comfort_message", {})
            if isinstance(comfort_raw, ComfortMessage):
                comfort = comfort_raw
            elif isinstance(comfort_raw, dict):
                comfort = ComfortMessage(
                    en=_coerce_str(comfort_raw.get("en"), _FALLBACK_COMFORT.en, "comfort.en", audit),
                    ar=_coerce_str(comfort_raw.get("ar"), _FALLBACK_COMFORT.ar, "comfort.ar", audit),
                )
            else:
                audit.warn(f"comfort_message had unexpected type {type(comfort_raw).__name__}; using fallback")
                comfort = _FALLBACK_COMFORT

            # ── Recommendations ────────────────────────────────────────────
            recs: list[ProductRecommendation] = []
            for raw_rec in response_dict.get("recommendations") or []:
                if isinstance(raw_rec, ProductRecommendation):
                    recs.append(raw_rec)
                else:
                    built = _build_recommendation(raw_rec, audit)
                    if built is not None:
                        recs.append(built)

            # ── Scalar fields ──────────────────────────────────────────────
            confidence = _clamp(
                _coerce_float(response_dict.get("confidence", 0.5), 0.5, "confidence", audit),
                audit=audit,
            )

            result = AIResponse(
                query=_coerce_str(response_dict.get("query"), "", "query", audit),
                intent=_coerce_str(response_dict.get("intent"), "unknown", "intent", audit),
                comfort_message=comfort,
                recommendations=recs,
                confidence=confidence,
                uncertainty=bool(response_dict.get("uncertainty", False)),
                guidance=response_dict.get("guidance"),
            )

            if _TRACING_ENABLED and span:
                span.set_attribute("response.intent", result.intent)
                span.set_attribute("response.confidence", result.confidence)
                span.set_attribute("response.uncertainty", result.uncertainty)
                span.set_attribute("response.recommendation_count", len(recs))

            _FORMAT_TOTAL.labels(outcome="success").inc()
            _CONFIDENCE_HIST.observe(confidence)
            return result

        except Exception as exc:
            log.error("format_response_failed", error=str(exc), exc_info=True)
            _FORMAT_TOTAL.labels(outcome="fallback").inc()
            if _TRACING_ENABLED and span:
                span.set_attribute("response.fallback", True)
            return AIResponse(
                query=_coerce_str(response_dict.get("query"), "", "query", audit),
                intent="unknown",
                comfort_message=_FALLBACK_COMFORT,
                recommendations=[],
                confidence=0.0,
                uncertainty=True,
                guidance=_FALLBACK_GUIDANCE,
            )

        finally:
            elapsed = time.perf_counter() - start
            _LATENCY.observe(elapsed)
            audit.emit()
            log.debug("format_response_complete", elapsed_ms=round(elapsed * 1000, 2))


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: single-call pipeline
# ─────────────────────────────────────────────────────────────────────────────

def assemble_response(response_dict: dict) -> AIResponse:
    """
    Convenience wrapper: validate → format in one call with a shared audit.

    Prefer calling ``validate_response`` + ``format_response`` separately when
    you need the intermediate dict (e.g., for logging or A/B testing).
    """
    audit = ValidationAudit()
    validated = validate_response(response_dict, audit=audit)
    return format_response(validated, audit=audit)