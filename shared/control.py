"""Reference implementation of the Automatic Solar Tracking control law.

This module is the single source of truth for the embedded control
behaviour.  ``firmware/solar_tracker/solar_tracker.ino`` implements the
same algorithm in C++ and ``simulator/device_sim.py`` imports this module
so the whole system can be demonstrated without physical hardware.

Anti-oscillation strategy (the core requirement of Task 14)
-----------------------------------------------------------
1. Median filter over the last ``filter_window`` ADC samples kills
   isolated noise spikes.
2. A ``deadband`` around equal readings means "nearly equal" light
   simply produces no motion at all.
3. ``confirm_samples`` consecutive out-of-deadband samples pointing in
   the *same* direction are required before the servo is allowed to
   move, so a jittering differential can never cause a left/right
   dither.
4. After every move the controller waits ``settle_ms`` before looking
   again, because moving the panel changes the geometry that the two
   LDRs see.
5. Step size is proportional to the light imbalance but clamped to
   ``min_step``..``max_step`` so the panel approaches the target
   monotonically instead of overshooting past it.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, Dict, Optional

MODE_AUTO = "AUTO"
MODE_MANUAL = "MANUAL"
MODES = (MODE_AUTO, MODE_MANUAL)

STATE_ALIGNED = "ALIGNED"
STATE_EVALUATING = "EVALUATING"
STATE_SLEWING_LEFT = "SLEWING_LEFT"
STATE_SLEWING_RIGHT = "SLEWING_RIGHT"
STATE_MANUAL = "MANUAL"
STATE_SENSOR_FAULT = "SENSOR_FAULT"

TRACKING_STATES = (
    STATE_ALIGNED,
    STATE_EVALUATING,
    STATE_SLEWING_LEFT,
    STATE_SLEWING_RIGHT,
    STATE_MANUAL,
    STATE_SENSOR_FAULT,
)

DIR_LEFT = -1
DIR_NONE = 0
DIR_RIGHT = 1


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


@dataclass
class TrackerConfig:
    """Tunable thresholds / timing.  Mirrors the #defines in the sketch."""

    angle_min: int = 0
    angle_max: int = 180
    soft_min: int = 10          # mechanical safe travel limits
    soft_max: int = 170
    park_angle: int = 90        # neutral position used on fault / park
    adc_min: int = 0
    adc_max: int = 1023
    deadband: int = 30          # ADC counts treated as "equal light"
    confirm_samples: int = 3    # consecutive agreeing samples to move
    filter_window: int = 5      # median filter length
    settle_ms: int = 400        # pause after each servo move
    min_step: float = 2.0       # degrees
    max_step: float = 15.0      # degrees
    stuck_low: int = 2          # ADC rail values treated as a fault
    stuck_high: int = 1021
    stuck_samples: int = 3      # consecutive filtered rail samples -> fault

    @property
    def full_span(self) -> float:
        return float(self.adc_max - self.adc_min)


