"""
Core utilities, configuration, and shared schemas for MumCare AI.

This module provides the foundation for the entire application, including:
  - Strongly typed Pydantic v2 schemas
  - Environment configuration with validation
  - Structured logging setup
"""

from .config import Settings, get_settings
from .logger import configure_logging, get_logger
from .schema import (
    AIResponse,
    ComfortMessage,
    ErrorResponse,
    HealthResponse,
    ProductRecommendation,
    QueryRequest,
    UsageGuidance,
)

__all__ = [
    "Settings",
    "get_settings",
    "configure_logging",
    "get_logger",
    "AIResponse",
    "ComfortMessage",
    "ErrorResponse",
    "HealthResponse",
    "ProductRecommendation",
    "QueryRequest",
    "UsageGuidance",
]
