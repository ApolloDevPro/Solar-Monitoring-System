"""FastAPI backend for Task 14 - Automatic Solar Tracking System.

Responsibilities
----------------
* ingest telemetry (2 LDR readings + panel angle + mode + tracking state)
* persist light measurements and orientation history
* accept / validate / store remote movement commands
* hand pending commands to the device and record the outcome
* serve the dashboard
"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

load_dotenv()

from backend.database import Database, utcnow  # noqa: E402
from shared.control import MODE_MANUAL, MODES, TrackerConfig  # noqa: E402

CFG = TrackerConfig()

DEVICE_ID = os.getenv("SOLAR_DEVICE_ID", "solar-tracker-01")
OFFLINE_AFTER_S = float(os.getenv("SOLAR_OFFLINE_AFTER", "10"))
COMMAND_EXPIRE_S = float(os.getenv("SOLAR_COMMAND_EXPIRE", "60"))
PRUNE_EVERY = 200
# Monotonic ingest counter; prune is triggered by ingest volume rather than
# the device-supplied ``seq`` so a device that omits or resets ``seq`` can
# never defeat pruning (and unbounded table growth).
_ingest_tick = count()

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"

DB_PATH = os.getenv(
    "SOLAR_DB_PATH", str(Path(__file__).resolve().with_name("solar_tracker.db"))
)
db = Database(DB_PATH)

# ----------------------------------------------------------------------
# rate limiter
# ----------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

# ----------------------------------------------------------------------
# models
# ----------------------------------------------------------------------
class Telemetry(BaseModel):
    device_id: str = Field(default=DEVICE_ID, max_length=64)
    ts_ms: int
    ldr_left: int = Field(ge=0, le=1023)
    ldr_right: int = Field(ge=0, le=1023)
    angle: float = Field(ge=0, le=180)
    mode: Literal["AUTO", "MANUAL"]
    tracking_state: Literal[
        "ALIGNED", "EVALUATING", "SLEWING_LEFT", "SLEWING_RIGHT", "MANUAL", "SENSOR_FAULT"
    ]
    fault: Optional[str] = Field(default=None, max_length=200)
    move_reason: Optional[str] = Field(default=None, max_length=200)
    boot_id: Optional[str] = Field(default=None, max_length=64)
    seq: Optional[int] = Field(default=None, ge=0)


class CommandCreate(BaseModel):
    type: str = Field(max_length=32)
    device_id: str = Field(default=DEVICE_ID, max_length=64)
    target_angle: Optional[float] = None
    mode: Optional[str] = Field(default=None, max_length=16)
    source: str = Field(default="dashboard", max_length=64)


class CommandResult(BaseModel):
    status: str = Field(default="EXECUTED", pattern="^(EXECUTED|REJECTED|FAILED)$")
    reason: Optional[str] = Field(default=None, max_length=200)


# ----------------------------------------------------------------------
# command validation (the "unsafe / impossible command is refused and
# recorded" requirement)
# ----------------------------------------------------------------------
COMMAND_TYPES = ("move", "set_mode", "park")


def validate_command(cmd: CommandCreate) -> List[str]:
    """Return a list of problems.  Empty list == accepted."""
    problems: List[str] = []

    if cmd.type not in COMMAND_TYPES:
        problems.append(
            f"unknown command type '{cmd.type}' (allowed: {', '.join(COMMAND_TYPES)})"
        )
        return problems

    if cmd.type == "move":
        if cmd.target_angle is None:
            problems.append("target_angle is required for a move command")
        elif not (CFG.angle_min <= float(cmd.target_angle) <= CFG.angle_max):
            problems.append(
                f"target angle {cmd.target_angle}° is outside the hardware range "
                f"{CFG.angle_min}-{CFG.angle_max}°"
            )
        elif not (CFG.soft_min <= float(cmd.target_angle) <= CFG.soft_max):
            problems.append(
                f"target angle {cmd.target_angle}° is outside the permitted travel "
                f"limits {CFG.soft_min}-{CFG.soft_max}°"
            )

    if cmd.type == "set_mode":
        if cmd.mode not in MODES:
            problems.append(
                f"invalid mode '{cmd.mode}' (allowed: {', '.join(MODES)})"
            )

    raw_state = db.get_state(cmd.device_id)
    if raw_state is None:
        problems.append(f"device '{cmd.device_id}' has never reported telemetry")
        return problems

    state = _with_presence(raw_state, cmd.device_id)
    if not state["is_online"]:
        problems.append(
            f"device '{cmd.device_id}' is offline "
            f"(last seen {state.get('last_seen') or 'never'})"
        )
        return problems

    if cmd.type == "move" and state["mode"] != MODE_MANUAL:
        problems.append(
            f"device is in {state['mode']} mode - switch to MANUAL before "
            "issuing a direct movement command"
        )

    return problems


# ----------------------------------------------------------------------
# telemetry ingest
# ----------------------------------------------------------------------
def ingest(t: Telemetry) -> Dict[str, Any]:
    now = utcnow()
    previous = db.get_state(t.device_id)

    db.add_light_sample(
        {
            "device_id": t.device_id,
            "ts": now,
            "ts_ms": t.ts_ms,
            "ldr_left": t.ldr_left,
            "ldr_right": t.ldr_right,
            "difference": t.ldr_right - t.ldr_left,
        }
    )

    changed = True
    if previous:
        changed = (
            abs(float(previous["angle"]) - t.angle) > 0.05
            or previous["mode"] != t.mode
            or previous["tracking_state"] != t.tracking_state
            or (previous.get("fault") or None) != (t.fault or None)
        )
    if changed:
        db.add_orientation(
            {
                "device_id": t.device_id,
                "ts": now,
                "angle": t.angle,
                "mode": t.mode,
                "tracking_state": t.tracking_state,
                "reason": t.move_reason,
            }
        )

    db.upsert_state(
        {
            "device_id": t.device_id,
            "mode": t.mode,
            "tracking_state": t.tracking_state,
            "angle": t.angle,
            "ldr_left": t.ldr_left,
            "ldr_right": t.ldr_right,
            "difference": t.ldr_right - t.ldr_left,
            "fault": t.fault,
            "is_online": True,
            "last_seen": now,
            "boot_id": t.boot_id,
            "updated_at": now,
        }
    )

    if next(_ingest_tick) % PRUNE_EVERY == 0:
        db.prune()

    state = db.get_state(t.device_id) or {}
    state["age_seconds"] = 0.0
    return state


# ----------------------------------------------------------------------
# application
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    async def reaper() -> None:
        while True:
            try:
                # Blocking SQLite work must not run on the event loop; offload
                # it so a slow prune / write cannot stall every request.
                await asyncio.to_thread(_reap_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[reaper] {exc}", file=sys.stderr)
            await asyncio.sleep(2)

    task = asyncio.create_task(reaper())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover - expected on shutdown
            pass
        db.close()


def _reap_once() -> None:
    """Expire stale commands and mark silent devices offline (runs in a thread)."""
    cutoff_cmds = (
        datetime.now(timezone.utc) - timedelta(seconds=COMMAND_EXPIRE_S)
    ).isoformat(timespec="milliseconds")
    db.expire_stale_commands(cutoff_cmds)

    cutoff_offline = (
        datetime.now(timezone.utc) - timedelta(seconds=OFFLINE_AFTER_S)
    ).isoformat(timespec="milliseconds")
    db.mark_offline_before(cutoff_offline)


app = FastAPI(
    title="Automatic Solar Tracking System",
    description="Task 14 - Integrated Embedded, IoT, UI & Database",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Apply the ``default_limits`` configured on the Limiter; without this
# middleware only routes carrying an explicit ``@limiter.limit`` decorator
# (i.e. /api/telemetry) were throttled.
app.add_middleware(SlowAPIMiddleware)


def _age_seconds(state: Optional[Dict[str, Any]]) -> Optional[float]:
    if not state or not state.get("last_seen"):
        return None
    try:
        last = datetime.fromisoformat(state["last_seen"])
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - last).total_seconds(), 1)


def _with_presence(
    state: Optional[Dict[str, Any]], device_id: Optional[str] = None
) -> Dict[str, Any]:
    if not state:
        return {"device_id": device_id or DEVICE_ID, "is_online": False, "age_seconds": None}
    state = dict(state)
    age = _age_seconds(state)
    state["age_seconds"] = age
    state["is_online"] = bool(state.get("is_online")) and age is not None and age <= OFFLINE_AFTER_S
    return state


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "device_id": DEVICE_ID, "time": utcnow()}


@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    return {
        "deadband": CFG.deadband,
        "confirm_samples": CFG.confirm_samples,
        "filter_window": CFG.filter_window,
        "settle_ms": CFG.settle_ms,
        "min_step": CFG.min_step,
        "max_step": CFG.max_step,
        "soft_min": CFG.soft_min,
        "soft_max": CFG.soft_max,
        "park_angle": CFG.park_angle,
        "offline_after_s": OFFLINE_AFTER_S,
        "command_expire_s": COMMAND_EXPIRE_S,
        "command_types": list(COMMAND_TYPES),
        "modes": list(MODES),
    }


@app.get("/api/state")
def get_state(
    device_id: str = Query(default=DEVICE_ID, max_length=64),
) -> Dict[str, Any]:
    return _with_presence(db.get_state(device_id), device_id)


@app.get("/api/events")
async def stream_events(
    request: Request,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Stream snapshots with resumable sequence IDs for reconnecting clients."""
    try:
        event_id = max(0, int(last_event_id or 0))
    except ValueError:
        event_id = 0

    def _snapshot() -> Dict[str, Any]:
        # Blocking SQLite reads; called via asyncio.to_thread so the event
        # loop is never stalled by a slow query or a lock held elsewhere.
        return {
            "state": _with_presence(db.get_state(DEVICE_ID), DEVICE_ID),
            "counts": db.counts(),
            "time": utcnow(),
        }

    async def gen():
        nonlocal event_id
        while True:
            try:
                if await request.is_disconnected():
                    break
                payload = await asyncio.to_thread(_snapshot)
                event_id += 1
                yield f"id: {event_id}\ndata: {json.dumps(payload)}\n\n"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - surfaced as an SSE error event
                print(f"[events] {exc}", file=sys.stderr)
                yield (
                    "event: error\ndata: "
                    f"{json.dumps({'error': 'event stream temporarily unavailable'})}\n\n"
                )
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/telemetry")
@limiter.limit("300/minute")
def post_telemetry(
    request: Request,
    t: Telemetry,
) -> Dict[str, Any]:
    return ingest(t)


