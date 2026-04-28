"""
Intent extraction and bilingual comfort message generation.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Final

import aiohttp
from pydantic import BaseModel, Field, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from app.core.config import get_settings
from app.core.logger import get_logger

log = get_logger(__name__)

_VALID_INTENTS: Final[frozenset[str]] = frozenset(
    {"postpartum_care", "feeding", "baby_care", "general", "unknown"}
)
_MAX_QUERY_CHARS: Final[int] = 500
_MAX_ISSUE_CHARS: Final[int] = 200
_CIRCUIT_MAX_FAILURES: Final[int] = 5
_CIRCUIT_COOL_DOWN_S: Final[float] = 30.0
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RETRYABLE = (aiohttp.ClientError, asyncio.TimeoutError)
_GIFT_KEYWORDS: Final[tuple[str, ...]] = ("gift", "starter kit", "new mum", "new mom")
_AMBIGUOUS_SAFETY_PHRASES: Final[tuple[str, ...]] = (
    "feel strange",
    "feels strange",
    "seems off",
    "something seems off",
    "not right",
)

_INTENT_FALLBACK: Final[dict[str, Any]] = {
    "intent": "unknown",
    "issue_detected": "",
    "confidence": 0.4,
    "uncertainty": True,
}

_FALLBACK_EN = (
    "Many mothers experience this during recovery - you're doing better than you think. "
    "Let's find the right products to help you feel more comfortable."
)
_FALLBACK_AR = (
    "تمرّ الكثير من الأمهات بهذه التجربة خلال فترة التعافي - أنتِ تبذلين أفضل مما تعتقدين. "
    "دعينا نجد المنتجات المناسبة لتشعري بتحسّن."
)

_INTENT_PROMPT = """\
You are an expert AI assistant specialised in maternal and infant health product guidance.

Analyse the user query below and return ONLY a strict JSON object - no markdown, no preamble.

User query: {query}

JSON schema to return:
{{
  "intent": "<one of: postpartum_care | feeding | baby_care | general | unknown>",
  "issue_detected": "<concise description of the detected issue in 5-10 words>",
  "confidence": <float 0.0-1.0>,
  "uncertainty": <true | false>
}}

Classification rules:
- postpartum_care  -> physical/emotional recovery after childbirth
- feeding          -> breastfeeding, bottle feeding, pumping, milk supply
- baby_care        -> diapering, sleeping, swaddling, skin care, general infant needs
- general          -> unclear but safe product query
- unknown          -> completely off-topic or uninterpretable

Uncertainty rules (set uncertainty=true when ANY apply):
  1. Query mentions severe pain, heavy bleeding, infection, or symptoms needing immediate care
  2. Query is ambiguous between a medical concern and a product need
  3. Query is entirely off-topic
  4. confidence < 0.45
"""

_COMFORT_EN = """\
You are a compassionate AI assistant for mothers and caregivers.

The user is experiencing: {issue_detected}

Write a warm, personalised comfort message in English (2-3 sentences MAX).
The message MUST:
  1. Normalise the situation (explain it is common and why it happens)
  2. Reassure the user emotionally without being dismissive
  3. Signal that practical help is coming

Do NOT:
  - Give medical advice
  - Open with generic filler like "You're not alone"
  - Exceed 3 sentences

Comfort message (English only, no labels):"""

_COMFORT_AR = """\
أنتِ مساعدة ذكاء اصطناعي متعاطفة مع الأمهات ومقدّمات الرعاية.

المستخدمة تعاني من: {issue_detected}

اكتبي رسالة دافئة وشخصية باللغة العربية (جملتان إلى ثلاث جمل كحد أقصى).
يجب أن تقوم الرسالة بـ:
  1. تطبيع الموقف
  2. طمأنة المستخدمة عاطفياً
  3. الإشارة إلى أن المساعدة في الطريق

لا تقومي بـ:
  - تقديم نصائح طبية
  - تجاوز ثلاث جمل

