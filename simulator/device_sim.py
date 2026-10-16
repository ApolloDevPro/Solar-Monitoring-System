"""Virtual solar-tracker device.

Runs the same control law as the Arduino firmware (``shared.control``),
simulates a slowly moving sun and two LDRs, then feeds telemetry to the
FastAPI backend and executes any remote movement commands waiting for it.

Usage
-----
    python -m simulator.device_sim                       # defaults
    python -m simulator.device_sim --host 127.0.0.1 --port 8000 --period 0.5
    python -m simulator.device_sim --sun 150             # fixed sun azimuth
"""

from __future__ import annotations

import argparse
import math
import random
import time
import uuid
from typing import Any, Dict, Optional

from shared.control import (  # noqa: E402
    MODE_MANUAL,
    SolarTracker,
    TrackerConfig,
    simulate_ldr_pair,
)
from simulator.http_client import Backend  # noqa: E402


class SunModel:
    """Sun azimuth that arcs across the sky (or is pinned with --sun)."""

    def __init__(self, fixed: Optional[float] = None, period_s: float = 90.0) -> None:
        self.fixed = fixed
        self.period_s = period_s
        self.t0 = time.monotonic()

    def azimuth(self) -> float:
        if self.fixed is not None:
            return self.fixed
        # 20° .. 160° sine sweep, one full arc every period_s seconds.
        # phase must NOT be doubled: sin(2*pi*elapsed/period) already has
        # period_s as its period (doubling made it sweep twice as fast).
        phase = (time.monotonic() - self.t0) / self.period_s
        return 90.0 + 70.0 * math.sin(2.0 * math.pi * phase)


def apply_command(tracker: SolarTracker, cmd: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one pending command against the in-memory tracker."""
    ctype = cmd.get("type")
    payload = cmd.get("payload") or {}
    try:
        if ctype == "set_mode":
            mode = payload.get("mode")
            tracker.set_mode(mode)
            return {"status": "EXECUTED", "reason": f"mode is now {tracker.mode}"}
        if ctype == "move":
            angle = payload.get("target_angle")
            if angle is None:
                return {"status": "FAILED", "reason": "target_angle missing"}
            if tracker.mode != MODE_MANUAL:
                return {"status": "REJECTED", "reason": "device is not in MANUAL mode"}
            tracker.set_manual_target(float(angle))
            return {"status": "EXECUTED", "reason": f"manual target {tracker.manual_target}°"}
        if ctype == "park":
            # park() itself forces MANUAL mode (see shared.control.park), so
            # no follow-up set_mode/park is needed here.
            tracker.park()
            return {"status": "EXECUTED", "reason": f"parked at {tracker.manual_target}°"}
        return {"status": "REJECTED", "reason": f"unknown command type '{ctype}'"}
    except ValueError as exc:
        return {"status": "REJECTED", "reason": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        return {"status": "FAILED", "reason": f"execution error: {exc}"}


def run(args: argparse.Namespace) -> int:
    backend = Backend(f"http://{args.host}:{args.port}")
    cfg = TrackerConfig()
    tracker = SolarTracker(cfg, angle=args.angle, mode=args.mode)
    sun = SunModel(fixed=args.sun, period_s=args.sun_period)
    rng = random.Random(args.seed)
    boot_id = uuid.uuid4().hex[:8]
    seq = 0
    last_cmd_id = 0

    print(f"[sim] boot_id={boot_id} mode={tracker.mode} angle={tracker.angle}")
    print(f"[sim] posting telemetry to {backend.base_url} every {args.period}s")

    # wait for the backend so the first samples are not lost silently
    for attempt in range(40):
        health = backend.get("/api/health")
        if health:
            print("[sim] backend is up")
            break
        time.sleep(0.25)
    else:
        print("[sim] WARNING: backend never answered - continuing anyway")

    try:
        while True:
            now_ms = int(time.monotonic() * 1000)
            azimuth = sun.azimuth()
            left, right = simulate_ldr_pair(azimuth, tracker.angle)

            if args.noise:
                left = int(left + rng.gauss(0, args.noise))
                right = int(right + rng.gauss(0, args.noise))
            left = max(0, min(1023, left))
            right = max(0, min(1023, right))

            out = tracker.step(now_ms, left, right)
            seq += 1

            payload = {
                "device_id": args.device_id,
                "ts_ms": now_ms,
                # The firmware reports the *filtered* ADC values (median
                # window), so the simulator must do the same or the two device
                # paths would show different signal character on the dashboard.
                "ldr_left": out["filtered_left"],
                "ldr_right": out["filtered_right"],
                "angle": out["angle"],
                "mode": out["mode"],
                "tracking_state": out["state"],
                "fault": out["fault"],
                "move_reason": out["move_reason"],
                "boot_id": boot_id,
                "seq": seq,
            }
            if not backend.post("/api/telemetry", payload):
                print("[sim] telemetry post failed (backend down?)")

            pending = backend.get(
                f"/api/commands/pending?device_id={args.device_id}&after_id={last_cmd_id}"
            )
            for cmd in (pending or {}).get("items", []):
                result = apply_command(tracker, cmd)
                if backend.post(f"/api/commands/{cmd['id']}/result", result):
                    # Only advance the cursor once the backend acknowledged the
                    # result; otherwise the command would stay PENDING forever
                    # and the moved cursor would prevent it ever being refetched.
                    last_cmd_id = max(last_cmd_id, int(cmd["id"]))
                else:
                    print(
                        f"[sim] failed to report result for #{cmd['id']} "
                        f"(will retry)"
                    )
                print(
                    f"[sim] command #{cmd['id']} {cmd['type']} -> "
                    f"{result['status']}: {result.get('reason')}"
                )

            move = "MOVE" if out["moved"] else "    "
            print(
                f"[sim] {move} sun={azimuth:5.1f}° panel={out['angle']:5.1f}° "
                f"L={out['ldr_left']:4d} R={out['ldr_right']:4d} "
                f"d={out['difference']:+4d} {out['state']:<14}"
                + (f" FAULT={out['fault']}" if out["fault"] else ""),
                end="\r",
            )
            time.sleep(args.period)
    except KeyboardInterrupt:
        print("\n[sim] stopped")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Virtual solar tracker device")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--device-id", default="solar-tracker-01")
    p.add_argument("--period", type=float, default=0.5, help="seconds between ticks")
    p.add_argument("--sun", type=float, default=None, help="fixed sun azimuth (deg)")
    p.add_argument("--sun-period", type=float, default=90.0,
                   help="seconds for one full sun sweep")
    p.add_argument("--noise", type=float, default=4.0, help="Gaussian ADC noise sigma")
    p.add_argument("--angle", type=float, default=90.0)
    p.add_argument("--mode", default="AUTO", choices=("AUTO", "MANUAL"))
    p.add_argument("--seed", type=int, default=42)
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
