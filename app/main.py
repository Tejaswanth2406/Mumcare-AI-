"""
Production-grade FastAPI application entry point.

Startup sequence:
  1. Configure structured logging
  2. Validate settings (fail-fast on bad config)
  3. Register middleware (CORS, request logging)
  4. Mount routers
  5. Expose /health and /version endpoints

The application object is importable as `app` for use with uvicorn
and ASGI test clients alike.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.logger import configure_logging, get_logger
from app.core.schema import ErrorResponse, HealthResponse
from app.middleware.rate_limit import RateLimitMiddleware
from app.routes import ai
from app.services.intent_parser import close_session as close_llm_session

# ── Bootstrap ────────────────────────────────────────────────────────────────
settings = get_settings()
configure_logging(log_level=settings.log_level, log_format=settings.log_format)
log = get_logger(__name__)

APP_VERSION = "2.0.0"
START_TIME = time.perf_counter()


# ── Lifespan (startup / shutdown) ────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    app.state.started_at = START_TIME
    log.info(
        "mumcare_ai_starting",
        version=APP_VERSION,
        model=settings.openrouter_model,
        log_level=settings.log_level,
    )
    # Pre-warm the product cache so first request is not slower
    from app.services.retriever import _load_products  # noqa: WPS433
    try:
        _load_products()
        log.info("product_cache_warmed")
    except FileNotFoundError as e:
        log.error("product_cache_failed", error=f"Products file not found: {e}")
        raise RuntimeError(
            f"Cannot start without product catalogue. Ensure products.json exists: {e}"
        ) from e
    except Exception as e:
        log.error("product_cache_failed", error=f"Failed to load products: {e}")
        raise RuntimeError(
            f"Failed to load product catalogue. Check file integrity: {e}"
        ) from e
    yield
    await close_llm_session()
    log.info("mumcare_ai_shutdown")


# ── Application factory ───────────────────────────────────────────────────────
app = FastAPI(
    title="MumzWorld AI",
    description=(
        "AI-native decision engine for Mumzworld — transforms natural-language "
        "queries into grounded, bilingual product recommendations with built-in "
        "safety and uncertainty handling."
    ),
    version=APP_VERSION,
    contact={"name": "MumzWorld AI Team", "url": "https://mumzworld.com"},
    license_info={"name": "Proprietary"},
    openapi_tags=[
        {
            "name": "AI Pipeline",
            "description": "Core recommendation pipeline endpoints.",
        },
        {
            "name": "Operations",
            "description": "Health checks and operational endpoints.",
        },
    ],
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.api_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Rate limiting middleware ───────────────────────────────────────────────────
# Limit requests per IP to prevent abuse and ensure fair resource usage
# Parse rate limit from config (format: "requests/period" e.g. "60/minute")
def _parse_rate_limit(rate_limit_str: str) -> tuple[int, int]:
    """Parse rate limit string like '60/minute' into (max_requests, time_window_seconds)."""
    try:
        parts = rate_limit_str.lower().split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid format: {rate_limit_str}")
        requests = int(parts[0])
        period = parts[1].strip()
        
        period_map = {
            "second": 1,
            "seconds": 1,
            "minute": 60,
            "minutes": 60,
            "hour": 3600,
            "hours": 3600,
            "day": 86400,
            "days": 86400,
        }
        
        if period not in period_map:
            raise ValueError(f"Unknown period: {period}")
        
        return requests, period_map[period]
    except Exception as e:
        log.warning(
            "rate_limit_parse_failed",
            error=str(e),
            fallback="60/60s",
        )
        return 60, 60  # Safe default

max_reqs, time_window = _parse_rate_limit(settings.rate_limit)
app.add_middleware(
    RateLimitMiddleware,
    max_requests=max_reqs,
    time_window_seconds=time_window,
)


# ── Request logging middleware ────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:  # type: ignore[type-arg]
    if not settings.enable_request_logging:
        return await call_next(request)

    t0 = time.perf_counter()
    response: Response = await call_next(request)
    latency = round((time.perf_counter() - t0) * 1000)

    log.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        latency_ms=latency,
        client=request.client.host if request.client else "unknown",
    )
    return response


# ── Global exception handler ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            detail="An unexpected error occurred. Please try again.",
            error_code="INTERNAL_SERVER_ERROR",
        ).model_dump(),
    )


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(ai.router)


# ── Operational endpoints ─────────────────────────────────────────────────────
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Operations"],
    summary="Health check",
)
async def health_check() -> HealthResponse:
    """Lightweight liveness probe — returns 200 when the service is up."""
    started_at = getattr(app.state, "started_at", START_TIME)
    return HealthResponse(
        status="ok",
        service="MumzWorld AI",
        version=APP_VERSION,
        uptime_seconds=round(time.perf_counter() - started_at, 2),
    )


# ── Static Files ───────────────────────────────────────────────────────────────
# Mount static files AFTER API routes so they don't intercept API calls
@app.get(
    "/version",
    tags=["Operations"],
    summary="Version info",
)
async def version() -> dict:  # type: ignore[type-arg]
    """Returns service version and active model."""
    return {
        "version": APP_VERSION,
        "model": settings.openrouter_model,
        "log_level": settings.log_level,
    }


# Mount static files after the operational/API routes so they do not intercept
# exact matches like /version.
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
