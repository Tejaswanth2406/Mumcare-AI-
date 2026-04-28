@echo off
REM MumzWorld AI - Quick Start Script for Windows

echo.
echo 🚀 MumzWorld AI - Starting Services
echo ====================================
echo.

REM Check if .env exists
if not exist .env (
    echo ⚠️  .env file not found. Copying from .env.example...
    copy .env.example .env
    echo 📝 Please edit .env and add your OpenRouter API key
    pause
    exit /b 1
)

REM Check if venv exists
if not exist venv (
    echo 📦 Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

echo ✅ Environment ready
echo.
echo Starting services...
echo.

REM Start backend
echo 🔧 Starting FastAPI backend on port 8000...
start "MumCare API" cmd /k "uvicorn app.main:app --reload"

REM Give backend time to start
timeout /t 3 /nobreak

REM Start frontend
echo 🎨 Starting Streamlit frontend on port 8501...
echo.
echo 📱 Frontend URL: http://localhost:8501
echo 🔌 Backend URL: http://localhost:8000
echo.
echo Close the windows to stop the services.
echo.

streamlit run frontend/app.py
