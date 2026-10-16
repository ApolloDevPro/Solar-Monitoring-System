"""Unit tests for simulator/device_sim.py and simulator/serial_bridge.py."""
from __future__ import annotations

from shared.control import MODE_AUTO, MODE_MANUAL, SolarTracker, TrackerConfig
from simulator.device_sim import apply_command
from simulator.serial_bridge import (
    command_to_serial,
    enqueue_telemetry,
    flush_telemetry,
    parse_telemetry,
)

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


def test_sun_model_fixed_value():
    from simulator.device_sim import SunModel

    assert SunModel(fixed=45.0).azimuth() == 45.0


def test_sun_model_full_period_sweeps_once():
    # Regression: a doubled phase factor made the sun sweep twice as fast.
    # azimuth(t) = 90 + 70*sin(2*pi*t/period); check the four quarter marks.
    import time

    import pytest

    from simulator.device_sim import SunModel

    period = 90.0
    sun = SunModel(period_s=period)
    t_now = time.monotonic

    # t=0 (just constructed) -> sin(0)=0 -> 90
    assert sun.azimuth() == pytest.approx(90.0, abs=1.0)

    # quarter period -> sin(pi/2)=1 -> 160
    sun.t0 = t_now() - period / 4
    assert sun.azimuth() == pytest.approx(160.0, abs=1.0)

    # half period -> sin(pi)=0 -> 90
    sun.t0 = t_now() - period / 2
    assert sun.azimuth() == pytest.approx(90.0, abs=1.0)

    # three-quarter period -> sin(3*pi/2)=-1 -> 20
    sun.t0 = t_now() - 3 * period / 4
    assert sun.azimuth() == pytest.approx(20.0, abs=1.0)

    # full period -> back to start (a doubled phase would land at 360/720)
    sun.t0 = t_now() - period
    assert sun.azimuth() == pytest.approx(90.0, abs=1.0)


def test_sun_model_annotations_resolve():
    # Regression: `Optional` was used before being imported, so the module
    # failed at import time with a NameError under from __future__ annotations.
    from typing import get_type_hints

    from simulator.device_sim import SunModel

    hints = get_type_hints(SunModel.__init__)
    assert "fixed" in hints


# ── telemetry retry queue ────────────────────────────────────────────────────

class _FakeBackend:
    """Stands in for simulator.http_client.Backend in queue tests."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.posts: list = []

    def post(self, path, payload):
        self.posts.append((path, payload))
        return self.ok


def test_enqueue_telemetry_appends_payload():
    from collections import deque

    q: deque = deque(maxlen=3)
    enqueue_telemetry(q, {"seq": 1})
    enqueue_telemetry(q, {"seq": 2})
    assert [p["seq"] for p in q] == [1, 2]


def test_enqueue_telemetry_bounded_drops_oldest():
    from collections import deque

    q: deque = deque(maxlen=2)
    for i in range(5):
        enqueue_telemetry(q, {"seq": i})
    assert [p["seq"] for p in q] == [3, 4]


def test_flush_telemetry_drains_one_sample_in_order():
    from collections import deque

    q: deque = deque([{"seq": 1}, {"seq": 2}])
    backend = _FakeBackend(ok=True)
    last = flush_telemetry(backend, q, now=10.0, last_flush=0.0, min_interval_s=1.5)
    assert last == 10.0
    assert [p["seq"] for p in q] == [2]
    assert backend.posts == [("/api/telemetry", {"seq": 1})]


def test_flush_telemetry_respects_pacing():
    from collections import deque

    q: deque = deque([{"seq": 1}])
    backend = _FakeBackend(ok=True)
    # not enough time elapsed -> nothing sent
    last = flush_telemetry(backend, q, now=1.0, last_flush=0.0, min_interval_s=1.5)
    assert last == 0.0
    assert len(q) == 1
    assert backend.posts == []


def test_flush_telemetry_keeps_queue_on_failure():
    from collections import deque

    q: deque = deque([{"seq": 1}, {"seq": 2}])
    backend = _FakeBackend(ok=False)
    last = flush_telemetry(backend, q, now=10.0, last_flush=0.0, min_interval_s=1.5)
    assert last == 0.0  # unchanged: backend still down
    assert [p["seq"] for p in q] == [1, 2]  # order preserved for retry


def test_flush_telemetry_empty_queue_is_noop():
    from collections import deque

    q: deque = deque()
    backend = _FakeBackend(ok=True)
    last = flush_telemetry(backend, q, now=10.0, last_flush=0.0, min_interval_s=1.5)
    assert last == 0.0
    assert backend.posts == []
