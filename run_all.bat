@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Automatic Solar Tracking System - Task 14
echo ============================================
echo.
echo 1) Make sure dependencies are installed:
echo      pip install -r requirements.txt
echo.
python ensure_env.py
if errorlevel 1 (
  echo ERROR: Could not configure the local API key.
  pause
  exit /b 1
)

echo Starting backend + dashboard on http://127.0.0.1:8000
echo Starting device simulator in a second window...
echo Close both windows to stop.
echo.

start "Solar Tracker - Backend" cmd /k "cd /d "%~dp0" && python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000"
timeout /t 2 /nobreak >nul
start "Solar Tracker - Simulator" cmd /k "cd /d "%~dp0" && python -m simulator.device_sim"

timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8000
