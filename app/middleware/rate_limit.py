"""
Simple in-memory rate limiting middleware for FastAPI.

Implements sliding-window rate limiting per IP address.
Suitable for single-instance deployments. For distributed systems,
use Redis-backed rate limiting via slowapi or similar.

Design:
  - Tracks requests per IP in memory
  - Enforces max_requests per time_window_seconds
  - Removes stale records automatically
  - Minimal overhead (~1-2ms per request)
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.logger import get_logger

log = get_logger(__name__)

# Pre-compiled cleanup interval constant (in seconds)
_CLEANUP_INTERVAL = 300  # Clean up stale IPs every 5 minutes


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    In-memory rate limiting middleware.

    Configuration:
      max_requests: Maximum requests allowed per IP
      time_window_seconds: Time window for rate limit checks
    """

    def __init__(
        self,
        app: ASGIApp,
        max_requests: int = 60,
        time_window_seconds: int = 60,
    ) -> None:
        super().__init__(app)
        self.max_requests = max_requests
        self.time_window = time_window_seconds
        # IP -> list of request timestamps
        self.request_history: dict[str, list[float]] = defaultdict(list)
        # Track last cleanup time to prevent unbounded memory growth
        self.last_cleanup = time.time()

    async def dispatch(self, request: Request, call_next: Callable) -> JSONResponse:  # type: ignore[type-arg]
        # Skip rate limiting for health checks
        if request.url.path in ["/health", "/version", "/docs", "/redoc", "/openapi.json"]:
            return await call_next(request)

        # Extract client IP
        client_ip = request.client.host if request.client else "unknown"

        # Check rate limit
        current_time = time.time()
        request_times = self.request_history[client_ip]

        # Remove requests older than the time window
        request_times[:] = [ts for ts in request_times if current_time - ts < self.time_window]

        # Periodic cleanup: remove IPs with no recent requests (prevents memory leak)
        if current_time - self.last_cleanup > _CLEANUP_INTERVAL:
            self._cleanup_stale_ips(current_time)
            self.last_cleanup = current_time

        # Check if limit exceeded
        if len(request_times) >= self.max_requests:
            log.warning(
                "rate_limit_exceeded",
                client=client_ip,
                requests_in_window=len(request_times),
                max_allowed=self.max_requests,
            )
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": f"Rate limit exceeded: maximum {self.max_requests} requests per {self.time_window} seconds",
                    "retry_after": self.time_window,
                },
            )

        # Record this request
        request_times.append(current_time)

        # Proceed with request
        response = await call_next(request)
        return response

    def _cleanup_stale_ips(self, current_time: float) -> None:
        """
        Remove IPs with no recent activity to prevent unbounded dict growth.
        
        This is called periodically to clean up IP entries that haven't had
        any requests in the past time window, preventing a memory leak from
        diverse IP addresses making one request and never returning.
        """
        stale_ips = [
            ip for ip, timestamps in self.request_history.items()
            if not timestamps or (current_time - timestamps[-1] > self.time_window * 2)
        ]
        for ip in stale_ips:
            del self.request_history[ip]
        if stale_ips:
            log.debug(
                "rate_limit_cleanup",
                ips_removed=len(stale_ips),
                remaining_ips=len(self.request_history),
            )