class SolarTracker:
    """Deterministic, tick driven tracker (no wall-clock dependency)."""

    def __init__(
        self,
        config: Optional[TrackerConfig] = None,
        angle: float = 90.0,
        mode: str = MODE_AUTO,
    ) -> None:
        self.cfg = config or TrackerConfig()
        self.angle = float(clamp(angle, self.cfg.soft_min, self.cfg.soft_max))
        self.mode = mode if mode in MODES else MODE_AUTO
        self.manual_target = self.angle
        self.state = STATE_ALIGNED
        self.fault: Optional[str] = None
        self.filtered_left = 0
        self.filtered_right = 0
        self.last_diff = 0
        self.last_step_deg = 0.0
        self.last_move_reason = "boot"
        self.settle_until_ms = 0

        self._left_buf: Deque[int] = deque(maxlen=self.cfg.filter_window)
        self._right_buf: Deque[int] = deque(maxlen=self.cfg.filter_window)
        self._pending_dir = DIR_NONE
        self._confirm = 0
        self._l_stuck = 0
        self._r_stuck = 0

        self.stats: Dict[str, int] = {
            "moves": 0,
            "direction_reversals": 0,
            "manual_moves": 0,
            "faults": 0,
            "samples": 0,
        }
        self._last_move_dir = DIR_NONE

    # ------------------------------------------------------------------
    # configuration helpers
    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise ValueError(f"invalid mode {mode!r}")
        if mode != self.mode:
            self.mode = mode
            if mode == MODE_MANUAL:
                # freeze where we are so switching modes never moves the panel
                self.manual_target = self.angle
            self._pending_dir = DIR_NONE
            self._confirm = 0
            self.last_move_reason = f"mode -> {mode}"
        return self.mode

    def set_manual_target(self, angle: float) -> float:
        self.manual_target = float(
            clamp(angle, self.cfg.soft_min, self.cfg.soft_max)
        )
        return self.manual_target

    def park(self) -> float:
        # Match the firmware contract: a park command explicitly forces
        # manual mode and points the panel at the neutral parking angle.
        self.set_mode(MODE_MANUAL)
        self.manual_target = float(clamp(self.cfg.park_angle, self.cfg.soft_min, self.cfg.soft_max))
        self.last_move_reason = "park"
        return self.manual_target

    # ------------------------------------------------------------------
    # main control tick
    # ------------------------------------------------------------------
    def step(self, now_ms: int, ldr_left: int, ldr_right: int) -> Dict:
        cfg = self.cfg
        raw_l = int(clamp(ldr_left, cfg.adc_min, cfg.adc_max))
        raw_r = int(clamp(ldr_right, cfg.adc_min, cfg.adc_max))

        self._left_buf.append(raw_l)
        self._right_buf.append(raw_r)
        fl = int(statistics.median(self._left_buf))
        fr = int(statistics.median(self._right_buf))
        self.filtered_left = fl
        self.filtered_right = fr
        self.last_diff = fr - fl
        self.stats["samples"] += 1

        self._update_fault(fl, fr)

        moved = False
        if self.fault:
            moved = self._hold(STATE_SENSOR_FAULT, "sensor fault - holding")
        elif now_ms < self.settle_until_ms:
            # still settling: keep the current state, do not move
            pass
        elif self.mode == MODE_MANUAL:
            moved = self._manual_tick(now_ms)
        else:
            moved = self._auto_tick(now_ms)

        return {
            "ts_ms": now_ms,
            "mode": self.mode,
            "state": self.state,
            "angle": round(self.angle, 1),
            "ldr_left": raw_l,
            "ldr_right": raw_r,
            "filtered_left": fl,
            "filtered_right": fr,
            "difference": fr - fl,
            "moved": moved,
            "step_deg": self.last_step_deg if moved else 0.0,
            "move_reason": self.last_move_reason,
            "fault": self.fault,
            "settle_remaining_ms": max(0, self.settle_until_ms - now_ms),
        }

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _hold(self, state: str, reason: str) -> bool:
        if self.state != state:
            self.state = state
            self.last_move_reason = reason
        return False

    def _manual_tick(self, now_ms: int) -> bool:
        self.state = STATE_MANUAL
        if abs(self.manual_target - self.angle) < 0.5:
            self.last_move_reason = "manual hold"
            return False
        direction = DIR_RIGHT if self.manual_target > self.angle else DIR_LEFT
        new_angle = self.manual_target
        moved = self._commit_move(new_angle, now_ms, "manual move")
        if moved:
            self._note_direction(direction)
            self.stats["manual_moves"] += 1
        return moved

    def _auto_tick(self, now_ms: int) -> bool:
        cfg = self.cfg
        diff = self.last_diff

        if abs(diff) <= cfg.deadband:
            # light is "nearly equal" -> deliberately do nothing
            self._pending_dir = DIR_NONE
            self._confirm = 0
            return self._hold(STATE_ALIGNED, "inside deadband - holding")

        direction = DIR_RIGHT if diff > 0 else DIR_LEFT
        if direction != self._pending_dir:
            self._pending_dir = direction
            self._confirm = 1
        else:
            self._confirm += 1

        if self._confirm < cfg.confirm_samples:
            # not enough agreement yet: reject the (possibly noisy) input
            return self._hold(STATE_EVALUATING, "awaiting confirmation")

        fraction = clamp(
            (abs(diff) - cfg.deadband) / (cfg.full_span - cfg.deadband),
            0.0,
            1.0,
        )
        step = cfg.min_step + (cfg.max_step - cfg.min_step) * fraction
        target = clamp(self.angle + direction * step, cfg.soft_min, cfg.soft_max)

        if abs(target - self.angle) < 0.5:
            self._pending_dir = DIR_NONE
            self._confirm = 0
            return self._hold(STATE_ALIGNED, "at travel limit")

        moved = self._commit_move(target, now_ms, "auto track")
        if moved:
            self._note_direction(direction)
            self.state = (
                STATE_SLEWING_RIGHT if direction == DIR_RIGHT else STATE_SLEWING_LEFT
            )
            self.last_move_reason = "auto track"
            self._pending_dir = DIR_NONE
            self._confirm = 0
        return moved

    def _commit_move(self, target: float, now_ms: int, reason: str) -> bool:
        cfg = self.cfg
        target = clamp(target, cfg.soft_min, cfg.soft_max)
        if abs(target - self.angle) < 0.5:
            return False
        self.last_step_deg = abs(target - self.angle)
        self.angle = float(target)
        self.settle_until_ms = now_ms + cfg.settle_ms
        self.last_move_reason = reason
        self.stats["moves"] += 1
        return True

    def _note_direction(self, direction: int) -> None:
        if self._last_move_dir != DIR_NONE and direction != self._last_move_dir:
            self.stats["direction_reversals"] += 1
        self._last_move_dir = direction

    def _update_fault(self, fl: int, fr: int) -> None:
        """Detect a dead / shorted LDR.

        Assumption (documented engineering decision): an LDR whose
        *filtered* output is welded to an ADC rail while its twin still
        reports a healthy value is a wiring fault, not a lighting
        condition.  The median filter means a single noise spike can
        never raise the alarm.
        """
        cfg = self.cfg
        left_bad = fl <= cfg.stuck_low or fl >= cfg.stuck_high
        right_bad = fr <= cfg.stuck_low or fr >= cfg.stuck_high

        if left_bad != right_bad:            # exactly one sensor is dead
            self._l_stuck = self._l_stuck + 1 if left_bad else 0
            self._r_stuck = self._r_stuck + 1 if right_bad else 0
        else:                                # both healthy or both at rail
            self._l_stuck = 0
            self._r_stuck = 0

        reason = None
        if self._l_stuck >= cfg.stuck_samples:
            reason = "left LDR stuck at ADC rail"
        elif self._r_stuck >= cfg.stuck_samples:
            reason = "right LDR stuck at ADC rail"

        if reason and not self.fault:
            self.fault = reason
            self.stats["faults"] += 1
            # NOTE: we deliberately do NOT touch manual_target here.  Setting
            # it to park would (a) be a no-op in AUTO (auto ticks ignore it)
            # and (b) cause an unrequested slew to the park angle when a
            # latched fault later clears while in MANUAL mode.  The fault
            # itself already freezes the actuator via _hold().
        elif not reason and self.fault:
            self.fault = None

    # ------------------------------------------------------------------
    # serialisation
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict:
        data = {
            "mode": self.mode,
            "state": self.state,
            "angle": round(self.angle, 1),
            "manual_target": round(self.manual_target, 1),
            "fault": self.fault,
            "filtered_left": self.filtered_left,
            "filtered_right": self.filtered_right,
            "difference": self.last_diff,
            "stats": dict(self.stats),
            "config": asdict(self.cfg),
        }
        return data


def simulate_ldr_pair(
    sun_azimuth_deg: float,
    panel_angle_deg: float,
    ambient: int = 80,
    peak: int = 700,
    half_angle: float = 30.0,
) -> "tuple[int, int]":
    """Physical model of the two LDRs either side of a shading fin.

    The fin splits the panel; the sensor facing the sun reads brighter.
    Returns ``(left, right)`` in 10-bit ADC counts.
    """
    delta = math.radians(sun_azimuth_deg - panel_angle_deg)
    gamma = math.radians(half_angle)
    left = ambient + peak * max(0.0, math.cos(delta + gamma))
    right = ambient + peak * max(0.0, math.cos(delta - gamma))
    return (
        int(clamp(round(left), 0, 1023)),
        int(clamp(round(right), 0, 1023)),
    )
