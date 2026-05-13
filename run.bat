@echo off
cd /d "%~dp0"
echo Starting Production Tracker...
echo Open http://localhost:8000 in your browser
echo.
pip install -r requirements.txt -q
uvicorn app:app --reload --host 0.0.0.0 --port 8000
