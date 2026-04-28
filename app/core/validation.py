"""
Enterprise-grade input validation and sanitization utilities.

Provides:
  - Safe string normalization
  - XSS prevention through HTML escaping
  - Injection attack prevention (SQL, NoSQL, Command, Path traversal, LDAP)
  - Type-safe validators with structured error context
  - Compiled regex caching for performance
  - Audit-ready structured logging

Security model: fail-closed. Any unrecognised or ambiguous input raises
ValueError so callers can decide how to surface the error to the user.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field as dc_field
from functools import lru_cache
from typing import Any, Final

from app.core.logger import get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MAX_LENGTH: Final[int] = 500
_UNICODE_CATEGORIES_BLOCKED: Final[frozenset[str]] = frozenset(
    # Control characters (Cc), Format characters (Cf), Surrogates (Cs)
    ["Cc", "Cf", "Cs"]
)

# ─────────────────────────────────────────────────────────────────────────────
# Structured error context
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ValidationError(ValueError):
    """
    Structured validation error with machine-readable context.

    Attributes:
        message:   Human-readable description of the failure.
        field:     Name of the field that failed validation (if known).
        code:      Short machine-readable error code (e.g. "xss_detected").
        context:   Arbitrary extra metadata for logging / APM.
    """

    message: str
    field: str = ""
    code: str = "validation_error"
    context: dict[str, Any] = dc_field(default_factory=dict)

    # Make ``str(exc)`` behave like a normal ValueError for compatibility.
    def __str__(self) -> str:  # noqa: D105
        return self.message

    # ValueError.__init__ expects a single positional arg; satisfy it.
    def __post_init__(self) -> None:
        super().__init__(self.message)


# ─────────────────────────────────────────────────────────────────────────────
# Compiled security patterns (module-level = compiled once, reused always)
# ─────────────────────────────────────────────────────────────────────────────

# --- SQL injection ---
# Covers UNION-based, boolean-blind, stacked queries, comment sequences,
# stored-proc prefixes, and common operator tricks.
_SQL_INJECTION: re.Pattern[str] = re.compile(
    r"""
    \b(union)\b.*\b(select)\b           # UNION SELECT
    | \b(select|insert|update|delete    # DML with WHERE
         |drop|truncate|alter|create
         |exec|execute|call)\b
      .*\b(where|from|into|set)\b
    | \bor\b\s+['\"]?\s*\d+\s*['\"]?\s*=\s*['\"]?\s*\d   # OR 1=1
    | \band\b\s+['\"]?\s*\d+\s*['\"]?\s*=\s*['\"]?\s*\d  # AND 1=1
    | --[^\r\n]*                         # SQL line comment
    | /\*.*?\*/                          # SQL block comment
    | ;\s*(drop|delete|truncate|insert
           |update|create|alter)\b       # Stacked statement
    | \b(xp_|sp_|fn_|sys\.)             # MSSQL/stored proc prefixes
    | \bsleep\s*\(                       # Time-based blind
    | \bwaitfor\s+delay\b               # MSSQL time-based blind
    | \bload_file\s*\(                  # MySQL file read
    | \binto\s+(outfile|dumpfile)\b     # MySQL file write
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# --- Cross-site scripting ---
# Catches inline scripts, event handlers, javascript: URIs, data: URIs,
# numeric HTML entities, and common encoding tricks.
_XSS: re.Pattern[str] = re.compile(
    r"""
    <\s*script                          # <script …>
    | javascript\s*:                    # javascript: URI
    | vbscript\s*:                      # vbscript: URI
    | data\s*:\s*text/html             # data:text/html URI
    | <\s*(iframe|embed|object|applet  # Dangerous elements
           |meta|link|base|form
           |input|button|svg|math)\b
    | \bon\w+\s*=                       # Event handler attributes
    | eval\s*\(                         # eval()
    | expression\s*\(                   # CSS expression()
    | &#\d+;?                           # Decimal HTML entity
    | &#x[\da-f]+;?                     # Hex HTML entity
    | \\x3[cC]                          # Hex-encoded <
    | \\u003[cC]                        # Unicode-escaped <
    """,
    re.IGNORECASE | re.VERBOSE,
)

# --- OS command injection ---
_CMD_INJECTION: re.Pattern[str] = re.compile(
    r"""
    [;&|`]                              # Shell metacharacters
    | \$\(                              # Command substitution $(…)
    | \$\{                              # Variable expansion ${…}
    | >>\s*\S                           # Append redirect
    | >\s*\S                            # Write redirect
    | \|\s*\w                           # Pipe to command
    """,
    re.VERBOSE,
)

# --- Path traversal ---
_PATH_TRAVERSAL: re.Pattern[str] = re.compile(
    r"""
    \.\.[\\/]                           # ../  or ..\
    | [\\/]\.\.                         # /..  or \..
    | %2e%2e                            # URL-encoded ..
    | %252e                             # Double-encoded .
    """,
    re.IGNORECASE | re.VERBOSE,
)

# --- LDAP injection ---
_LDAP_INJECTION: re.Pattern[str] = re.compile(
    r"[)(\\*\x00]"                      # Special LDAP chars
)

# --- NoSQL / MongoDB injection ---
_NOSQL_INJECTION: re.Pattern[str] = re.compile(
    r"""
    \$where\b                           # $where operator
    | \$ne\b | \$gt\b | \$lt\b         # Comparison operators
    | \$regex\b | \$exists\b           # Other operators
    | \{\s*["']?\$                      # JSON operator object
    """,
    re.IGNORECASE | re.VERBOSE,
)

# --- Medical severity keywords ---
_MEDICAL_SEVERITY: re.Pattern[str] = re.compile(
    r"""
    \b(
      severe\s+pain | heavy\s+bleeding | won['\u2019]?t\s+stop
      | hemorrhage | fever | infection | sepsis | seizure
      | unconscious | emergency | suicidal | self[\s\-]?harm
      | chest\s+pain | can['\u2019]?t\s+breathe | eclampsia
      | pre[\s\-]?eclampsia | postpartum\s+hemorrhage
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────


def _strip_control_characters(text: str) -> str:
    """Remove Unicode control / format / surrogate characters."""
    return "".join(
        ch
        for ch in text
        if unicodedata.category(ch) not in _UNICODE_CATEGORIES_BLOCKED
    )


@lru_cache(maxsize=256)
def _cached_nfc(text: str) -> str:
    """Return NFC-normalised form; result is cached for repeat inputs."""
    return unicodedata.normalize("NFC", text)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def sanitize_input(
    text: str,
    *,
    max_length: int = DEFAULT_MAX_LENGTH,
    field_name: str = "",
    allow_cmd_chars: bool = False,
    check_nosql: bool = False,
    check_ldap: bool = False,
    check_path: bool = False,
) -> str:
    """
    Sanitize and validate user input for safe downstream processing.

    Pipeline (in order):
      1.  Unicode NFC normalisation
      2.  Strip control / format characters
      3.  Strip leading / trailing whitespace
      4.  Enforce non-empty and maximum length constraints
      5.  SQL injection detection
      6.  XSS pattern detection
      7.  OS command injection detection  (opt-in via ``allow_cmd_chars=False``)
      8.  Path traversal detection         (opt-in via ``check_path=True``)
      9.  LDAP injection detection         (opt-in via ``check_ldap=True``)
      10. NoSQL / MongoDB injection         (opt-in via ``check_nosql=True``)
      11. HTML-escape dangerous characters
      12. Collapse internal whitespace

    Args:
        text:             Raw user input.
        max_length:       Hard character limit (default 500).
        field_name:       Field identifier embedded in errors / logs.
        allow_cmd_chars:  Set ``True`` only for fields that legitimately
                          contain shell metacharacters (e.g. file paths on
                          a privileged internal API).
        check_nosql:      Enable NoSQL operator detection.
        check_ldap:       Enable LDAP special-character detection.
        check_path:       Enable path-traversal sequence detection.

    Returns:
        Sanitized, HTML-escaped, whitespace-collapsed string.

    Raises:
        ValidationError: Structured error with ``code`` and ``context``.
    """

    def _raise(code: str, msg: str, **ctx: Any) -> None:
        log.warning(
            "input_validation_failed",
            code=code,
            field=field_name,
            preview=text[:60],
            **ctx,
        )
        raise ValidationError(message=msg, field=field_name, code=code, context=ctx)

    # 1. Unicode normalisation (cached for repeated values like common phrases)
    text = _cached_nfc(text)

    # 2. Strip dangerous control / format characters
    text = _strip_control_characters(text)

    # 3. Strip surrounding whitespace
    text = text.strip()

    # 4. Length / emptiness constraints
    if not text:
        _raise("empty_input", "Input cannot be empty after stripping whitespace.")
    if len(text) > max_length:
        _raise(
            "input_too_long",
            f"Input exceeds maximum length of {max_length} characters.",
            length=len(text),
            max_length=max_length,
        )

    # 5–10. Injection / attack surface checks
    _CHECKS: list[tuple[re.Pattern[str], str, str, bool]] = [
        (_SQL_INJECTION, "sql_injection_detected",
         "Input contains potentially malicious SQL-like patterns.", True),
        (_XSS, "xss_detected",
         "Input contains potentially malicious script patterns.", True),
        (_CMD_INJECTION, "cmd_injection_detected",
         "Input contains shell metacharacters.", not allow_cmd_chars),
        (_PATH_TRAVERSAL, "path_traversal_detected",
         "Input contains path-traversal sequences.", check_path),
        (_LDAP_INJECTION, "ldap_injection_detected",
         "Input contains LDAP special characters.", check_ldap),
        (_NOSQL_INJECTION, "nosql_injection_detected",
         "Input contains NoSQL operator patterns.", check_nosql),
    ]

    for pattern, code, msg, enabled in _CHECKS:
        if enabled and pattern.search(text):
            _raise(code, msg)

    # 11. HTML-escape remaining dangerous characters
    text = html.escape(text, quote=True)

    # 12. Collapse internal whitespace sequences to a single space
    text = " ".join(text.split())

    return text


def validate_confidence(value: Any, *, field_name: str = "confidence") -> float:
    """
    Validate and normalise a confidence score to ``[0.0, 1.0]``.

    Accepts int, float, or numeric string.  Rounds to 4 decimal places.

    Args:
        value:       Raw confidence value.
        field_name:  Field identifier for structured errors.

    Returns:
        Normalised ``float`` in ``[0.0, 1.0]``.

    Raises:
        ValidationError: If value is not numeric or out of range.
    """
    try:
        conf = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            message=f"Invalid confidence value '{value}': cannot convert to float.",
            field=field_name,
            code="invalid_confidence_type",
            context={"raw_value": str(value), "original_error": str(exc)},
        ) from exc

    if not (0.0 <= conf <= 1.0):
        raise ValidationError(
            message=f"Confidence {conf} is outside the valid range [0.0, 1.0].",
            field=field_name,
            code="confidence_out_of_range",
            context={"raw_value": conf},
        )

    return round(conf, 4)


# Immutable lookup; frozenset membership test is O(1)
_VALID_INTENTS: Final[frozenset[str]] = frozenset(
    {
        "postpartum_care",
        "feeding",
        "baby_care",
        "general",
        "unknown",
    }
)


def validate_intent(value: str, *, field_name: str = "intent") -> str:
    """
    Validate that *value* is one of the allowed intent categories.

    Args:
        value:       Raw intent string.
        field_name:  Field identifier for structured errors.

    Returns:
        Lowercased, validated intent category.

    Raises:
        ValidationError: If intent is not in the allowed set.
    """
    normalized = str(value).lower().strip()
    if normalized not in _VALID_INTENTS:
        raise ValidationError(
            message=(
                f"Invalid intent '{value}'. "
                f"Must be one of: {', '.join(sorted(_VALID_INTENTS))}."
            ),
            field=field_name,
            code="invalid_intent",
            context={"received": value, "allowed": sorted(_VALID_INTENTS)},
        )
    return normalized


_VALID_LANGUAGE_CODES: Final[frozenset[str]] = frozenset({"en", "ar"})


def validate_language_code(code: str, *, field_name: str = "language") -> str:
    """
    Validate that *code* is a supported BCP-47 language tag.

    Currently supported: ``en``, ``ar``.

    Args:
        code:        Raw language code.
        field_name:  Field identifier for structured errors.

    Returns:
        Lowercase validated language code.

    Raises:
        ValidationError: If the language code is not supported.
    """
    normalized = str(code).lower().strip()
    if normalized not in _VALID_LANGUAGE_CODES:
        raise ValidationError(
            message=(
                f"Unsupported language '{code}'. "
                f"Supported codes: {', '.join(sorted(_VALID_LANGUAGE_CODES))}."
            ),
            field=field_name,
            code="unsupported_language",
            context={"received": code, "allowed": sorted(_VALID_LANGUAGE_CODES)},
        )
    return normalized


def validate_non_empty_string(
    value: Any,
    *,
    field_name: str = "value",
    max_length: int = DEFAULT_MAX_LENGTH,
    min_length: int = 1,
) -> str:
    """
    Validate that *value* is a non-empty string within length bounds.

    Args:
        value:       Input to validate.
        field_name:  Field identifier for structured errors.
        max_length:  Upper bound on string length.
        min_length:  Lower bound on string length (default 1).

    Returns:
        Stripped string.

    Raises:
        ValidationError: If the value fails type or length checks.
    """
    if not isinstance(value, str):
        raise ValidationError(
            message=f"Expected a string for '{field_name}', got {type(value).__name__}.",
            field=field_name,
            code="invalid_type",
            context={"received_type": type(value).__name__},
        )
    stripped = value.strip()
    length = len(stripped)
    if length < min_length:
        raise ValidationError(
            message=f"'{field_name}' must be at least {min_length} character(s).",
            field=field_name,
            code="string_too_short",
            context={"length": length, "min_length": min_length},
        )
    if length > max_length:
        raise ValidationError(
            message=f"'{field_name}' exceeds maximum length of {max_length}.",
            field=field_name,
            code="string_too_long",
            context={"length": length, "max_length": max_length},
        )
    return stripped


def validate_positive_integer(
    value: Any,
    *,
    field_name: str = "value",
    min_value: int = 1,
    max_value: int | None = None,
) -> int:
    """
    Validate that *value* is a positive integer within optional bounds.

    Args:
        value:       Input to validate (int or numeric string).
        field_name:  Field identifier for structured errors.
        min_value:   Inclusive lower bound (default 1).
        max_value:   Optional inclusive upper bound.

    Returns:
        Validated integer.

    Raises:
        ValidationError: On type mismatch or out-of-bounds value.
    """
    try:
        int_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            message=f"'{field_name}' must be an integer, got '{value}'.",
            field=field_name,
            code="invalid_integer",
            context={"raw_value": str(value), "original_error": str(exc)},
        ) from exc

    if int_value < min_value:
        raise ValidationError(
            message=f"'{field_name}' must be >= {min_value}, got {int_value}.",
            field=field_name,
            code="integer_too_small",
            context={"value": int_value, "min_value": min_value},
        )
    if max_value is not None and int_value > max_value:
        raise ValidationError(
            message=f"'{field_name}' must be <= {max_value}, got {int_value}.",
            field=field_name,
            code="integer_too_large",
            context={"value": int_value, "max_value": max_value},
        )
    return int_value


def is_medical_severity_query(query: str) -> bool:
    """
    Return ``True`` if *query* contains medical-emergency language.

    Used to flag uncertain or unsafe queries that require immediate
    expert triage before any automated response is sent.

    Args:
        query: User query string (raw, unsanitized is fine here).

    Returns:
        ``True`` if emergency keywords are detected; ``False`` otherwise.
    """
    return bool(_MEDICAL_SEVERITY.search(query))