"""Unit tests for the FastAPI backend (backend/app.py + backend/database.py).

Runs entirely in-process using httpx + FastAPI TestClient — no live server needed.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import backend.app as app_module

# Import after conftest.py ensures project root is on sys.path
from backend.database import Database


@pytest.fixture()
def fresh_db(tmp_path):
    """A clean in-memory-like DB in a temp directory for each test."""
    db = Database(path=tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture()
def client(fresh_db, monkeypatch):
    """TestClient with the global db swapped for a fresh one."""
    monkeypatch.setattr(app_module, "db", fresh_db)
    monkeypatch.setattr(app_module, "SOLAR_API_KEY", "test-secret")
    with TestClient(app_module.app, raise_server_exceptions=True) as c:
        c.headers.update({"X-API-Key": "test-secret"})
        yield c


# ── helpers ──────────────────────────────────────────────────────────────────
BASE_TELEMETRY = {
    "device_id": "solar-tracker-01",
    "ts_ms": 1000,
    "ldr_left": 400,
    "ldr_right": 600,
    "angle": 90.0,
    "mode": "AUTO",
    "tracking_state": "EVALUATING",
    "fault": None,
    "move_reason": "test",
    "boot_id": "abc123",
    "seq": 1,
}


def post_telemetry(client, overrides=None):
    payload = dict(BASE_TELEMETRY, **(overrides or {}))
    return client.post("/api/telemetry", json=payload)


# ── health / config ───────────────────────────────────────────────────────────
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_config_has_required_keys(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    for key in ("deadband", "confirm_samples", "filter_window", "settle_ms",
                "soft_min", "soft_max", "park_angle", "modes"):
        assert key in body, f"config missing key: {key}"


# ── telemetry ingest ──────────────────────────────────────────────────────────
def test_telemetry_accepted(client):
    r = post_telemetry(client)
    assert r.status_code == 200
    assert r.json()["ldr_left"] == 400


def test_telemetry_bad_ldr_rejected(client):
    r = post_telemetry(client, {"ldr_left": 9999})
    assert r.status_code == 422


def test_telemetry_bad_angle_rejected(client):
    r = post_telemetry(client, {"angle": 999.0})
    assert r.status_code == 422


def test_telemetry_fault_too_long_rejected(client):
    r = post_telemetry(client, {"fault": "x" * 300})
    assert r.status_code == 422


def test_telemetry_reason_too_long_rejected(client):
    r = post_telemetry(client, {"move_reason": "y" * 300})
    assert r.status_code == 422


def test_state_shows_online_after_telemetry(client):
    post_telemetry(client)
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["is_online"] is True
    assert body["angle"] == 90.0


def test_light_history_populated(client):
    post_telemetry(client)
    r = client.get("/api/history/light?limit=10")
    assert r.status_code == 200
    assert len(r.json()["items"]) >= 1


def test_orientation_history_populated(client):
    post_telemetry(client)
    r = client.get("/api/history/orientation?limit=10")
    assert r.status_code == 200
    # At least one orientation row should exist
    assert len(r.json()["items"]) >= 1


# ── command validation ────────────────────────────────────────────────────────
def test_command_rejected_unknown_type(client):
    post_telemetry(client)  # device must be online
    r = client.post("/api/commands", json={"type": "explode"})
    assert r.status_code == 201
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"]


def test_command_rejected_out_of_range_angle(client):
    post_telemetry(client)
    r = client.post("/api/commands", json={"type": "move", "target_angle": 999})
    assert r.status_code == 201
    assert r.json()["accepted"] is False


def test_move_rejected_in_auto_mode(client):
    post_telemetry(client, {"mode": "AUTO"})
    r = client.post("/api/commands", json={"type": "move", "target_angle": 120})
    assert r.status_code == 201
    assert r.json()["accepted"] is False


def test_invalid_mode_rejected(client):
    post_telemetry(client)
    r = client.post("/api/commands", json={"type": "set_mode", "mode": "CHAOS"})
    assert r.status_code == 201
    assert r.json()["accepted"] is False


def test_move_missing_target_angle_rejected(client):
    post_telemetry(client)
    r = client.post("/api/commands", json={"type": "move"})
    assert r.status_code == 201
    assert r.json()["accepted"] is False


def test_set_mode_accepted(client):
    post_telemetry(client)
    r = client.post("/api/commands", json={"type": "set_mode", "mode": "MANUAL"})
    assert r.status_code == 201
    assert r.json()["accepted"] is True


def test_move_accepted_in_manual_mode(client):
    post_telemetry(client, {"mode": "MANUAL", "tracking_state": "MANUAL"})
    r = client.post("/api/commands", json={"type": "move", "target_angle": 140})
    assert r.status_code == 201
    assert r.json()["accepted"] is True


def test_park_accepted(client):
    post_telemetry(client, {"mode": "MANUAL", "tracking_state": "MANUAL"})
    r = client.post("/api/commands", json={"type": "park"})
    assert r.status_code == 201
    assert r.json()["accepted"] is True


# ── command lifecycle ─────────────────────────────────────────────────────────
def test_command_appears_in_pending(client):
    post_telemetry(client)
    r = client.post("/api/commands", json={"type": "set_mode", "mode": "MANUAL"})
    cmd_id = r.json()["command"]["id"]
    r2 = client.get("/api/commands/pending")
    ids = [c["id"] for c in r2.json()["items"]]
    assert cmd_id in ids


def test_command_result_recorded(client):
    post_telemetry(client)
    r = client.post("/api/commands", json={"type": "set_mode", "mode": "MANUAL"})
    cmd_id = r.json()["command"]["id"]
    r2 = client.post(f"/api/commands/{cmd_id}/result",
                     json={"status": "EXECUTED", "reason": "done"})
    assert r2.json()["command"]["status"] == "EXECUTED"


def test_command_404_for_missing_id(client):
    r = client.post("/api/commands/9999/result",
                    json={"status": "EXECUTED"})
    assert r.status_code == 404


# ── summary / history ─────────────────────────────────────────────────────────
def test_summary_returns_efficiency(client):
    post_telemetry(client)
    r = client.get("/api/summary")
    assert r.status_code == 200
    body = r.json()
    assert "efficiency" in body
    assert "total_degrees_moved" in body["efficiency"]


def test_fault_history_endpoint(client):
    # Post a fault telemetry
    post_telemetry(
        client, {"tracking_state": "SENSOR_FAULT", "fault": "left LDR stuck at ADC rail"}
    )
    r = client.get("/api/history/faults")
    assert r.status_code == 200
    assert r.json()["items"][0]["tracking_state"] == "SENSOR_FAULT"


# ── API key auth ──────────────────────────────────────────────────────────────
def test_api_key_required_when_set(fresh_db, monkeypatch):
    monkeypatch.setattr(app_module, "db", fresh_db)
    monkeypatch.setattr(app_module, "SOLAR_API_KEY", "secret123")
    with TestClient(app_module.app) as c:
        # Without key — should be 401
        post_telemetry_no_key = c.post("/api/telemetry", json=BASE_TELEMETRY)
        assert post_telemetry_no_key.status_code == 401
        assert c.get("/api/state").status_code == 401
        # With correct key — should work
        r = c.post("/api/telemetry", json=BASE_TELEMETRY,
                   headers={"X-API-Key": "secret123"})
        assert r.status_code == 200


def test_protected_routes_fail_closed_without_configured_key(fresh_db, monkeypatch):
    monkeypatch.setattr(app_module, "db", fresh_db)
    monkeypatch.setattr(app_module, "SOLAR_API_KEY", "")
    with TestClient(app_module.app) as c:
        response = c.post("/api/telemetry", json=BASE_TELEMETRY)
    assert response.status_code == 503


def test_validate_command_rejects_unknown_type(client):
    command = app_module.CommandCreate(type="unknown")
    assert "unknown command type" in app_module.validate_command(command)[0]


def test_ingest_persists_state_and_history(fresh_db, monkeypatch):
    monkeypatch.setattr(app_module, "db", fresh_db)
    state = app_module.ingest(app_module.Telemetry(**BASE_TELEMETRY))
    assert state["device_id"] == BASE_TELEMETRY["device_id"]
    assert len(fresh_db.recent_light(10)) == 1
    assert len(fresh_db.recent_orientation(10)) == 1


def test_sse_reconnect_continues_event_id():
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def first_event():
        request = Request(
            {"type": "http", "method": "GET", "path": "/api/events", "headers": []},
            receive,
        )
        response = await app_module.stream_events(request, "41", None)
        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return chunk

    assert asyncio.run(first_event()).startswith("id: 42\n")


def test_example_placeholder_key_is_rejected(client, monkeypatch):
    monkeypatch.setattr(
        app_module, "SOLAR_API_KEY", "replace-this-with-a-long-random-secret"
    )
    response = client.get("/api/state")
    assert response.status_code == 503


# ── delete endpoints ──────────────────────────────────────────────────────────
def test_cancel_pending_commands(client):
    post_telemetry(client)
    client.post("/api/commands", json={"type": "set_mode", "mode": "MANUAL"})
    r = client.delete("/api/commands/pending")
    assert r.status_code == 200
    assert r.json()["cancelled"] >= 1
    # Pending list should now be empty
    r2 = client.get("/api/commands/pending")
    assert len(r2.json()["items"]) == 0


def test_clear_history(client):
    post_telemetry(client)
    r = client.delete("/api/history")
    assert r.status_code == 200
    assert "deleted" in r.json()
    # History should be empty
    r2 = client.get("/api/history/light?limit=10")
    assert len(r2.json()["items"]) == 0


def test_command_result_reason_has_length_limit(client):
    response = client.post(
        "/api/commands/1/result",
        json={"status": "EXECUTED", "reason": "x" * 201},
    )
    assert response.status_code == 422


def test_z_telemetry_rate_limit(client):
    # The autouse conftest fixture disables rate limiting for the whole
    # session; this test opts back in to verify the limit is real.
    app_module.limiter.enabled = True
    try:
        statuses = [
            post_telemetry(client, {"ts_ms": tick}).status_code
            for tick in range(310)
        ]
        assert 429 in statuses
    finally:
        app_module.limiter.enabled = False
