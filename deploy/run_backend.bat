@echo off
rem Starts the FastAPI backend from the project root.
rem Optional settings (SOLAR_DEVICE_ID, SOLAR_DB_PATH, ...) are read from .env.
cd /d "%~dp0.."
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
