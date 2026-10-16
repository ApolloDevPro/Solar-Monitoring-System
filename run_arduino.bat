@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Solar Tracker - backend + ARDUINO board
echo ============================================
echo.
echo Prereqs:
echo   1) pip install -r requirements.txt pyserial
echo   2) Arduino flashed with firmware\solar_tracker
echo   3) Board plugged in, port noted in IDE
echo.

python ensure_env.py
if errorlevel 1 (
  echo ERROR: Could not configure the local API key.
  pause
  exit /b 1
)

REM ---- serial port override: run_arduino.bat COM5 ----
set "COMPORT=%~1"

start "Solar Tracker - Backend" cmd /k "cd /d "%~dp0" && python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000"
timeout /t 3 /nobreak >nul

if "%COMPORT%"=="" (
  start "Solar Tracker - Serial Bridge" cmd /k "cd /d "%~dp0" && python -m simulator.serial_bridge"
) else (
  start "Solar Tracker - Serial Bridge" cmd /k "cd /d "%~dp0" && python -m simulator.serial_bridge --port-name %COMPORT%"
)

timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8000
