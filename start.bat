@echo off
REM MumzWorld AI — Quick Start Script
REM Activates venv and starts FastAPI backend

echo.
echo ╔════════════════════════════════════════════════╗
echo ║         MumzWorld AI — Smart Maternal Helper     ║
echo ║                                                ║
echo ║   Starting FastAPI Backend + Website...       ║
echo ╚════════════════════════════════════════════════╝
echo.

REM Check if venv exists
if not exist ".venv" (
    echo ⚠️  Virtual environment not found!
    echo Creating venv...
    python -m venv .venv
)

REM Activate venv
echo Activating virtual environment...
call .venv\Scripts\activate.bat

REM Check if requirements installed
pip list | findstr "fastapi" >nul
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
)

REM Check if .env exists
if not exist ".env" (
    echo.
    echo ⚠️  .env file not found!
    echo Please copy .env.example to .env and add your OpenRouter API key:
    echo OPENROUTER_API_KEY=your-key-here
    echo.
    pause
    exit /b 1
)

echo.
echo ✅ Starting MumzWorld AI Backend...
echo.
echo 🌐 Open your browser and go to: http://localhost:8000
echo 📚 API Docs: http://localhost:8000/docs
echo.
echo Press Ctrl+C to stop the server
echo.

REM Start FastAPI
uvicorn app.main:app --reload --port 8000

pause
