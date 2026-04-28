"""
Production-grade Streamlit frontend for MumzWorld AI.

Design principles:
  - Premium dark-mode UI with glassmorphism cards
  - Progressive disclosure: basic view → AI transparency expander
  - Graceful degradation: all error states handled visually
  - Bilingual display (EN + AR side-by-side)
  - Real-time streaming spinner with stage feedback
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import requests
import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# Page config — MUST be first Streamlit call
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MumzWorld AI — Smart Maternal Assistant",
    page_icon="🌸",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Load API base URL from environment for production deployments
API_BASE = os.getenv("MUMCARE_API_BASE", "http://localhost:8000")

# ─────────────────────────────────────────────────────────────────────────────
# Premium CSS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* { font-family: 'Inter', sans-serif; box-sizing: border-box; }

/* ── Dark background ── */
.stApp { background: #0f1117; color: #e8eaf0; }

/* ── Hero banner ── */
.hero {
    background: linear-gradient(135deg, #1a1040 0%, #2d1b69 40%, #1a3a5c 100%);
    border-radius: 20px;
    padding: 2.5rem 2rem 2rem;
    margin-bottom: 1.5rem;
    border: 1px solid rgba(180, 130, 255, 0.25);
    box-shadow: 0 8px 40px rgba(120, 60, 220, 0.20);
}
.hero h1 { font-size: 2.4rem; font-weight: 700; color: #fff; margin: 0 0 0.4rem; }
.hero p  { font-size: 1.05rem; color: rgba(255,255,255,0.72); margin: 0; }
.hero .badge {
    display: inline-block; background: rgba(255,255,255,0.12);
    color: #c4b5fd; padding: 0.25rem 0.75rem; border-radius: 999px;
    font-size: 0.78rem; font-weight: 500; margin-bottom: 0.8rem;
    border: 1px solid rgba(180,130,255,0.3);
}

/* ── Input area ── */
.stTextArea textarea {
    background: #1c1f2e !important;
    border: 1.5px solid #3b3f5c !important;
    border-radius: 12px !important;
    color: #e8eaf0 !important;
    font-size: 1rem !important;
    padding: 1rem !important;
    transition: border-color 0.2s;
}
.stTextArea textarea:focus {
    border-color: #7c5cfc !important;
    box-shadow: 0 0 0 3px rgba(124,92,252,0.15) !important;
}

/* ── Primary button ── */
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #7c5cfc, #5b8dee) !important;
    border: none !important; border-radius: 10px !important;
    font-weight: 600 !important; font-size: 1rem !important;
    padding: 0.65rem 1.5rem !important; color: #fff !important;
    transition: all 0.2s !important;
}
.stButton > button[kind="primary"]:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(124,92,252,0.4) !important;
}

/* ── Secondary button ── */
.stButton > button:not([kind="primary"]) {
    background: #1c1f2e !important; color: #a0a8c0 !important;
    border: 1.5px solid #3b3f5c !important; border-radius: 10px !important;
    font-weight: 500 !important; transition: all 0.2s !important;
}
.stButton > button:not([kind="primary"]):hover {
    border-color: #7c5cfc !important; color: #c4b5fd !important;
}

/* ── Comfort message ── */
.comfort-card {
    background: linear-gradient(135deg, rgba(124,92,252,0.12), rgba(91,141,238,0.10));
    border: 1px solid rgba(124,92,252,0.3);
    border-left: 4px solid #7c5cfc;
    border-radius: 14px; padding: 1.4rem 1.6rem;
    margin: 1.2rem 0;
}
.comfort-card .label {
    font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: #a78bfa; margin-bottom: 0.5rem;
}
.comfort-card p { color: #d4d8f0; font-size: 1rem; line-height: 1.65; margin: 0; }

/* ── Uncertainty / safety card ── */
.safety-card {
    background: linear-gradient(135deg, rgba(251,146,60,0.12), rgba(239,68,68,0.08));
    border: 1px solid rgba(251,146,60,0.4);
    border-left: 4px solid #fb923c;
    border-radius: 14px; padding: 1.4rem 1.6rem; margin: 1.2rem 0;
}
.safety-card .label {
    font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: #fb923c; margin-bottom: 0.5rem;
}
.safety-card p { color: #f1d0b8; font-size: 1rem; line-height: 1.65; margin: 0; }

/* ── Product card ── */
.product-card {
    background: #161927;
    border: 1px solid #2a2e45;
    border-radius: 16px; padding: 1.6rem;
    margin: 1rem 0;
    transition: border-color 0.25s, box-shadow 0.25s;
}
.product-card:hover {
    border-color: #7c5cfc;
    box-shadow: 0 4px 24px rgba(124,92,252,0.15);
}
.product-title { font-size: 1.15rem; font-weight: 700; color: #e8eaf0; margin: 0 0 0.3rem; }
.product-why   { font-size: 0.88rem; color: #7c5cfc; font-weight: 500; margin-bottom: 0.9rem; }
.product-reason { font-size: 0.95rem; color: #b0b8d4; line-height: 1.6; margin-bottom: 1rem; }

/* ── Confidence badge ── */
.conf-badge {
    display: inline-block; padding: 0.3rem 0.75rem;
    border-radius: 999px; font-size: 0.8rem; font-weight: 600;
}
.conf-high   { background: rgba(52,211,153,0.15); color: #34d399; border: 1px solid rgba(52,211,153,0.3); }
.conf-medium { background: rgba(251,191,36,0.15); color: #fbbf24; border: 1px solid rgba(251,191,36,0.3); }
.conf-low    { background: rgba(239,68,68,0.15);  color: #ef4444; border: 1px solid rgba(239,68,68,0.3); }

/* ── Guidance box ── */
.guidance-box {
    background: #12151f; border: 1px solid #2a2e45;
    border-radius: 12px; padding: 1.2rem 1.4rem; margin-top: 1rem;
    font-size: 0.95rem; color: #9aa0bf; line-height: 1.7;
}

/* ── Metrics ── */
[data-testid="stMetricValue"] { color: #c4b5fd !important; font-weight: 700 !important; }
[data-testid="stMetricLabel"] { color: #6b7280 !important; font-size: 0.8rem !important; }

/* ── Sidebar ── */
[data-testid="stSidebar"] { background: #12151f !important; border-right: 1px solid #1e2235; }
[data-testid="stSidebar"] .stMarkdown { color: #9aa0bf; }

/* ── Divider ── */
hr { border-color: #1e2235 !important; }

/* ── Expander ── */
.streamlit-expanderHeader { color: #9aa0bf !important; font-weight: 500 !important; }

/* ── Section tag ── */
.section-tag {
    display: inline-block; background: rgba(124,92,252,0.12);
    color: #a78bfa; padding: 0.2rem 0.7rem; border-radius: 6px;
    font-size: 0.78rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.07em; margin-bottom: 0.6rem;
}

/* ── Arabic text ── */
.ar-text { direction: rtl; text-align: right; font-size: 0.93rem; color: #b0b8d4; line-height: 1.7; }

/* ── Example query pill ── */
.example-pill {
    background: #1c1f2e; border: 1px solid #3b3f5c; border-radius: 10px;
    padding: 0.5rem 1rem; margin: 0.3rem 0; cursor: pointer;
    font-size: 0.88rem; color: #9aa0bf; transition: all 0.2s;
}
.example-pill:hover { border-color: #7c5cfc; color: #c4b5fd; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _conf_class(conf: float) -> str:
    if conf >= 0.80:
        return "conf-high"
    if conf >= 0.60:
        return "conf-medium"
    return "conf-low"


def _conf_label(conf: float) -> str:
    pct = int(conf * 100)
    if conf >= 0.80:
        return f"✦ {pct}% Strong Match"
    if conf >= 0.60:
        return f"◆ {pct}% Moderate Match"
    return f"◇ {pct}% Weak Match"


def _call_api(query: str) -> dict | None:
    """
    Call the backend API with comprehensive error handling.

    Handles:
      - Connection errors
      - HTTP error codes (400, 429, 503, etc.)
      - Timeouts
      - JSON parsing errors
      - Unexpected exceptions

    Args:
        query: User's natural-language query

    Returns:
        Parsed JSON response dict, or None on error
    """
    try:
        if not query or not query.strip():
            st.error("❌ Query cannot be empty. Please enter a valid question.")
            return None

        if len(query) > 500:
            st.error("❌ Query is too long (max 500 characters). Please shorten it.")
            return None

        resp = requests.post(
            f"{API_BASE}/ai/query",
            json={"query": query},
            timeout=45,
        )

        # Handle specific HTTP status codes with helpful messages
        if resp.status_code == 400:
            error_msg = resp.json().get("detail", "Invalid query")
            st.error(f"❌ Invalid input: {error_msg}")
            return None
        elif resp.status_code == 413:
            st.error("❌ Your query is too large. Please use fewer characters.")
            return None
        elif resp.status_code == 429:
            st.error(
                "⏳ Rate limit reached. Please wait a moment before submitting again. "
                "(Limit: 60 requests per minute)"
            )
            return None
        elif resp.status_code == 503:
            st.error(
                "🔄 Backend service is temporarily unavailable. "
                "Please try again in a few moments."
            )
            return None
        elif resp.status_code >= 500:
            st.error(
                f"❌ Server error ({resp.status_code}). Our team has been notified. "
                "Please try again shortly."
            )
            return None
        elif resp.status_code >= 400:
            st.error(
                f"❌ Unexpected error ({resp.status_code}). "
                f"Please try rephrasing your question."
            )
            return None

        if resp.status_code == 200:
            try:
                return resp.json()
            except requests.exceptions.JSONDecodeError:
                st.error("❌ Received invalid response format from backend.")
                return None

        st.error(f"❌ Unexpected HTTP {resp.status_code}")
        return None

    except requests.exceptions.Timeout:
        st.error(
            "⏱️ Request timed out (45 seconds). "
            "The backend might be overloaded. Please try again."
        )
        return None

    except requests.exceptions.ConnectionError:
        st.error(
            "❌ Cannot reach the backend. "
            "Make sure FastAPI is running:\n\n"
            "`uvicorn app.main:app --reload`"
        )
        return None

    except requests.exceptions.RequestException as exc:
        st.error(f"❌ Network error: {type(exc).__name__}")
        return None

    except Exception as exc:
        st.error(
            f"❌ Unexpected error: {type(exc).__name__}\n\n"
            f"Details: {str(exc)[:200]}"
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🌸 MumzWorld AI")
    st.markdown(
        "<p style='color:#6b7280;font-size:0.85rem;'>AI-native decision engine for Mumzworld</p>",
        unsafe_allow_html=True,
    )
    st.markdown("---")

    st.markdown("**⚡ How It Works**")
    steps = [
        ("🧠", "Intent Analysis", "AI understands your exact need"),
        ("🔍", "RAG Retrieval", "Fetches matching products from catalogue"),
        ("✨", "LLM Reasoning", "Explains why each product fits"),
        ("🛡️", "Safety Layer", "Flags medical concerns automatically"),
        ("🌍", "Bilingual Output", "EN + AR generated natively"),
    ]
    for icon, title, desc in steps:
        st.markdown(
            f"<div style='margin:0.6rem 0;'>"
            f"<span style='font-size:1.1rem'>{icon}</span> "
            f"<span style='color:#c4b5fd;font-weight:600;font-size:0.88rem'>{title}</span><br>"
            f"<span style='color:#6b7280;font-size:0.8rem;margin-left:1.6rem'>{desc}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown("---")
    st.markdown(
        "<div style='background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.3);"
        "border-radius:10px;padding:0.8rem;font-size:0.82rem;color:#fca5a5;'>"
        "⚕️ <strong>Medical Disclaimer</strong><br>"
        "This tool assists with product selection only. "
        "For medical concerns, always consult a healthcare professional."
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown(
        "<p style='color:#374151;font-size:0.75rem;text-align:center;'>"
        "MumzWorld AI v2.0 · Built for Mumzworld</p>",
        unsafe_allow_html=True,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Hero
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="hero">
  <div class="badge">🌸 AI-Native Decision Engine</div>
  <h1>MumzWorld AI</h1>
  <p>Describe your situation in natural language — get empathetic, grounded product recommendations<br>
     with usage guidance in English & Arabic. No filters. No browsing. Just answers.</p>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Query input
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("#### 💬 What do you need help with?")

# Pre-fill from example click
default_query = st.session_state.get("prefill_query", "")

query = st.text_area(
    label="query_input",
    value=default_query,
    placeholder=(
        "e.g. 'I have leakage after childbirth'  ·  "
        "'My baby is 6 months, bottle feeding help'  ·  "
        "'I have sore nipples from nursing'"
    ),
    height=90,
    label_visibility="collapsed",
    key="query_text",
)

col_submit, col_ex, col_clear = st.columns([3, 2, 1])
with col_submit:
    submit = st.button("🔍 Get Recommendations", type="primary", use_container_width=True)
with col_ex:
    if st.button("💡 Show Examples", use_container_width=True):
        st.session_state["show_ex"] = not st.session_state.get("show_ex", False)
with col_clear:
    if st.button("✕ Clear", use_container_width=True):
        st.session_state.pop("prefill_query", None)
        st.rerun()

# ── Example queries ──────────────────────────────────────────────────────────
EXAMPLES = [
    ("🤱 Postpartum",   "I have leakage and discomfort after childbirth"),
    ("🍼 Feeding",      "My baby is 6 months old and I want to start bottle feeding"),
    ("💊 Nursing Pain", "I have sore and cracked nipples from breastfeeding"),
    ("👶 Baby Care",    "My newborn has a diaper rash and sensitive skin"),
    ("🎁 Gift",         "I want a thoughtful gift pack for a new mum"),
    ("⚠️ Edge Case",    "I feel strange after feeding — something seems off"),
    ("🚨 Safety",       "I have severe pain and heavy bleeding that won't stop"),
    ("❓ Out-of-Scope",  "What is the weather forecast for Dubai tomorrow?"),
]

if st.session_state.get("show_ex"):
    st.markdown("---")
    st.markdown("**Click any example to load it:**")
    cols = st.columns(2)
    for i, (label, ex_query) in enumerate(EXAMPLES):
        if cols[i % 2].button(f"{label}: _{ex_query[:55]}…_" if len(ex_query) > 55 else f"{label}: _{ex_query}_",
                              use_container_width=True, key=f"ex_{i}"):
            st.session_state["prefill_query"] = ex_query
            st.session_state["show_ex"] = False
            st.rerun()
    st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Process & render
# ─────────────────────────────────────────────────────────────────────────────
if submit:
    if not query or not query.strip():
        st.warning("⚠️ Please enter your question above.")
    else:
        with st.spinner("🤔 Analysing your query through the AI pipeline…"):
            data = _call_api(query.strip())

        if data is None:
            st.stop()

        st.markdown("---")

        # ── Safety / uncertainty ──────────────────────────────────────────
        if data.get("uncertainty"):
            st.markdown(
                f"""<div class="safety-card">
                <div class="label">⚠️ Safety Notice</div>
                <p>{data.get("guidance", "Please consult a healthcare professional.")}</p>
                </div>""",
                unsafe_allow_html=True,
            )
            # Still show comfort message even on uncertainty
            cm = data.get("comfort_message", {})
            if cm.get("en"):
                st.markdown(
                    f"""<div class="comfort-card">
                    <div class="label">💝 We hear you</div>
                    <p>{cm.get("en", "")}</p>
                    </div>""",
                    unsafe_allow_html=True,
                )
            st.stop()

        # ── Comfort message ───────────────────────────────────────────────
        cm = data.get("comfort_message", {})
        if cm.get("en"):
            st.markdown(
                f"""<div class="comfort-card">
                <div class="label">💝 Comfort Message</div>
                <p>{cm.get("en", "")}</p>
                </div>""",
                unsafe_allow_html=True,
            )

        # ── Metrics ───────────────────────────────────────────────────────
        intent_label = data.get("intent", "Unknown").replace("_", " ").title()
        confidence   = data.get("confidence", 0)
        num_recs     = len(data.get("recommendations", []))
        assessment   = "✅ Confident" if not data.get("uncertainty") else "⚠️ Uncertain"

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Intent Detected",   intent_label)
        m2.metric("Confidence",         f"{confidence*100:.0f}%")
        m3.metric("Recommendations",    str(num_recs))
        m4.metric("Assessment",         assessment)

        # ── AI transparency expander ──────────────────────────────────────
        with st.expander("🧠 AI Pipeline Transparency"):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"**Intent Category:** `{data.get('intent', 'N/A')}`")
                st.markdown(f"**Confidence Score:** `{confidence:.4f}`")
                st.markdown(f"**Uncertainty Flag:** `{data.get('uncertainty', False)}`")
            with c2:
                st.markdown(f"**Products Retrieved:** `{num_recs}`")
                st.markdown(f"**Guidance Type:** `{'safety' if data.get('uncertainty') else 'contextual'}`")
                st.markdown(f"**Processed At:** `{datetime.now().strftime('%H:%M:%S')}`")

            # Raw JSON for reviewers
            with st.expander("📄 Raw API Response (JSON)"):
                st.code(json.dumps(data, indent=2, ensure_ascii=False), language="json")

        st.markdown("---")

        # ── Recommendations ───────────────────────────────────────────────
        recs = data.get("recommendations", [])
        if recs:
            st.markdown(f"### 🛍️ Recommended Products ({len(recs)})")

            for i, rec in enumerate(recs, 1):
                conf = rec.get("confidence", 0)
                badge_cls = _conf_class(conf)
                badge_lbl = _conf_label(conf)

                # Usage guidance — handle both flat and nested formats
                usage = rec.get("usage_guidance", {})
                if isinstance(usage, dict):
                    usage_en = usage.get("en", rec.get("usage_guidance_en", ""))
                    usage_ar = usage.get("ar", rec.get("usage_guidance_ar", ""))
                else:
                    usage_en = rec.get("usage_guidance_en", "")
                    usage_ar = rec.get("usage_guidance_ar", "")

                st.markdown(f"""
