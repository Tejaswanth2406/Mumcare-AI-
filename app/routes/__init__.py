"""
FastAPI route definitions.

Exposes the REST API surfaces for both web application integration and
mobile app consumption. All endpoints return strictly validated JSON
compliant with the application schemas.
"""

from .ai import router as ai_router

__all__ = ["ai_router"]
