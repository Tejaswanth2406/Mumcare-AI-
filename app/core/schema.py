"""
Enterprise-grade Pydantic v2 schemas for MumzWorld AI.

Design principles
─────────────────
• Every public boundary is fully typed and validated at the edge.
• Auth layer supports both short-lived JWTs and hashed API keys so callers
  can choose a credential style without touching business logic.
• Every request carries a correlation ID for end-to-end tracing.
• Every response is wrapped in a typed envelope so clients never have to
  inspect HTTP status codes to discover whether a call succeeded.
• The error hierarchy maps 1-to-1 with HTTP semantics AND carries a
  machine-readable error_code so front-ends can localise messages.
• Sensitive fields (raw tokens, hashed secrets) are redacted from
  model_dump() / __repr__ via Pydantic's SecretStr.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, Generic, List, Optional, TypeVar

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
# GenericModel is not needed in Pydantic v2 for generics


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_correlation_id() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# Enums — single source of truth for controlled vocabularies
# ─────────────────────────────────────────────────────────────────────────────

class IntentCategory(str, Enum):
    POSTPARTUM_CARE = "postpartum_care"
    FEEDING = "feeding"
    BABY_CARE = "baby_care"
    GENERAL = "general"
    UNKNOWN = "unknown"


class TokenType(str, Enum):
    BEARER = "bearer"
    API_KEY = "api_key"


class ApiKeyScope(str, Enum):
    """Least-privilege scopes carried on every API key."""
    READ_ONLY = "read_only"       # GET endpoints only
    QUERY = "query"               # submit queries, read responses
    ADMIN = "admin"               # key management, audit logs


class ErrorCode(str, Enum):
    """
    Machine-readable error codes.
    HTTP status → ErrorCode:
      400 → VALIDATION_ERROR, MALFORMED_REQUEST
      401 → UNAUTHENTICATED
      403 → FORBIDDEN, SCOPE_INSUFFICIENT
      404 → NOT_FOUND
      409 → CONFLICT
      422 → BUSINESS_RULE_VIOLATION
      429 → RATE_LIMITED
      500 → INTERNAL_ERROR
      503 → SERVICE_UNAVAILABLE
    """
    # 4xx client errors
    VALIDATION_ERROR         = "VALIDATION_ERROR"
    MALFORMED_REQUEST        = "MALFORMED_REQUEST"
    UNAUTHENTICATED          = "UNAUTHENTICATED"
    FORBIDDEN                = "FORBIDDEN"
    SCOPE_INSUFFICIENT       = "SCOPE_INSUFFICIENT"
    NOT_FOUND                = "NOT_FOUND"
    CONFLICT                 = "CONFLICT"
    BUSINESS_RULE_VIOLATION  = "BUSINESS_RULE_VIOLATION"
    RATE_LIMITED             = "RATE_LIMITED"
    # 5xx server errors
    INTERNAL_ERROR           = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE      = "SERVICE_UNAVAILABLE"
    UPSTREAM_TIMEOUT         = "UPSTREAM_TIMEOUT"


# ─────────────────────────────────────────────────────────────────────────────
# Generic type variable used by the response envelope
# ─────────────────────────────────────────────────────────────────────────────

DataT = TypeVar("DataT")


# ─────────────────────────────────────────────────────────────────────────────
# Base model — shared config applied to every schema in this module
# ─────────────────────────────────────────────────────────────────────────────

class _Base(BaseModel):
    model_config = {
        # Reject unknown fields — prevents payload stuffing attacks.
        "extra": "forbid",
        # Validate even when a field value is mutated after construction.
        "validate_assignment": True,
        # Make frozen copies trivially hashable (useful for caching layers).
        "frozen": False,
        # Serialize enums as their .value not their label.
        "use_enum_values": True,
        # Populate by field name AND by alias (for camelCase ↔ snake_case).
        "populate_by_name": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Auth — JWT
# ─────────────────────────────────────────────────────────────────────────────

class JWTPayload(_Base):
    """
    Claims embedded inside every signed JWT.
    Follows RFC 7519 registered claim names plus private MumCare claims.
    """
    # Standard claims
    sub: str = Field(..., description="Subject — the authenticated user/service ID.")
    iss: str = Field(..., description="Issuer URI (e.g. 'https://auth.mumcare.io').")
    aud: str = Field(..., description="Intended audience (e.g. 'mumcare-api').")
    iat: datetime = Field(default_factory=_utcnow, description="Issued-at timestamp (UTC).")
    exp: datetime = Field(..., description="Expiry timestamp (UTC). Validated on every request.")
    jti: str = Field(
        default_factory=_new_correlation_id,
        description="JWT ID — unique per token, used for revocation.",
    )
    # Private MumCare claims
    scope: List[ApiKeyScope] = Field(
        ...,
        min_length=1,
        description="Scopes granted to this token.",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant ID for multi-tenant deployments. None = platform-level token.",
    )
    is_service_account: bool = Field(
        default=False,
        description="True when this token belongs to a backend service, not a human user.",
    )

    @model_validator(mode="after")
    def _exp_after_iat(self) -> "JWTPayload":
        if self.exp <= self.iat:
            raise ValueError("`exp` must be strictly after `iat`.")
        return self


class TokenResponse(_Base):
    """
    Returned by /auth/token (password flow) and /auth/refresh.
    The raw token is a SecretStr — it will NOT appear in logs or __repr__.
    """
    access_token: SecretStr = Field(..., description="Signed JWT. Keep confidential.")
    refresh_token: Optional[SecretStr] = Field(
        default=None,
        description="Long-lived refresh token. Absent for service-account flows.",
    )
    token_type: TokenType = Field(default=TokenType.BEARER)
    expires_in: int = Field(
        ...,
        gt=0,
        description="Lifetime of the access token in seconds.",
    )
    scope: List[ApiKeyScope] = Field(..., description="Granted scopes (mirrors JWT payload).")


class RefreshRequest(_Base):
    """Body payload for POST /auth/refresh."""
    refresh_token: SecretStr = Field(
        ...,
        description="The refresh token previously issued by /auth/token.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Auth — API keys
# ─────────────────────────────────────────────────────────────────────────────

_API_KEY_PREFIX_RE = re.compile(r"^mk_(?:live|test)_[A-Za-z0-9]{32,}$")


class ApiKeyCreateRequest(_Base):
    """
    Request body for POST /auth/api-keys.
    The caller names the key and chooses its scope; the server returns the
    raw secret exactly once (at creation). Subsequent reads return only the
    hashed digest.
    """
    name: Annotated[
        str,
        Field(
            ...,
            min_length=3,
            max_length=80,
            description="Human-readable label for this key (e.g. 'prod-backend').",
        ),
    ]
    scope: List[ApiKeyScope] = Field(
        ...,
        min_length=1,
        description="Scopes to grant. Apply least-privilege.",
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description="Optional hard expiry. If None the key is non-expiring until revoked.",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Optional free-text note for auditors.",
    )


class ApiKeySecret(_Base):
    """
    Returned ONCE after creation.
    ``raw_key`` is a SecretStr — never persisted; caller must store it securely.
    """
    key_id: str = Field(..., description="Stable identifier for this key (safe to log).")
    raw_key: SecretStr = Field(
        ...,
        description="Full API key. Shown only once. Store in a secrets manager.",
    )
    prefix: str = Field(
        ...,
        description="Public prefix (e.g. 'mk_live_xxxxxx') for UI display.",
    )
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("prefix")
    @classmethod
    def _valid_prefix(cls, v: str) -> str:
        if not _API_KEY_PREFIX_RE.match(v):
            raise ValueError(
                "Key prefix must match mk_(live|test)_<32+ alphanumeric chars>."
            )
        return v


class ApiKeyRecord(_Base):
    """
    Public representation stored in the DB and returned by GET /auth/api-keys.
    The raw secret is NEVER returned after creation — only the BLAKE2b digest.
    """
    key_id: str
    name: str
    prefix: str
    scope: List[ApiKeyScope]
    hashed_key: str = Field(
        ...,
        description="BLAKE2b-256 digest of the raw key. Never reverse-exportable.",
    )
    created_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked: bool = False
    revoked_at: Optional[datetime] = None
    description: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Request tracing / context
# ─────────────────────────────────────────────────────────────────────────────

class RequestContext(_Base):
    """
    Injected by the tracing middleware into every handler.
    Carries the correlation ID that propagates across service calls via the
    X-Correlation-ID header.
    """
    correlation_id: str = Field(
        default_factory=_new_correlation_id,
        description="UUID v4, echoed in every response header and log line.",
    )
    received_at: datetime = Field(
        default_factory=_utcnow,
        description="Server-side wall-clock timestamp at ingress.",
    )
    api_version: str = Field(
        default="v1",
        pattern=r"^v\d+$",
        description="API version segment parsed from the URL path.",
    )
    caller_ip: Optional[str] = Field(
        default=None,
        description="Originating IP after trusted-proxy unwrapping.",
    )
    user_agent: Optional[str] = Field(
        default=None,
        max_length=512,
        description="User-Agent header value.",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description="Tenant extracted from the verified JWT/API key.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rate-limit metadata
# ─────────────────────────────────────────────────────────────────────────────

class RateLimitMeta(_Base):
    """
    Mirrors the standard RateLimit-* response headers (IETF draft-07).
    Embedded in every envelope so clients can self-throttle without parsing
    HTTP headers.
    """
    limit: int = Field(..., ge=1, description="Max requests per window.")
    remaining: int = Field(..., ge=0, description="Requests remaining in the current window.")
    reset_at: datetime = Field(..., description="UTC timestamp when the window resets.")
    retry_after: Optional[int] = Field(
        default=None,
        description="Seconds to wait before retrying (only present when 429 is returned).",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pagination
# ─────────────────────────────────────────────────────────────────────────────

class PaginationMeta(_Base):
    """Cursor-based pagination metadata. Prefer cursors over offsets at scale."""
    page_size: int = Field(..., ge=1, le=100)
    next_cursor: Optional[str] = Field(
        default=None,
        description="Opaque cursor for the next page. Absent when this is the last page.",
    )
    prev_cursor: Optional[str] = Field(
        default=None,
        description="Opaque cursor for the previous page.",
    )
    total: Optional[int] = Field(
        default=None,
        description="Total item count. May be None when an exact count is too expensive.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Canonical response envelope
# ─────────────────────────────────────────────────────────────────────────────

class ResponseMeta(_Base):
    """
    Metadata block present on every API response.
    Never omitted — clients should always be able to read these fields.
    """
    correlation_id: str
    api_version: str
    timestamp: datetime = Field(default_factory=_utcnow)
    duration_ms: Optional[float] = Field(
        default=None,
        description="Server-side processing time in milliseconds.",
    )
    rate_limit: Optional[RateLimitMeta] = None


class Envelope(_Base, Generic[DataT]):
    """
    All successful responses are wrapped in this envelope.

        {
            "ok": true,
            "data": { … },
            "meta": { "correlation_id": "…", … }
        }

    All error responses use ErrorEnvelope instead.
    """
    ok: bool = Field(default=True, description="Always True for 2xx responses.")
    data: DataT
    meta: ResponseMeta
    pagination: Optional[PaginationMeta] = None


# ─────────────────────────────────────────────────────────────────────────────
# Error schemas
# ─────────────────────────────────────────────────────────────────────────────

class FieldError(_Base):
    """Per-field validation failure, compatible with RFC 7807 'Problem Details'."""
    field: str = Field(..., description="Dot-notation field path (e.g. 'query.text').")
    message: str = Field(..., description="Human-readable description of the failure.")
    rejected_value: Optional[Any] = Field(
        default=None,
        description="The value that failed validation (omitted for sensitive fields).",
    )


class ErrorDetail(_Base):
    """
    Structured error payload.  Maps HTTP error semantics onto typed codes
    so front-ends can branch on ``error_code`` without string-matching
    ``detail``.
    """
    error_code: ErrorCode
    message: str = Field(..., description="Developer-facing explanation.")
    user_message: Optional[str] = Field(
        default=None,
        description=(
            "Safe, user-facing string (no stack traces). "
            "If None, fall back to a generic localised message."
        ),
    )
    field_errors: List[FieldError] = Field(
        default_factory=list,
        description="Per-field validation errors. Non-empty only for 400/422.",
    )
    docs_url: Optional[AnyHttpUrl] = Field(
        default=None,
        description="Link to relevant API documentation section.",
    )


class ErrorEnvelope(_Base):
    """
    Returned for every non-2xx response.

        {
            "ok": false,
            "error": { "error_code": "RATE_LIMITED", "message": "…" },
            "meta": { "correlation_id": "…", … }
        }
    """
    ok: bool = Field(default=False)
    error: ErrorDetail
    meta: ResponseMeta


class ErrorResponse(_Base):
    """Simple error response model for backward compatibility with FastAPI exception handlers."""
    error_code: str
    detail: str


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────

class AuditEvent(_Base):
    """
    Immutable audit record written to append-only storage on every
    auth-sensitive operation (key creation/revocation, scope escalation, etc.).
    """
    event_id: str = Field(default_factory=_new_correlation_id)
    event_type: str = Field(
        ...,
        description="Namespaced event name, e.g. 'api_key.created', 'token.revoked'.",
        pattern=r"^[a-z_]+\.[a-z_]+$",
    )
    actor_id: str = Field(..., description="Sub claim of the performing identity.")
    actor_is_service: bool = False
    tenant_id: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    correlation_id: str = Field(default_factory=_new_correlation_id)
    occurred_at: datetime = Field(default_factory=_utcnow)
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary key-value pairs. Never include secrets or PII.",
    )

    model_config = {**_Base.model_config, "frozen": True}  # audit events are immutable


# ─────────────────────────────────────────────────────────────────────────────
# Core domain schemas (MumzWorld AI pipeline)
# ─────────────────────────────────────────────────────────────────────────────

class QueryRequest(_Base):
    """Incoming user query payload."""
    query: Annotated[
        str,
        Field(
            ...,
            min_length=2,
            max_length=500,
            description="Natural-language query from the user.",
            examples=["I have leakage after childbirth"],
        ),
    ]
    locale: str = Field(
        default="en",
        pattern=r"^[a-z]{2}(-[A-Z]{2})?$",
        description="BCP-47 locale tag. Drives language selection for comfort messages.",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Optional opaque session identifier for multi-turn context.",
    )

    @field_validator("query")
    @classmethod
    def _strip_and_normalise(cls, v: str) -> str:
        return " ".join(v.split())

    model_config = {
        **_Base.model_config,
        "json_schema_extra": {
            "example": {
                "query": "I have leakage after childbirth",
                "locale": "en",
            }
        },
    }


class ComfortMessage(_Base):
    en: str = Field(..., description="English comfort message.")
    ar: str = Field(..., description="Arabic comfort message (native, not translated).")


class UsageGuidance(_Base):
    en: str = Field(..., description="Usage instructions in English.")
    ar: str = Field(..., description="Usage instructions in Arabic.")


class ProductRecommendation(_Base):
    product_id: int = Field(..., description="Unique product identifier.")
    product_name: str = Field(..., description="Product display name.")
    why_this_product: str = Field(
        ...,
        max_length=300,
        description="One-line differentiator for this specific condition.",
    )
    reason: str = Field(
        ...,
        max_length=600,
        description="Context-aware reasoning — no generic filler phrases.",
    )
    usage_guidance: UsageGuidance
    review_summary: str = Field(..., description="Aggregated customer sentiment.")
    confidence: float = Field(..., ge=0.0, le=1.0)


class AIResponse(_Base):
    """
    Complete pipeline response.
    Safety contract: uncertain=True ⟹ recommendations is always empty.
    """
    query: str = Field(..., description="Echo of the original user query.")
    intent: IntentCategory
    comfort_message: ComfortMessage
    recommendations: List[ProductRecommendation] = Field(
        default_factory=list,
        max_length=5,
        description="Ordered product recommendations (max 5).",
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    uncertainty: bool
    guidance: Optional[str] = Field(default=None, max_length=800)

    @model_validator(mode="after")
    def _safety_invariant(self) -> "AIResponse":
        if self.uncertainty and self.recommendations:
            self.recommendations = []
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

class DependencyStatus(_Base):
    name: str
    healthy: bool
    latency_ms: Optional[float] = None
    detail: Optional[str] = None


class HealthResponse(_Base):
    """Returned by GET /health (public) and GET /health/detailed (authenticated)."""
    status: str = Field(..., description="'ok' or 'degraded' or 'down'.")
    service: str
    version: str
    uptime_seconds: float
    dependencies: List[DependencyStatus] = Field(
        default_factory=list,
        description="Status of downstream dependencies (DB, vector store, LLM, etc.).",
    )