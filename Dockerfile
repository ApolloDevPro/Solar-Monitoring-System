# ── Solar Tracking System — Backend + Dashboard ──────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY backend/   ./backend/
COPY shared/    ./shared/
COPY simulator/ ./simulator/
COPY dashboard/ ./dashboard/

EXPOSE 8000

# Environment variable defaults (override with docker run -e or docker-compose)
ENV SOLAR_DEVICE_ID=solar-tracker-01
ENV SOLAR_OFFLINE_AFTER=10
ENV SOLAR_COMMAND_EXPIRE=60

CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
