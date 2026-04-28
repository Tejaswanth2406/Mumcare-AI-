#!/bin/bash
# MumCare AI - Quick Start Script

echo "🚀 MumCare AI - Starting Services"
echo "===================================="

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Copying from .env.example..."
    cp .env.example .env
    echo "📝 Please edit .env and add your OpenRouter API key"
    exit 1
fi

# Install dependencies if needed
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo "✅ Environment ready"
echo ""
echo "Starting services..."
echo ""

# Start backend in background
echo "🔧 Starting FastAPI backend on port 8000..."
uvicorn app.main:app --reload &
BACKEND_PID=$!

# Give backend time to start
sleep 2

# Start frontend
echo "🎨 Starting Streamlit frontend on port 8501..."
echo ""
echo "📱 Frontend URL: http://localhost:8501"
echo "🔌 Backend URL: http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop all services"
echo ""

streamlit run frontend/app.py

# Cleanup on exit
trap "kill $BACKEND_PID" EXIT