@app.get("/api/history/light")
def history_light(limit: int = Query(default=300, ge=1, le=5000),
                  device_id: str = Query(default=DEVICE_ID, max_length=64)) -> Dict[str, Any]:
    return {"items": db.recent_light(limit, device_id)}


@app.get("/api/history/orientation")
def history_orientation(
    limit: int = Query(default=200, ge=1, le=2000),
) -> Dict[str, Any]:
    return {"items": db.recent_orientation(limit)}


@app.get("/api/history/faults")
def history_faults(
    limit: int = Query(default=100, ge=1, le=1000),
) -> Dict[str, Any]:
    return {"items": db.recent_faults(limit)}


@app.get("/api/history/commands")
def history_commands(limit: int = Query(default=100, ge=1, le=1000),
                     status: Optional[str] = Query(default=None, max_length=16)) -> Dict[str, Any]:
    return {"items": db.list_commands(limit, status)}


@app.get("/api/summary")
def summary() -> Dict[str, Any]:
    counts = db.counts()
    return {
        "device_id": DEVICE_ID,
        "counts": counts,
        "light_samples": counts.get("light_samples", 0),
        "orientation_events": counts.get("orientation_history", 0),
        "commands_total": counts.get("commands", 0),
        "commands_rejected": counts.get("rejected_commands", 0),
        "commands_executed": counts.get("executed_commands", 0),
        "efficiency": db.efficiency_stats(DEVICE_ID),
    }


