"""Pytest-compatible end-to-end API smoke checks using an in-process server."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.database import Database


@pytest.fixture()
def smoke_client(tmp_path, monkeypatch):
    database = Database(tmp_path / "smoke.db")
    monkeypatch.setattr(app_module, "db", database)
    with TestClient(app_module.app) as client:
        yield client
    database.close()


def test_smoke_api_health_and_dashboard(smoke_client):
    assert smoke_client.get("/api/health").json()["status"] == "ok"
    assert smoke_client.get("/api/config").status_code == 200
    assert "Automatic Solar Tracking System" in smoke_client.get("/").text
    assert "handleSseFrame" in smoke_client.get("/app.js").text


def test_smoke_api_telemetry_commands_and_history(smoke_client):
    telemetry = {
        "device_id": "solar-tracker-01",
        "ts_ms": 1000,
        "ldr_left": 400,
        "ldr_right": 700,
        "angle": 90,
        "mode": "MANUAL",
        "tracking_state": "MANUAL",
        "fault": None,
        "move_reason": "smoke test",
        "boot_id": "smoke1",
        "seq": 1,
    }
    assert smoke_client.post("/api/telemetry", json=telemetry).status_code == 200
    assert smoke_client.get("/api/state").json()["is_online"] is True

    command = smoke_client.post(
        "/api/commands", json={"type": "move", "target_angle": 140}
    )
    assert command.status_code == 201
    assert command.json()["accepted"] is True
    command_id = command.json()["command"]["id"]
    assert command_id in [
        row["id"] for row in smoke_client.get("/api/commands/pending").json()["items"]
    ]
    assert smoke_client.post(
        f"/api/commands/{command_id}/result",
        json={"status": "EXECUTED", "reason": "move complete"},
    ).json()["command"]["status"] == "EXECUTED"

    assert smoke_client.get("/api/history/light").json()["items"]
    assert smoke_client.get("/api/history/orientation").json()["items"]
    assert smoke_client.get("/api/summary").json()["commands_executed"] == 1
