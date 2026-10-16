@echo off
setlocal
cd /d "%~dp0"
python ensure_env.py
if errorlevel 1 (
  echo ERROR: Could not create the local .env file.
  pause
  exit /b 1
)
echo Starting device simulator ...
python -m simulator.device_sim %*
pause
