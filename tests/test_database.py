"""Unit tests for backend/database.py — tests the DB layer in isolation."""
from __future__ import annotations

import pytest

from backend.database import Database, utcnow


@pytest.fixture()
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    yield d
    d.close()


SAMPLE_STATE = {
    "device_id": "test-device",
    "mode": "AUTO",
    "tracking_state": "ALIGNED",
    "angle": 90.0,
    "ldr_left": 400,
    "ldr_right": 600,
    "difference": 200,
    "fault": None,
    "is_online": True,
    "last_seen": utcnow(),
    "boot_id": "abc",
    "updated_at": utcnow(),
}


def test_upsert_and_get_state(db):
    db.upsert_state(SAMPLE_STATE)
    s = db.get_state("test-device")
    assert s is not None
    assert s["angle"] == 90.0
    assert s["is_online"] == 1


def test_upsert_overwrites(db):
    db.upsert_state(SAMPLE_STATE)
    db.upsert_state(dict(SAMPLE_STATE, angle=120.0))
    s = db.get_state("test-device")
    assert s["angle"] == 120.0


def test_get_state_returns_none_for_unknown(db):
    assert db.get_state("nobody") is None


def test_add_and_recent_light(db):
    db.add_light_sample({"device_id": "d", "ts": utcnow(), "ts_ms": 1000,
                          "ldr_left": 100, "ldr_right": 200, "difference": 100})
    rows = db.recent_light(10, "d")
    assert len(rows) == 1
    assert rows[0]["ldr_left"] == 100


def test_add_and_recent_orientation(db):
    db.add_orientation({"device_id": "d", "ts": utcnow(), "angle": 45.0,
                         "mode": "AUTO", "tracking_state": "SLEWING_RIGHT", "reason": "test"})
    rows = db.recent_orientation(10)
    assert len(rows) == 1
    assert rows[0]["angle"] == 45.0


def test_create_and_get_command(db):
    cmd = db.create_command("d", "move", {"target_angle": 90}, "dashboard", "PENDING", None)
    assert cmd["id"] > 0
    fetched = db.get_command(cmd["id"])
    assert fetched["type"] == "move"
    assert fetched["payload"] == {"target_angle": 90}


def test_pending_commands(db):
    db.upsert_state(SAMPLE_STATE)
    cmd = db.create_command("test-device", "park", {}, "dashboard", "PENDING", None)
    pending = db.pending_commands("test-device", 0, 10)
    assert any(c["id"] == cmd["id"] for c in pending)


def test_update_command(db):
    cmd = db.create_command("d", "park", {}, "dashboard", "PENDING", None)
    updated = db.update_command(cmd["id"], "EXECUTED", "done")
    assert updated["status"] == "EXECUTED"
    assert updated["reason"] == "done"


def test_expire_stale_commands(db):
    db.create_command("d", "move", {"target_angle": 90}, "dashboard", "PENDING", None)
    # Expire everything up to now+1s
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(timespec="milliseconds")
    count = db.expire_stale_commands(future)
    assert count >= 1


def test_counts(db):
    db.add_light_sample({"device_id": "d", "ts": utcnow(), "ts_ms": 1,
                          "ldr_left": 1, "ldr_right": 2, "difference": 1})
    c = db.counts()
    assert c["light_samples"] >= 1


def test_prune_removes_old_rows(db):
    for i in range(10):
        db.add_light_sample({"device_id": "d", "ts": utcnow(), "ts_ms": i,
                              "ldr_left": i, "ldr_right": i, "difference": 0})
    # Prune with max=5
    from backend.database import MAX_ROWS
    original = MAX_ROWS["light_samples"]
    MAX_ROWS["light_samples"] = 5
    db.prune()
    MAX_ROWS["light_samples"] = original
    rows = db.recent_light(100, "d")
    assert len(rows) <= 5


def test_recent_faults(db):
    db.add_orientation({"device_id": "d", "ts": utcnow(), "angle": 90.0,
                         "mode": "AUTO", "tracking_state": "SENSOR_FAULT",
                         "reason": "left LDR stuck"})
    db.add_orientation({"device_id": "d", "ts": utcnow(), "angle": 90.0,
                         "mode": "AUTO", "tracking_state": "ALIGNED",
                         "reason": "recovered"})
    faults = db.recent_faults(10)
    assert len(faults) == 1
    assert faults[0]["tracking_state"] == "SENSOR_FAULT"


def test_clear_history(db):
    db.add_light_sample({"device_id": "d", "ts": utcnow(), "ts_ms": 1,
                          "ldr_left": 1, "ldr_right": 2, "difference": 1})
    db.add_orientation({"device_id": "d", "ts": utcnow(), "angle": 90.0,
                         "mode": "AUTO", "tracking_state": "ALIGNED"})
    db.create_command("d", "park", {}, "dashboard", "PENDING", None)
    db.create_command("d", "park", {}, "dashboard", "EXECUTED", "done")
    deleted = db.clear_history()
    assert db.recent_light(100) == []
    assert db.recent_orientation(100) == []
    assert deleted["commands"] == 1
    assert len(db.pending_commands("d", 0, 10)) == 1


def test_cancel_pending_commands(db):
    db.create_command("d", "park", {}, "dashboard", "PENDING", None)
    db.create_command("d", "move", {"target_angle": 90}, "dashboard", "PENDING", None)
    count = db.cancel_pending_commands("d")
    assert count == 2
    pending = db.pending_commands("d", 0, 10)
    assert pending == []


def test_efficiency_stats_empty(db):
    stats = db.efficiency_stats("d")
    assert stats["total_degrees_moved"] == 0.0
    assert stats["auto_events"] == 0


def test_efficiency_stats_with_data(db):
    for angle in [90.0, 100.0, 110.0, 115.0]:
        db.add_orientation({"device_id": "d", "ts": utcnow(), "angle": angle,
                             "mode": "AUTO", "tracking_state": "SLEWING_RIGHT",
                             "reason": "auto track"})
    stats = db.efficiency_stats("d")
    assert stats["total_degrees_moved"] == pytest.approx(25.0)
    assert stats["auto_events"] == 4


def test_efficiency_stats_measure_mode_duration(db):
    base = "2025-01-01T00:00:00+00:00"
    db.add_orientation({"device_id": "d", "ts": base, "angle": 90.0,
                         "mode": "AUTO", "tracking_state": "ALIGNED"})
    db.add_orientation({"device_id": "d", "ts": "2025-01-01T00:00:10+00:00",
                         "angle": 100.0, "mode": "AUTO",
                         "tracking_state": "SLEWING_RIGHT"})
    db.add_orientation({"device_id": "d", "ts": "2025-01-01T00:00:30+00:00",
                         "angle": 100.0, "mode": "MANUAL",
                         "tracking_state": "MANUAL"})

    stats = db.efficiency_stats("d")
    assert stats["time_in_auto_seconds"] == pytest.approx(30.0)
    assert stats["time_in_manual_seconds"] == pytest.approx(0.0)
    assert stats["auto_pct"] == pytest.approx(100.0)
