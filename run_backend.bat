@echo off
setlocal
cd /d "%~dp0"
python ensure_env.py
if errorlevel 1 (
  echo ERROR: Could not create the local .env file.
  pause
  exit /b 1
)
echo Starting backend (dashboard -> http://127.0.0.1:8000) ...
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
pause
