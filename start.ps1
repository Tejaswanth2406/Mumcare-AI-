#!/usr/bin/env pwsh
# MumzWorld AI — Quick Start Script (PowerShell)
# Activates venv and starts FastAPI backend

Write-Host ""
Write-Host "╔════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║         MumzWorld AI — Smart Maternal Helper     ║" -ForegroundColor Cyan
Write-Host "║                                                ║" -ForegroundColor Cyan
Write-Host "║   Starting FastAPI Backend + Website...       ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# Check if venv exists
if (-not (Test-Path ".venv")) {
    Write-Host "⚠️  Virtual environment not found!" -ForegroundColor Yellow
    Write-Host "Creating venv..." -ForegroundColor Yellow
    python -m venv .venv
}

# Activate venv
Write-Host "Activating virtual environment..." -ForegroundColor Yellow
& .\.venv\Scripts\Activate.ps1

# Check if requirements installed
$pipList = pip list
if ($pipList -notcontains "fastapi") {
    Write-Host "Installing dependencies..." -ForegroundColor Yellow
    pip install -r requirements.txt
}

# Check if .env exists
if (-not (Test-Path ".env")) {
    Write-Host ""
    Write-Host "⚠️  .env file not found!" -ForegroundColor Red
    Write-Host "Please copy .env.example to .env and add your OpenRouter API key:" -ForegroundColor Yellow
    Write-Host "OPENROUTER_API_KEY=your-key-here" -ForegroundColor Gray
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "✅ Starting MumzWorld AI Backend..." -ForegroundColor Green
Write-Host ""
Write-Host "🌐 Open your browser and go to: http://localhost:8000" -ForegroundColor Cyan
Write-Host "📚 API Docs: http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press Ctrl+C to stop the server" -ForegroundColor Yellow
Write-Host ""

# Start FastAPI
uvicorn app.main:app --reload --port 8000
