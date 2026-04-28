"""
Context-aware guidance text for MumCare AI responses.
"""

from __future__ import annotations

from typing import Final

_EMERGENCY_KEYWORDS: Final[tuple[str, ...]] = (
    "severe",
    "emergency",
    "bleeding",
    "pain",
    "infection",
    "fever",
)

_NO_RESULTS_GUIDANCE: Final[dict[str, str]] = {
    "postpartum_care": (
        "I couldn't find specific products for your situation in our catalog. "
        "Please try describing your need differently, or contact our support team for personalized help."
    ),
    "feeding": (
        "No feeding products matched your query. Please try describing your need more specifically, "
        "or check back as we're always expanding our inventory."
    ),
    "baby_care": (
        "I couldn't find matching products for your baby's needs. "
        "Please describe your situation in more detail or contact support."
    ),
    "general": (
        "I couldn't find products matching your request. "
        "Please try a different search or contact our support team."
    ),
    "unknown": (
        "I'm not sure how to help with your request. "
        "Please try rephrasing your question or contact our support team."
    ),
}

_POSITIVE_GUIDANCE: Final[dict[str, str]] = {
    "postpartum_care": (
        "These products are specifically designed to support your postpartum recovery. "
        "Remember to consult with your healthcare provider if symptoms persist."
    ),
    "feeding": (
        "These products can help you with feeding. Remember that every baby is different; "
        "what works for one may not work for another. Feel free to reach out if you need more options."
    ),
    "baby_care": (
        "These products are carefully selected for your baby's needs. "
        "Always follow the age and safety guidelines provided with each product."
    ),
    "general": (
        "We've found these products for you. Feel free to explore more options or reach out for help."
    ),
    "unknown": (
        "These products might be helpful. Please contact our team if you need further assistance."
    ),
}


def generate_guidance(
    intent: str,
    recommendation_count: int,
    uncertainty: bool = False,
    query: str = "",
) -> str:
    """Return guidance appropriate to the current response state."""
    if uncertainty:
        query_lower = query.lower()
        if any(keyword in query_lower for keyword in _EMERGENCY_KEYWORDS):
            return (
                "This sounds urgent. Please contact your healthcare provider or "
                "call emergency services if needed. MumCare AI is not a substitute for medical advice."
            )
        return (
            "This is outside my area of expertise. Please consult with a healthcare provider "
            "or relevant specialist for personalized advice."
        )

    if recommendation_count == 0:
        return _NO_RESULTS_GUIDANCE.get(
            intent,
            "Please contact our support team for assistance.",
        )

    return _POSITIVE_GUIDANCE.get(intent, "We hope these products help!")