@app.post("/api/commands", status_code=201)
def create_command(
    cmd: CommandCreate,
) -> Dict[str, Any]:
    problems = validate_command(cmd)
    payload = cmd.model_dump(exclude={"source", "device_id"})
    if problems:
        row = db.create_command(
            device_id=cmd.device_id,
            cmd_type=cmd.type,
            payload=payload,
            source=cmd.source,
            status="REJECTED",
            reason="; ".join(problems),
        )
        if row is None:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail="failed to record rejected command")
        return {"command": row, "accepted": False, "reason": row["reason"]}

    row = db.create_command(
        device_id=cmd.device_id,
        cmd_type=cmd.type,
        payload=payload,
        source=cmd.source,
        status="PENDING",
        reason=None,
    )
    if row is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="failed to record command")
    return {"command": row, "accepted": True, "reason": None}


@app.get("/api/commands/pending")
@limiter.limit("600/minute")
def pending_commands(
    request: Request,
    device_id: str = Query(default=DEVICE_ID, max_length=64),
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> Dict[str, Any]:
    # Machine-poll endpoint: the serial bridge polls every 0.25s (240 req/min)
    # and the simulator every 0.5s (120 req/min).  Without this decorator the
    # global 200/min default throttles real-hardware command delivery after
    # ~50s of runtime (slowapi route limits override the default when set).
    return {"items": db.pending_commands(device_id, after_id, limit)}


@app.post("/api/commands/{cmd_id}/result")
def command_result(
    cmd_id: int,
    body: CommandResult,
) -> Dict[str, Any]:
    row = db.get_command(cmd_id)
    if row is None:
        raise HTTPException(status_code=404, detail="command not found")
    # Only a PENDING command may transition to a terminal state.  This stops
    # a late / duplicate report from overwriting an EXPIRED or already
    # EXECUTED command (and the 60s expiry reaper races with slow devices).
    if row["status"] != "PENDING":
        raise HTTPException(
            status_code=409,
            detail=f"command already {row['status'].lower()} - cannot update result",
        )
    updated = db.update_command(cmd_id, body.status, body.reason)
    return {"command": updated}


@app.get("/api/commands/{cmd_id}")
@limiter.limit("300/minute")
def get_command(
    request: Request,
    cmd_id: int,
) -> Dict[str, Any]:
    # This route MUST carry a limiter decorator.  slowapi's middleware resolves
    # the route handler by "last full match", so GET /api/commands/pending is
    # attributed to this parametric route ({cmd_id}="pending") whenever the
    # middleware stack evaluates it.  Undecorated, this route made the global
    # 200/min default apply to the bridge's machine-poll URL and throttled
    # real-hardware command delivery.  Decorated routes are skipped by the
    # middleware and limited exactly once by their own decorator.
    row = db.get_command(cmd_id)
    if row is None:
        raise HTTPException(status_code=404, detail="command not found")
    return {"command": row}


@app.delete("/api/commands/pending")
def clear_pending_commands(
    device_id: str = Query(default=DEVICE_ID, max_length=64),
) -> Dict[str, int]:
    return {"cancelled": db.cancel_pending_commands(device_id)}


@app.delete("/api/history")
def clear_history() -> Dict[str, Dict[str, int]]:
    return {"deleted": db.clear_history()}


# dashboard is mounted last so that /api/* keeps priority
app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")