الرسالة (بالعربية فقط، بدون عناوين):"""


class _IntentLLMResponse(BaseModel):
    model_config = {"extra": "forbid"}

    intent: str = Field(..., description="One of the five recognised intent slugs.")
    issue_detected: str = Field(..., min_length=3, max_length=300)
    confidence: float = Field(..., ge=0.0, le=1.0)
    uncertainty: bool

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        normalised = value.lower().strip()
        if normalised not in _VALID_INTENTS:
            raise ValueError(f"Unrecognised intent '{value}'.")
        return normalised


@dataclass
class LLMCallMeta:
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    correlation_id: str = ""


@dataclass
class _CircuitBreaker:
    failures: int = 0
    opened_at: float = 0.0

    @property
    def is_open(self) -> bool:
        if self.failures >= _CIRCUIT_MAX_FAILURES:
            if time.monotonic() - self.opened_at < _CIRCUIT_COOL_DOWN_S:
                return True
            self.failures = _CIRCUIT_MAX_FAILURES - 1
        return False

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= _CIRCUIT_MAX_FAILURES:
            self.opened_at = time.monotonic()
            log.error(
                "circuit_opened",
                failures=self.failures,
                cool_down_s=_CIRCUIT_COOL_DOWN_S,
            )


_connector: aiohttp.TCPConnector | None = None
_session: aiohttp.ClientSession | None = None
_circuit = _CircuitBreaker()


def _sanitise(text: str, max_chars: int) -> str:
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    return cleaned[:max_chars]


def _apply_rule_overrides(query: str, result: dict[str, Any]) -> dict[str, Any]:
    """Apply deterministic safety/product rules for known edge cases."""
    query_lower = query.lower()
    updated = dict(result)

    if any(keyword in query_lower for keyword in _GIFT_KEYWORDS):
        updated["intent"] = "baby_care"
        updated["uncertainty"] = False
        updated["confidence"] = max(float(updated.get("confidence", 0.0)), 0.85)
        updated["issue_detected"] = "gift for a new mum"

    if any(phrase in query_lower for phrase in _AMBIGUOUS_SAFETY_PHRASES):
        updated["uncertainty"] = True
        updated["confidence"] = min(float(updated.get("confidence", 1.0)), 0.4)

    return updated


def _get_connector() -> aiohttp.TCPConnector:
    global _connector
    if _connector is None:
        _connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=20,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
    return _connector


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(connector=_get_connector())
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


async def _call_llm(
    prompt: str,
    max_tokens: int,
    temperature: float,
    correlation_id: str = "",
) -> tuple[str, LLMCallMeta]:
    settings = get_settings()
    t0 = time.monotonic()

    payload: dict[str, Any] = {
        "model": settings.openrouter_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    async with asyncio.timeout(settings.llm_request_timeout + 2):
        async with _get_session().post(
            str(settings.openrouter_base_url),
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://mumzworld.com",
                "X-Title": "MumzWorld AI",
                "X-Correlation-ID": correlation_id,
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=settings.llm_request_timeout),
        ) as response:
            response.raise_for_status()
            data = await response.json()

    latency_ms = (time.monotonic() - t0) * 1000
    usage = data.get("usage", {})
    meta = LLMCallMeta(
        latency_ms=round(latency_ms, 2),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        model=data.get("model", settings.openrouter_model),
        correlation_id=correlation_id,
    )
    raw_text = data["choices"][0]["message"]["content"].strip()
    return raw_text, meta


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(3),
    wait=wait_random_exponential(multiplier=1, min=1, max=10),
    reraise=False,
)
async def extract_intent(query: str, correlation_id: str = "") -> dict[str, Any]:
    if _circuit.is_open:
        log.warning("circuit_open_fast_fail", correlation_id=correlation_id)
        return _apply_rule_overrides(
            query,
            {**_INTENT_FALLBACK, "issue_detected": _sanitise(query, _MAX_QUERY_CHARS)},
        )

    safe_query = _sanitise(query, _MAX_QUERY_CHARS)
    settings = get_settings()
    prompt = _INTENT_PROMPT.format(query=safe_query)

    try:
        raw, meta = await _call_llm(
            prompt,
            max_tokens=settings.intent_max_tokens,
            temperature=0.2,
            correlation_id=correlation_id,
        )
        _circuit.record_success()
        log.info(
            "llm_call_completed",
            stage="intent",
            latency_ms=meta.latency_ms,
            prompt_tokens=meta.prompt_tokens,
            completion_tokens=meta.completion_tokens,
            model=meta.model,
            correlation_id=correlation_id,
        )
    except _RETRYABLE as exc:
        _circuit.record_failure()
        log.error("llm_network_error", stage="intent", error=str(exc), correlation_id=correlation_id)
        raise
    except Exception as exc:
        _circuit.record_failure()
        log.error("llm_unexpected_error", stage="intent", error=str(exc), correlation_id=correlation_id)
        return _apply_rule_overrides(
            safe_query,
            {**_INTENT_FALLBACK, "issue_detected": safe_query},
        )

    try:
        clean = (
            raw.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        parsed = _IntentLLMResponse.model_validate_json(clean)
    except Exception as exc:
        log.warning("intent_parse_failed", error=str(exc), correlation_id=correlation_id)
        return _apply_rule_overrides(
            safe_query,
            {**_INTENT_FALLBACK, "issue_detected": safe_query},
        )

    return _apply_rule_overrides(safe_query, {
        "intent": parsed.intent,
        "issue_detected": parsed.issue_detected,
        "confidence": parsed.confidence,
        "uncertainty": parsed.uncertainty,
    })


async def generate_comfort_message(
    issue_detected: str,
    correlation_id: str = "",
) -> dict[str, str]:
    safe_issue = _sanitise(issue_detected, _MAX_ISSUE_CHARS)
    settings = get_settings()

    async def _safe_call(prompt: str, lang: str, fallback: str) -> str:
        if _circuit.is_open:
            log.warning(
                "circuit_open_fast_fail",
                stage=f"comfort_{lang}",
                correlation_id=correlation_id,
            )
            return fallback
        try:
            text, meta = await _call_llm(
                prompt,
                max_tokens=settings.comfort_max_tokens,
                temperature=settings.llm_temperature,
                correlation_id=correlation_id,
            )
            _circuit.record_success()
            log.info(
                "llm_call_completed",
                stage=f"comfort_{lang}",
                latency_ms=meta.latency_ms,
                prompt_tokens=meta.prompt_tokens,
                completion_tokens=meta.completion_tokens,
                correlation_id=correlation_id,
            )
            return text
        except Exception as exc:
            _circuit.record_failure()
            log.warning(
                "comfort_generation_failed",
                lang=lang,
                error=str(exc),
                correlation_id=correlation_id,
            )
            return fallback

    en_msg, ar_msg = await asyncio.gather(
        _safe_call(_COMFORT_EN.format(issue_detected=safe_issue), "en", _FALLBACK_EN),
        _safe_call(_COMFORT_AR.format(issue_detected=safe_issue), "ar", _FALLBACK_AR),
    )
    return {"en": en_msg, "ar": ar_msg}
