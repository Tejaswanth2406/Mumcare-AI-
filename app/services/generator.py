"""
Enterprise-hardened LLM recommendation generator.

Architecture
------------
  1. Input validation  — strict guards before any I/O.
  2. Prompt hardening  — schema enforced twice (prompt + system role).
  3. Async HTTP        — aiohttp with per-request timeout + connection limits.
  4. Retry strategy    — tenacity with jitter; 4xx errors short-circuit.
  5. Output validation — JSON schema, field-level checks, hallucination fence,
                         confidence bounding, prohibited-content scan.
  6. Enrichment        — authoritative DB values always overwrite LLM output.
  7. Observability     — structured logs; secrets never logged; query truncated.
  8. Error contract    — typed return; caller never sees an exception.

Security invariants
-------------------
  • ``openrouter_api_key`` is read via ``SecretStr.get_secret_value()`` exactly
    once per call and never stored in a local variable beyond the header dict.
  • Raw LLM output is scanned for prohibited phrases before enrichment.
  • Product IDs and review summaries always come from the DB, never the LLM.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Final

import aiohttp
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import get_settings
from app.core.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_QUERY_LOG_MAX_CHARS: Final[int] = 120
_MAX_PRODUCTS_IN_CONTEXT: Final[int] = 20       # guard against prompt bloat
_MAX_RECOMMENDATIONS: Final[int] = 10           # cap LLM output list length
_CONFIDENCE_FLOOR: Final[float] = 0.0
_CONFIDENCE_CEIL: Final[float] = 1.0
_CONFIDENCE_DEFAULT: Final[float] = 0.5
_MIN_REASON_CHARS: Final[int] = 30              # reject empty/trivial reasons

# Required keys in every LLM recommendation object.
_REQUIRED_REC_KEYS: Final[frozenset[str]] = frozenset({
    "product_name",
    "why_this_product",
    "reason",
    "usage_guidance_en",
    "usage_guidance_ar",
    "confidence",
})

# Phrases that indicate the LLM is hallucinating medical authority.
_PROHIBITED_CONTENT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(diagnos|prescri|medically proven|clinically tested|"
    r"guaranteed|cure|100\s*%\s*(safe|effective)|you must (take|use|stop))\b",
    re.IGNORECASE,
)

# Aiohttp connector — created lazily to avoid event loop issues at import time.
# See: https://docs.aiohttp.org/en/stable/connector.html
_CONNECTOR: aiohttp.TCPConnector | None = None

def _get_connector() -> aiohttp.TCPConnector:
    """
    Get or create the aiohttp connector.
    
    Created lazily (on first use) to avoid requiring an event loop at import time.
    """
    global _CONNECTOR
    if _CONNECTOR is None:
        _CONNECTOR = aiohttp.TCPConnector(
            limit=10,
            limit_per_host=5,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
    return _CONNECTOR

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT: Final[str] = (
    "You are an expert AI recommendation engine for Mumzworld — "
    "a maternal and infant care e-commerce platform. "
    "You ONLY recommend products from the list provided. "
    "You NEVER invent product names, IDs, or attributes. "
    "You respond ONLY with a valid JSON array — no markdown, no preamble, "
    "no trailing text, no code fences. "
    "If no products match, respond with exactly: []"
)

_USER_PROMPT_TEMPLATE: Final[str] = """\
User query: "{query}"

Available products (recommend ONLY from this list):
{products_context}

Return a JSON array where each element has EXACTLY these keys:
{{
  "product_name":       "<exact name from the list — no paraphrasing>",
  "why_this_product":   "<one sentence: specific differentiator vs a generic alternative>",
  "reason":             "<2–3 sentences: context-aware reasoning tied to the user's query>",
  "usage_guidance_en":  "<2–3 practical, specific English instructions>",
  "usage_guidance_ar":  "<2–3 practical, specific Arabic instructions — native, not translated>",
  "confidence":         <float 0.0–1.0>
}}