<div class="product-card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.6rem;">
    <div class="product-title">#{i} — {rec.get("product_name", "")}</div>
    <span class="conf-badge {badge_cls}">{badge_lbl}</span>
  </div>
  <div class="product-why">⚡ {rec.get("why_this_product", "")}</div>
  <div class="product-reason">{rec.get("reason", "")}</div>
</div>
""", unsafe_allow_html=True)

                # Usage guidance side-by-side
                if usage_en or usage_ar:
                    uc1, uc2 = st.columns(2)
                    with uc1:
                        st.markdown(
                            f"<div style='background:#12151f;border:1px solid #2a2e45;border-radius:10px;"
                            f"padding:0.9rem;'>"
                            f"<div class='section-tag'>🇬🇧 Usage — English</div>"
                            f"<p style='color:#c4cce0;font-size:0.9rem;line-height:1.6;margin:0'>{usage_en}</p>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                    with uc2:
                        st.markdown(
                            f"<div style='background:#12151f;border:1px solid #2a2e45;border-radius:10px;"
                            f"padding:0.9rem;'>"
                            f"<div class='section-tag'>🇸🇦 الاستخدام — العربية</div>"
                            f"<p class='ar-text' style='margin:0'>{usage_ar}</p>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                # Review summary
                review = rec.get("review_summary", "")
                if review:
                    st.markdown(
                        f"<div style='margin:0.6rem 0 1.2rem;padding:0.7rem 1rem;"
                        f"background:#0d1117;border-radius:8px;border-left:3px solid #fbbf24;'>"
                        f"<span style='color:#fbbf24;font-size:0.78rem;font-weight:600;"
                        f"text-transform:uppercase;letter-spacing:0.06em;'>⭐ Customers Say</span><br>"
                        f"<span style='color:#9aa0bf;font-size:0.88rem'>{review}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

        else:
            st.info(
                "🔍 No specific product matches found for your query. "
                "Try adding more detail — for example, mention your baby's age "
                "or describe the specific symptom you're experiencing."
            )

        # ── Guidance ──────────────────────────────────────────────────────
        guidance = data.get("guidance", "")
        if guidance:
            st.markdown("---")
            st.markdown(
                f"<div class='guidance-box'>"
                f"<strong style='color:#7c5cfc'>💡 Guidance</strong><br><br>{guidance}"
                f"</div>",
                unsafe_allow_html=True,
            )

        # ── Arabic comfort ────────────────────────────────────────────────
        if cm.get("ar") and not data.get("uncertainty"):
            with st.expander("🌍 Comfort Message in Arabic / رسالة الطمأنينة بالعربية"):
                st.markdown(
                    f"<div class='ar-text' style='background:#12151f;border-radius:10px;"
                    f"padding:1.2rem;border:1px solid #2a2e45'>{cm.get('ar', '')}</div>",
                    unsafe_allow_html=True,
                )

        st.caption(
            f"⏱️ Response generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
            "MumzWorld AI v2.0 · Powered by Mumzworld"
        )

# ─────────────────────────────────────────────────────────────────────────────
# Tips
# ─────────────────────────────────────────────────────────────────────────────
if not submit:
    with st.expander("💡 Tips for Better Results"):
        st.markdown("""
**Be specific — the more context you give, the more precise our recommendations.**

| ❌ Vague | ✅ Specific |
|---------|------------|
| "Baby help" | "My 4-month-old is refusing the bottle" |
| "Pain" | "Sore nipples from breastfeeding, 2 weeks postpartum" |
| "Products" | "I need postpartum recovery products after a C-section" |

**Mention:**
- How many weeks/months postpartum you are
- Your baby's age (newborn, 3 months, 6 months, etc.)
- Whether it's for you or your baby
        """)
