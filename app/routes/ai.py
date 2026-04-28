"""
Production-grade FastAPI route for the MumCare AI pipeline.

Features:
  - Full request/response logging with latency tracking
  - HTTPException with structured error bodies
  - Dependency-injected settings (testable)
  - Short-circuit on uncertainty to skip expensive LLM calls
  - Comprehensive docstring visible in auto-generated Swagger UI
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status

from app.core.schema import AIResponse, ErrorResponse, QueryRequest
from app.core.logger import get_logger
from app.core.validation import sanitize_input
from app.services.guidance import generate_guidance
from app.services.intent_parser import extract_intent, generate_comfort_message
from app.services.generator import generate_recommendations
from app.services.retriever import retrieve_products
from app.services.validator import format_response, validate_response

log = get_logger(__name__)

router = APIRouter(prefix="/ai", tags=["AI Pipeline"])


def _create_error_response(status_code: int, detail: str, request_id: Optional[str] = None) -> HTTPException:
    """Create a structured error response with request ID for tracking."""
    # Safely handle None request_id
    request_id_str = str(request_id).strip() if request_id else "unknown"
    error_detail = f"Request ID: {request_id_str} | {detail}"
    return HTTPException(status_code=status_code, detail=error_detail)


@router.post(
    "/query",
    response_model=AIResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Empty or invalid query"},
        503: {"model": ErrorResponse, "description": "LLM service unavailable"},
    },
    summary="Process a natural-language query through the MumCare AI pipeline",
    description="""
## MumCare AI Pipeline

Processes a user's natural-language query through a 6-step AI pipeline:

1. **Intent Extraction** — LLM classifies intent and flags uncertainty
2. **Comfort Message** — Bilingual (EN + AR) empathetic message generated in parallel
3. **RAG Retrieval** — Multi-signal scoring retrieves top-k relevant products
4. **Recommendation Generation** — LLM reasons over retrieved products only (no hallucination)
5. **Guidance Layer** — Safety-aware contextual guidance applied
6. **Validation** — Schema validation + safety invariants enforced

### Safety Contract
When `uncertainty=true`, no product recommendations are returned.
Medical emergency keywords trigger an immediate safety response.
""",
)
async def process_query(request: Request, body: QueryRequest) -> AIResponse:
    # Generate request ID for distributed tracing
    request_id = str(uuid.uuid4())[:8]
    query = body.query
    t0 = time.perf_counter()
    client_host = request.client.host if request.client else "unknown"

    log.info(
        "query_received",
        request_id=request_id,
        query_length=len(query),
        client=client_host,
    )

    # Comprehensive input validation and sanitization
    try:
        query = sanitize_input(query, max_length=500)
    except ValueError as exc:
        log.warning("input_validation_failed", request_id=request_id, error=str(exc))
        raise _create_error_response(
            status.HTTP_400_BAD_REQUEST,
            f"Invalid input: {str(exc)}",
            request_id=request_id,
        )

    # ── Step 1: Intent extraction ─────────────────────────────────────────
    try:
        intent_data = await extract_intent(query, correlation_id=request_id)
        intent = intent_data.get("intent", "unknown")
        uncertainty = intent_data.get("uncertainty", False)
        confidence = intent_data.get("confidence", 0.5)
        issue_detected = intent_data.get("issue_detected", query)
        log.debug("intent_extraction_succeeded", request_id=request_id, intent=intent)
    except Exception as exc:
        log.error("intent_extraction_failed", request_id=request_id, error=str(exc))
        raise _create_error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Intent analysis service temporarily unavailable. Please try again.",
            request_id=request_id,
        )

    # ── Step 2: Comfort message (always generated) ────────────────────────
    try:
        comfort_data = await generate_comfort_message(
            issue_detected,
            correlation_id=request_id,
        )
        log.debug("comfort_message_generated", request_id=request_id)
    except Exception as exc:
        log.warning("comfort_message_generation_failed", request_id=request_id, error=str(exc))
        # Safe fallback when comfort message fails
        comfort_data = {
            "en": "We're here to help you find the right products for your needs.",
            "ar": "نحن هنا لمساعدتك في العثور على المنتجات المناسبة لاحتياجاتك."
        }

    # ── Short-circuit for uncertain / medical queries ─────────────────────
    # Skip expensive retrieval + generation when we know we can't help safely
    if uncertainty:
        guidance = generate_guidance(intent, 0, uncertainty=True, query=query)
        response_dict = {
            "query": query,
            "intent": intent,
            "comfort_message": comfort_data,
            "recommendations": [],
            "confidence": confidence,
            "uncertainty": True,
            "guidance": guidance,
        }
        latency_ms = round((time.perf_counter() - t0) * 1000)
        log.info(
            "query_short_circuited",
            request_id=request_id,
            intent=intent,
            reason="uncertainty",
            latency_ms=latency_ms,
        )
        return format_response(validate_response(response_dict))

    # ── Step 3: RAG retrieval ─────────────────────────────────────────────
    try:
        retrieved_matches = retrieve_products(query, intent)
        retrieved_products = [match.product for match in retrieved_matches]
        log.debug(
            "product_retrieval_succeeded",
            request_id=request_id,
            count=len(retrieved_products),
        )
    except Exception as exc:
        log.error("product_retrieval_failed", request_id=request_id, error=str(exc))
        retrieved_products = []

    # ── Step 4: LLM recommendation generation ────────────────────────────
    try:
        recommendation_result = await generate_recommendations(query, retrieved_products)
        recommendations = recommendation_result.recommendations
        rec_confidence = recommendation_result.avg_confidence
        log.debug(
            "recommendations_generated",
            request_id=request_id,
            count=len(recommendations),
            used_fallback=recommendation_result.used_fallback,
        )
    except Exception as exc:
        log.error("recommendation_generation_failed", request_id=request_id, error=str(exc))
        recommendations = []
        rec_confidence = 0.0

    # ── Step 5: Guidance ──────────────────────────────────────────────────
    guidance = generate_guidance(intent, len(recommendations), uncertainty, query)

    # ── Step 6: Validate & format ─────────────────────────────────────────
    # Blend intent confidence and recommendation confidence
    blended_confidence = (confidence * 0.4 + rec_confidence * 0.6) if rec_confidence > 0 else confidence

    response_dict = {
        "query": query,
        "intent": intent,
        "comfort_message": comfort_data,
        "recommendations": recommendations,
        "confidence": round(blended_confidence, 4),
        "uncertainty": uncertainty,
        "guidance": guidance,
    }

    response_dict = validate_response(response_dict)
    result = format_response(response_dict)

    latency = round((time.perf_counter() - t0) * 1000)
    log.info(
        "query_complete",
        request_id=request_id,
        intent=intent,
        recommendations=len(result.recommendations),
        confidence=result.confidence,
        uncertainty=result.uncertainty,
        latency_ms=latency,
    )

    return result