Quality requirements:
  - "why_this_product" must name a concrete differentiator, NOT "great for mothers".
  - "reason" must reference the user's specific situation, NOT generic category copy.
  - Usage guidance must be actionable: NOT "use regularly" or "apply as needed".
  - Arabic guidance must be culturally natural, NOT a word-for-word translation.
  - "review_summary" is intentionally absent — it will be injected from the database.

Confidence thresholds:
  ≥ 0.85  → strong, direct match
  0.60–0.84 → good match with minor caveats
  < 0.60  → weak match (include only if no better option exists)

Anti-patterns:
  ❌ "Helps with leakage"          ✅ "Designed for heavy early-postpartum flow"
  ❌ "Use as directed"             ✅ "Change every 2 hours during the first 48 hours"
  ❌ "Great for mothers"           ✅ "Shaped to fit over stitches without pressure"

If NO products match the query: return exactly []
"""


# ---------------------------------------------------------------------------
# Input / output dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RecommendationResult:
    """
    Typed return value from ``generate_recommendations``.

    Attributes
    ----------
    recommendations:
        Validated, DB-enriched recommendation dicts, ready for the API layer.
    avg_confidence:
        Mean confidence across all returned recommendations (0.0 if empty).
    latency_ms:
        Total wall-clock time for the LLM call(s), in milliseconds.
    used_fallback:
        True when all retries failed and an empty list was returned.
    failure_reason:
        Non-None only when ``used_fallback=True``; a short error code.
    """

    recommendations: list[dict[str, Any]]
    avg_confidence: float
    latency_ms: float
    used_fallback: bool = False
    failure_reason: str | None = field(default=None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _format_products_context(products: list[dict[str, Any]]) -> str:
    """Render the product list into the prompt context block."""
    rows = []
    for p in products[:_MAX_PRODUCTS_IN_CONTEXT]:
        name = p.get("product_name", "").strip()
        category = p.get("category", "").replace("_", " ")
        description = p.get("description", "").strip()
        if name:
            rows.append(f"- {name} [{category}]: {description}")
    return "\n".join(rows)


def _strip_code_fences(raw: str) -> str:
    """Remove markdown code fences that some models emit despite instructions."""
    return (
        raw.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )


def _validate_rec_structure(rec: Any) -> str | None:
    """
    Return an error string if ``rec`` fails structural validation, else None.
    """
    if not isinstance(rec, dict):
        return "not_a_dict"
    missing = _REQUIRED_REC_KEYS - rec.keys()
    if missing:
        return f"missing_keys:{','.join(sorted(missing))}"
    if not isinstance(rec.get("product_name"), str) or not rec["product_name"].strip():
        return "empty_product_name"
    if len(str(rec.get("reason", ""))) < _MIN_REASON_CHARS:
        return "reason_too_short"
    try:
        float(rec["confidence"])
    except (TypeError, ValueError):
        return "invalid_confidence"
    return None


def _enrich_recommendation(
    rec: dict[str, Any],
    matched_product: dict[str, Any],
) -> dict[str, Any]:
    """
    Overwrite LLM-generated fields with authoritative DB values and
    restructure usage guidance into a nested dict.
    """
    enriched = dict(rec)

    # DB values always win — LLM must never fabricate these.
    enriched["product_id"] = matched_product["id"]
    enriched["review_summary"] = matched_product.get("review_summary", "")

    # Bound and normalise confidence.
    raw_conf = enriched.get("confidence", _CONFIDENCE_DEFAULT)
    try:
        enriched["confidence"] = max(
            _CONFIDENCE_FLOOR, min(_CONFIDENCE_CEIL, float(raw_conf))
        )
    except (TypeError, ValueError):
        enriched["confidence"] = _CONFIDENCE_DEFAULT

    # Guarantee why_this_product exists (forward-compat with older model outputs).
    if not str(enriched.get("why_this_product", "")).strip():
        category_label = matched_product.get("category", "").replace("_", " ")
        enriched["why_this_product"] = (
            f"Specifically suited for {category_label} needs."
        )

    # Restructure usage guidance into schema-compliant nested dict.
    enriched["usage_guidance"] = {
        "en": enriched.pop("usage_guidance_en", ""),
        "ar": enriched.pop("usage_guidance_ar", ""),
    }

    return enriched


# ---------------------------------------------------------------------------
# Retry predicate: do NOT retry on 4xx — they won't resolve.
# ---------------------------------------------------------------------------

class _RetryableHTTPError(aiohttp.ClientResponseError):
    """Subclass used as a retry signal for 5xx / network errors only."""


async def _post_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    """
    POST ``payload`` and return parsed JSON.

    Raises ``_RetryableHTTPError`` for 5xx so tenacity retries.
    Raises ``aiohttp.ClientResponseError`` for 4xx — tenacity will NOT retry.
    """
    async with session.post(
        url,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
    ) as resp:
        if resp.status >= 500:
            raise _RetryableHTTPError(
                resp.request_info,
                resp.history,
                status=resp.status,
                message=f"Server error {resp.status}",
            )
        resp.raise_for_status()   # 4xx → raises, not retried
        return await resp.json()


# ---------------------------------------------------------------------------
# Core async generator
# ---------------------------------------------------------------------------

async def generate_recommendations(
    query: str,
    products: list[dict[str, Any]],
) -> RecommendationResult:
    """
    Generate grounded, validated product recommendations via LLM.

    Parameters
    ----------
    query:
        User's natural-language query. Truncated in logs.
    products:
        Pre-retrieved candidate products from the RAG layer.
        At most ``_MAX_PRODUCTS_IN_CONTEXT`` are sent to the LLM.

    Returns
    -------
    RecommendationResult
        Always returns a typed result — never raises. On failure,
        ``used_fallback=True`` and ``recommendations`` is empty.
    """
    t0 = time.monotonic()

    # --- Input validation ---
    if not isinstance(query, str) or not query.strip():
        log.warning("invalid_query_input")
        return RecommendationResult(
            recommendations=[], avg_confidence=0.0, latency_ms=0.0,
            used_fallback=True, failure_reason="invalid_query",
        )

    if not products:
        log.info("no_candidates", query_snippet=query[:_QUERY_LOG_MAX_CHARS])
        return RecommendationResult(
            recommendations=[], avg_confidence=0.0, latency_ms=0.0,
        )

    settings = get_settings()
    products_context = _format_products_context(products)
    product_lookup: dict[str, dict[str, Any]] = {
        p["product_name"]: p for p in products if p.get("product_name")
    }

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        query=query,
        products_context=products_context,
    )

    payload: dict[str, Any] = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": settings.llm_temperature,
        "max_tokens": settings.recommendation_max_tokens,
    }

    headers: dict[str, str] = {
        # Secret is consumed exactly once, inline, and not stored.
        "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://mumzworld.com",
        "X-Title": "MumCare AI",
    }

    # --- Tenacity retry (5xx + network errors only) ---
    @retry(
        retry=retry_if_exception_type(
            (_RetryableHTTPError, aiohttp.ClientConnectorError, aiohttp.ServerTimeoutError)
        ),
        stop=stop_after_attempt(settings.llm_max_retries),
        wait=wait_exponential_jitter(
            initial=settings.llm_retry_wait_seconds,
            max=8.0,
            jitter=0.5,
        ),
        reraise=True,
    )
    async def _call() -> dict[str, Any]:
        async with aiohttp.ClientSession(connector=_get_connector(), connector_owner=False) as session:
            return await _post_with_retry(
                session,
                str(settings.openrouter_base_url),
                headers,
                payload,
                settings.llm_request_timeout,
            )

    failure_reason: str | None = None
    raw_data: dict[str, Any] | None = None

    try:
        raw_data = await _call()
    except RetryError as exc:
        failure_reason = "retries_exhausted"
        log.error(
            "recommendation_retries_exhausted",
            query_snippet=query[:_QUERY_LOG_MAX_CHARS],
            error=str(exc),
        )
    except aiohttp.ClientResponseError as exc:
        failure_reason = f"http_{exc.status}"
        log.error(
            "recommendation_http_error",
            status=exc.status,
            query_snippet=query[:_QUERY_LOG_MAX_CHARS],
        )
    except aiohttp.ClientError as exc:
        failure_reason = type(exc).__name__
        log.error("recommendation_client_error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        failure_reason = "unexpected"
        log.exception("recommendation_unexpected_error", error=str(exc))

    if raw_data is None:
        latency_ms = round((time.monotonic() - t0) * 1_000, 1)
        return RecommendationResult(
            recommendations=[], avg_confidence=0.0,
            latency_ms=latency_ms, used_fallback=True,
            failure_reason=failure_reason,
        )

    # --- Parse LLM response ---
    try:
        raw_text: str = raw_data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("recommendation_malformed_response", error=str(exc))
        latency_ms = round((time.monotonic() - t0) * 1_000, 1)
        return RecommendationResult(
            recommendations=[], avg_confidence=0.0,
            latency_ms=latency_ms, used_fallback=True,
            failure_reason="malformed_api_response",
        )

    clean_text = _strip_code_fences(raw_text)

    try:
        llm_recs: Any = json.loads(clean_text)
    except json.JSONDecodeError as exc:
        log.warning(
            "recommendation_json_parse_failed",
            error=str(exc),
            raw_snippet=clean_text[:200],
        )
        latency_ms = round((time.monotonic() - t0) * 1_000, 1)
        return RecommendationResult(
            recommendations=[], avg_confidence=0.0,
            latency_ms=latency_ms, used_fallback=True,
            failure_reason="json_parse_error",
        )

    if not isinstance(llm_recs, list):
        log.warning("recommendation_not_a_list", actual_type=type(llm_recs).__name__)
        latency_ms = round((time.monotonic() - t0) * 1_000, 1)
        return RecommendationResult(
            recommendations=[], avg_confidence=0.0,
            latency_ms=latency_ms, used_fallback=True,
            failure_reason="not_a_list",
        )

    # --- Per-recommendation validation + enrichment ---
    enriched: list[dict[str, Any]] = []

    for idx, rec in enumerate(llm_recs[:_MAX_RECOMMENDATIONS]):
        struct_error = _validate_rec_structure(rec)
        if struct_error:
            log.warning(
                "recommendation_invalid_structure",
                index=idx,
                reason=struct_error,
            )
            continue

        name: str = rec["product_name"].strip()
        matched = product_lookup.get(name)
        if not matched:
            # LLM hallucinated a product — discard, always.
            log.warning("hallucinated_product_discarded", product_name=name)
            continue

        # Scan concatenated text fields for prohibited content.
        combined_text = " ".join([
            str(rec.get("reason", "")),
            str(rec.get("why_this_product", "")),
            str(rec.get("usage_guidance_en", "")),
        ])
        if _PROHIBITED_CONTENT_RE.search(combined_text):
            log.warning(
                "recommendation_prohibited_content",
                product_name=name,
                index=idx,
            )
            continue

        enriched.append(_enrich_recommendation(rec, matched))

    avg_confidence = (
        sum(r["confidence"] for r in enriched) / len(enriched) if enriched else 0.0
    )
    latency_ms = round((time.monotonic() - t0) * 1_000, 1)

    log.info(
        "recommendations_generated",
        count=len(enriched),
        avg_confidence=round(avg_confidence, 3),
        latency_ms=latency_ms,
        query_snippet=query[:_QUERY_LOG_MAX_CHARS],
    )

    return RecommendationResult(
        recommendations=enriched,
        avg_confidence=round(avg_confidence, 4),
        latency_ms=latency_ms,
    )