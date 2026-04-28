# MumCare AI — Complete Setup & Running Guide

## Overview
MumCare AI now has **two interfaces** that work together:

1. **Website (FastAPI + HTML)** — Beautiful interactive web interface served at `http://localhost:8000`
2. **Streamlit App** — Premium dashboard served at `http://localhost:8501` (optional)

Both connect to the same backend API.

---

## ✅ Prerequisites

### 1. Environment Setup
```bash
# Create virtual environment
python -m venv .venv

# Activate it
# Windows:
.venv\Scripts\Activate.ps1
# Mac/Linux:
source .venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment
```bash
# Copy the example .env file
copy .env.example .env

# Then edit .env and add your OpenRouter API key:
OPENROUTER_API_KEY=your-api-key-here
```

Get your API key from: https://openrouter.ai/keys

---

## 🚀 Running the Application

### Option 1: Run Everything (Backend + Website Only)
```bash
# Start the FastAPI backend with static file serving
uvicorn app.main:app --reload --port 8000
```

Then open: **http://localhost:8000**

---

### Option 2: Run Backend + Streamlit App (Both Interfaces)

**Terminal 1 — Backend API:**
```bash
uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — Streamlit Frontend:**
```bash
streamlit run frontend/app.py
```

Then:
- **Website**: http://localhost:8000 
- **Streamlit App**: http://localhost:8501

---

## 📝 Testing the Application

### Website (at http://localhost:8000)

1. **Type a question** in the chat input:
   - "I have cracked nipples from breastfeeding"
   - "My newborn won't sleep"
   - "I have postpartum bleeding"
   - Or ask anything about maternal/baby care!

2. **Press Send** and watch the AI respond with:
   - ✅ Detected intent
   - 💬 Bilingual comfort message (EN + AR)
   - 📦 Product recommendations with confidence scores
   - 📚 Expert guidance

### Expected Flow

```
You:      "I have colic symptoms in my newborn"
          ↓
AI:       [Processing...]
          ↓
Response: 
  • Intent: baby_care
  • Confidence: 87%
  • Products: Anti-colic bottles, probiotics, etc.
  • Message (EN): "Colic is frustrating but temporary..."
  • Message (AR): "المغص شائع جداً عند الأطفال..."
  • Guidance: Expert advice on soothing techniques
```

---

## 🔧 Troubleshooting

### "Connection Error - Check if backend is running"

**Fix:**
```bash
# Make sure you're in the venv and ran:
uvicorn app.main:app --reload --port 8000
```

### Port Already in Use

```bash
# Use a different port:
uvicorn app.main:app --reload --port 8001
# Then visit http://localhost:8001
```

### API Key Issues

```bash
# Check your .env file has:
OPENROUTER_API_KEY=sk-or-v1-xxxxx

# If not set, you'll get error when submitting queries
```

---

## 📁 File Structure

```
MumzWorld/
├── app/                    # FastAPI backend
│   ├── main.py            # ← Serves website + API
│   ├── routes/
│   │   └── ai.py          # API endpoint: /ai/query
│   └── services/          # LLM, RAG, validation logic
├── frontend/
│   ├── index.html         # ← NEW: Interactive website
│   └── app.py             # Streamlit app (optional)
└── requirements.txt       # All dependencies
```

---

## 🎯 Key Features

✨ **Now Working:**
- ✅ Interactive chat interface (no need for Streamlit)
- ✅ Real-time API integration 
- ✅ Bilingual responses (English + Arabic)
- ✅ Connection status indicator
- ✅ Error handling & loading states
- ✅ Product recommendations with confidence
- ✅ Safety validation for medical queries

📱 **Both Interfaces Use Same Backend:**
- Same `/ai/query` endpoint
- Same product catalog
- Same safety checks
- Same bilingual responses

---

## 💡 Architecture

```
Browser (http://localhost:8000)
    ↓
    → FastAPI (app/main.py)
        ├→ Static files (frontend/index.html) 
        └→ API routes (/ai/query)
            ├→ Intent parser
            ├→ RAG retriever
            ├→ LLM generator (OpenRouter)
            └→ Validator & formatter
```

---

## 🧪 API Testing

**Quick test using curl:**
```bash
curl -X POST http://localhost:8000/ai/query \
  -H "Content-Type: application/json" \
  -d '{"query": "I have postpartum bleeding"}'
```

**View interactive API docs:**
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

---

## 📖 Next Steps

1. ✅ **Run the backend** with `uvicorn app.main:app --reload`
2. ✅ **Open** http://localhost:8000 in your browser
3. ✅ **Type a query** about maternal/baby care
4. ✅ **See the AI respond** with products & guidance

Enjoy! 🌸
