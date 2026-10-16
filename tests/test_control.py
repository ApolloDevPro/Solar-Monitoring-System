"""Tests for the Task 14 anti-oscillation tracking algorithm."""

from __future__ import annotations

import random

from shared.control import (  # noqa: E402
    MODE_AUTO,
    MODE_MANUAL,
    STATE_ALIGNED,
    STATE_SENSOR_FAULT,
    STATE_SLEWING_LEFT,
    STATE_SLEWING_RIGHT,
    SolarTracker,
    TrackerConfig,
    simulate_ldr_pair,
)


def run(tracker: SolarTracker, sun: float, ticks: int, noise: float = 0.0,
        start_ms: int = 0, period_ms: int = 200, spikes: float = 0.0,
        spike_size: int = 400, rng: random.Random | None = None):
    rng = rng or random.Random(1234)
    now = start_ms
    out = None
    for _ in range(ticks):
        left, right = simulate_ldr_pair(sun, tracker.angle)
        if noise:
            left += rng.gauss(0, noise)
            right += rng.gauss(0, noise)
        if spikes and rng.random() < spikes:
            if rng.random() < 0.5:
                left += rng.choice((-spike_size, spike_size))
            else:
                right += rng.choice((-spike_size, spike_size))
        out = tracker.step(now, int(left), int(right))
        now += period_ms
    return out


def test_reads_are_equal_when_panel_faces_sun():
    left, right = simulate_ldr_pair(90, 90)
    assert left == right


def test_stronger_light_is_on_the_correct_side():
    left, right = simulate_ldr_pair(sun_azimuth_deg=140, panel_angle_deg=90)
    assert right > left, "sun to the right must light the right LDR"
    left, right = simulate_ldr_pair(sun_azimuth_deg=40, panel_angle_deg=90)
    assert left > right, "sun to the left must light the left LDR"


def test_no_movement_when_readings_are_nearly_equal():
    """Deadband: an almost centred sun must not cause any servo motion."""
    tracker = SolarTracker(angle=90)
    run(tracker, sun=91.5, ticks=600)
    assert tracker.stats["moves"] == 0, tracker.stats
    assert tracker.state == STATE_ALIGNED
    assert tracker.angle == 90


def test_no_oscillation_under_noise():
    """Repeated noisy samples must never produce a left/right dither."""
    tracker = SolarTracker(angle=90)
    rng = random.Random(7)
    run(tracker, sun=93.0, ticks=800, noise=8.0, spikes=0.01, rng=rng)
    assert tracker.stats["moves"] <= 2, tracker.stats
    assert tracker.stats["direction_reversals"] <= 1, tracker.stats


def test_tracks_the_stronger_source_and_settles():
    tracker = SolarTracker(angle=90)
    out = run(tracker, sun=150, ticks=600)
    assert tracker.angle > 130, tracker.angle
    assert tracker.stats["moves"] > 3
    # once settled it must stop moving entirely
    moves_before = tracker.stats["moves"]
    run(tracker, sun=150, ticks=200, start_ms=600 * 200)
    assert tracker.stats["moves"] == moves_before, tracker.stats
    assert tracker.state == STATE_ALIGNED
    # final alignment error should be inside the deadband
    left, right = simulate_ldr_pair(150, tracker.angle)
    assert abs(right - left) <= TrackerConfig().deadband
    assert out["state"] in (STATE_ALIGNED, STATE_SLEWING_RIGHT, STATE_SLEWING_LEFT)


def test_tracks_a_moving_sun_without_direction_reversal_freakout():
    tracker = SolarTracker(angle=90)
    now = 0
    for i in range(900):
        sun = 90 + 70 * __import__("math").sin(2 * 3.14159 * i / 900)
        left, right = simulate_ldr_pair(sun, tracker.angle)
        tracker.step(now, left, right)
        now += 200
    assert tracker.stats["moves"] > 10
    # smooth sun -> controller should basically never yank the servo backwards
    assert tracker.stats["direction_reversals"] < tracker.stats["moves"] * 0.5


def test_step_size_stays_within_clamps():
    cfg = TrackerConfig()
    tracker = SolarTracker(angle=90)
    now = 0
    biggest = 0.0
    for _ in range(400):
        left, right = simulate_ldr_pair(175, tracker.angle)
        out = tracker.step(now, left, right)
        if out["moved"]:
            biggest = max(biggest, out["step_deg"])
        now += 200
    assert 0 < biggest <= cfg.max_step, biggest
    assert cfg.soft_min <= tracker.angle <= cfg.soft_max


def test_manual_mode_moves_to_the_requested_angle_only():
    tracker = SolarTracker(angle=90)
    tracker.set_mode(MODE_MANUAL)
    tracker.set_manual_target(140)
    now = 0
    for _ in range(20):
        left, right = simulate_ldr_pair(20, tracker.angle)
        out = tracker.step(now, left, right)
        now += 200
        assert out["state"] == "MANUAL"
    assert tracker.angle == 140
    # switching modes must never jump the panel
    before = tracker.angle
    tracker.set_mode(MODE_AUTO)
    left, right = simulate_ldr_pair(20, tracker.angle)
    tracker.step(now, left, right)
    assert tracker.angle == before


def test_manual_target_is_clamped_to_safe_travel():
    tracker = SolarTracker(angle=90)
    assert tracker.set_manual_target(500) == TrackerConfig().soft_max
    assert tracker.set_manual_target(-40) == TrackerConfig().soft_min


def test_sensor_fault_holds_the_panel():
    tracker = SolarTracker(angle=120)
    now = 0
    for _ in range(TrackerConfig().stuck_samples + 5):
        out = tracker.step(now, 0, 512)
        now += 200
    assert out["state"] == STATE_SENSOR_FAULT
    assert tracker.angle == 120, "fault must freeze the actuator"
    assert tracker.fault

    # recovery
    for _ in range(TrackerConfig().filter_window + 2):
        left, right = simulate_ldr_pair(90, tracker.angle)
        out = tracker.step(now, left, right)
        now += 200
    assert out["fault"] is None


def test_park_command():
    tracker = SolarTracker(angle=30)
    tracker.set_mode(MODE_MANUAL)
    tracker.park()
    assert tracker.mode == MODE_MANUAL
    assert tracker.manual_target == TrackerConfig().park_angle
    now = 0
    for _ in range(30):
        left, right = simulate_ldr_pair(0, tracker.angle)
        tracker.step(now, left, right)
        now += 200
    assert tracker.angle == TrackerConfig().park_angle
