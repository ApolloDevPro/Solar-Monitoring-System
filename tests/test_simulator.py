"""Unit tests for simulator/device_sim.py and simulator/serial_bridge.py."""
from __future__ import annotations

from shared.control import MODE_AUTO, MODE_MANUAL, SolarTracker, TrackerConfig
from simulator.device_sim import apply_command
from simulator.serial_bridge import command_to_serial, parse_telemetry

# ── apply_command ─────────────────────────────────────────────────────────────

def make_tracker(mode=MODE_AUTO, angle=90.0):
    t = SolarTracker(TrackerConfig(), angle=angle, mode=mode)
    return t


def test_apply_set_mode_auto(make_tracker=make_tracker):
    t = make_tracker(mode=MODE_MANUAL)
    result = apply_command(t, {"type": "set_mode", "payload": {"mode": "AUTO"}})
    assert result["status"] == "EXECUTED"
    assert t.mode == MODE_AUTO


def test_apply_set_mode_manual():
    t = make_tracker()
    result = apply_command(t, {"type": "set_mode", "payload": {"mode": "MANUAL"}})
    assert result["status"] == "EXECUTED"
    assert t.mode == MODE_MANUAL


def test_apply_move_in_manual_mode():
    t = make_tracker(mode=MODE_MANUAL)
    result = apply_command(t, {"type": "move", "payload": {"target_angle": 130.0}})
    assert result["status"] == "EXECUTED"
    assert t.manual_target == 130.0


def test_apply_move_rejected_in_auto_mode():
    t = make_tracker(mode=MODE_AUTO)
    result = apply_command(t, {"type": "move", "payload": {"target_angle": 130.0}})
    assert result["status"] == "REJECTED"


def test_apply_move_missing_angle():
    t = make_tracker(mode=MODE_MANUAL)
    result = apply_command(t, {"type": "move", "payload": {}})
    assert result["status"] == "FAILED"


def test_apply_park():
    t = make_tracker(mode=MODE_AUTO, angle=150.0)
    result = apply_command(t, {"type": "park", "payload": {}})
    assert result["status"] == "EXECUTED"
    assert t.mode == MODE_MANUAL
    assert t.manual_target == TrackerConfig().park_angle


def test_apply_unknown_command():
    t = make_tracker()
    result = apply_command(t, {"type": "explode", "payload": {}})
    assert result["status"] == "REJECTED"


# ── parse_telemetry ───────────────────────────────────────────────────────────

def test_parse_valid_telemetry():
    parts = "T,1000,400,600,90.0,AUTO,ALIGNED,,boot".split(",")
    result = parse_telemetry(parts, "test-device")
    assert result is not None
    assert result["ldr_left"] == 400
    assert result["ldr_right"] == 600
    assert result["angle"] == 90.0
    assert result["mode"] == "AUTO"
    assert result["tracking_state"] == "ALIGNED"


def test_parse_telemetry_with_boot_id():
    """Firmware now appends a 10th field (boot ID)."""
    parts = "T,1000,400,600,90.0,AUTO,ALIGNED,,boot,42".split(",")
    result = parse_telemetry(parts, "test-device")
    assert result is not None
    assert result["boot_id"] == "42"


def test_parse_telemetry_clamps_ldr():
    parts = "T,1000,9999,-500,90.0,AUTO,ALIGNED,,".split(",")
    result = parse_telemetry(parts, "test-device")
    assert result["ldr_left"] == 1023
    assert result["ldr_right"] == 0


def test_parse_telemetry_clamps_angle():
    parts = "T,1000,400,600,999.0,AUTO,ALIGNED,,".split(",")
    result = parse_telemetry(parts, "test-device")
    assert result["angle"] == 180.0


def test_parse_telemetry_invalid_mode_defaults_to_auto():
    parts = "T,1000,400,600,90.0,CHAOS,ALIGNED,,".split(",")
    result = parse_telemetry(parts, "test-device")
    assert result["mode"] == "AUTO"


def test_parse_telemetry_too_few_fields():
    parts = "T,1000,400".split(",")
    result = parse_telemetry(parts, "test-device")
    assert result is None


def test_parse_telemetry_non_numeric_values():
    parts = "T,abc,def,ghi,xyz,AUTO,ALIGNED,,".split(",")
    result = parse_telemetry(parts, "test-device")
    assert result is None


# ── command_to_serial ─────────────────────────────────────────────────────────

def test_command_to_serial_set_mode_auto():
    result = command_to_serial({"type": "set_mode", "payload": {"mode": "AUTO"}})
    assert result == "MODE AUTO"


def test_command_to_serial_set_mode_manual():
    result = command_to_serial({"type": "set_mode", "payload": {"mode": "MANUAL"}})
    assert result == "MODE MANUAL"


def test_command_to_serial_move():
    result = command_to_serial({"type": "move", "payload": {"target_angle": 120.0}})
    assert result == "MOVE 120.0"


def test_command_to_serial_move_missing_angle():
    result = command_to_serial({"type": "move", "payload": {}})
    assert result is None


def test_command_to_serial_park():
    result = command_to_serial({"type": "park", "payload": {}})
    assert result == "PARK"


def test_command_to_serial_unknown():
    result = command_to_serial({"type": "explode", "payload": {}})
    assert result is None
