"""Serial bridge: real Arduino <-> FastAPI backend.

Reads telemetry lines from the Arduino (115200 baud), POSTs them to the
backend, then polls pending dashboard commands and forwards them to the
board over Serial.  Command answers (OK / ERR) are reported back.

Arduino line formats
--------------------
  T,<ts_ms>,<ldr_left>,<ldr_right>,<angle>,<mode>,<state>,<fault>,<reason>,<boot_id>
  OK <text> / ERR <text>
  STATUS ... / STATS ...

Usage
-----
    python -m simulator.serial_bridge                 # auto-detect COM port
    python -m simulator.serial_bridge --port COM5
    python -m simulator.serial_bridge --list          # list serial ports
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any, Deque, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    print("pyserial is required:  pip install pyserial")
    raise SystemExit(1)

from simulator.http_client import Backend  # noqa: E402  (single HTTP client)

log = logging.getLogger("solar.bridge")


def configure_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """Console + optional rotating-file logging for the bridge.

    A field deployment runs unattended, so stdout alone is not enough: this
    writes ``logs/serial_bridge.log`` (1 MB x 3 backups) in addition to the
    console.  Safe to call more than once (handlers are not duplicated).
    """
    root = logging.getLogger("solar")
    root.setLevel(level.upper())
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


# ---------------------------------------------------------- serial ports ---
def list_serial() -> List[Any]:
    return list(list_ports.comports())


def pick_port(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    ports = list_serial()
    if not ports:
        raise SystemExit("no serial ports found - is the Arduino plugged in?")
    # prefer known Arduino USB adapters
    for p in ports:
        desc = (p.description or "").lower()
        if any(k in desc for k in ("arduino", "ch340", "cp210", "usb serial", "wch")):
            log.info("auto-selected %s (%s)", p.device, p.description)
            return p.device
    log.info("auto-selected %s (%s)", ports[0].device, ports[0].description)
    return ports[0].device


# ------------------------------------------------------------ protocol -----
def parse_telemetry(parts: List[str], device_id: str) -> Optional[Dict[str, Any]]:
    """T,ts,left,right,angle,mode,state,fault,reason -> API payload."""
    if len(parts) < 7:
        return None
    try:
        ts_ms = int(float(parts[1]))
        left = max(0, min(1023, int(float(parts[2]))))
        right = max(0, min(1023, int(float(parts[3]))))
        angle = max(0.0, min(180.0, float(parts[4])))
    except ValueError:
        return None
    mode = parts[5].strip().upper()
    state = parts[6].strip()
    if mode not in ("AUTO", "MANUAL"):
        mode = "AUTO"
    fault = parts[7].strip() if len(parts) > 7 else ""
    reason = parts[8].strip() if len(parts) > 8 else ""
    boot_id = parts[9].strip() if len(parts) > 9 else None
    return {
        "device_id": device_id,
        "ts_ms": ts_ms,
        "ldr_left": left,
        "ldr_right": right,
        "angle": angle,
        "mode": mode,
        "tracking_state": state,
        "fault": fault or None,
        "move_reason": reason or None,
        "boot_id": boot_id or None,
        "seq": None,
    }


def command_to_serial(cmd: Dict[str, Any]) -> Optional[str]:
    ctype = cmd.get("type")
    payload = cmd.get("payload") or {}
    if ctype == "set_mode":
        return f"MODE {payload.get('mode', 'AUTO')}"
    if ctype == "move":
        angle = payload.get("target_angle")
        return f"MOVE {angle}" if angle is not None else None
    if ctype == "park":
        return "PARK"
    return None


# answer plumbing: FIFO of command ids waiting for an OK/ERR line.  The
# sketch is single threaded and answers in order, so matching is strictly
# first-in-first-out; a per-entry deadline keeps a lost ack from swallowing
# the answer to a much later command.
_ACK_TTL_S = 10.0
_WAITING: Deque[Dict[str, Any]] = deque()


def _queue_ack(cmd_id: int) -> None:
    _WAITING.append({"cmd_id": cmd_id, "deadline": time.monotonic() + _ACK_TTL_S})


def _answer_pending(backend: Backend, status: str, reason: str) -> None:
    now = time.monotonic()
    while _WAITING and _WAITING[0]["deadline"] < now:
        stale = _WAITING.popleft()
        log.warning("<- #%s ack timed out", stale["cmd_id"])
    if not _WAITING:
        return
    entry = _WAITING.popleft()
    backend.post(f"/api/commands/{entry['cmd_id']}/result", {"status": status, "reason": reason})
    log.info("<- #%s %s: %s", entry["cmd_id"], status, reason)


# ------------------------------------------------------- telemetry queue ---
def enqueue_telemetry(
    queue: Deque[Dict[str, Any]], payload: Dict[str, Any]
) -> None:
    """Buffer a sample the backend refused (429 / outage) for a later retry.

    ``queue`` is a ``deque(maxlen=...)`` so memory is bounded; the oldest
    sample is dropped first (live data beats stale data on a solar tracker).
    """
    queue.append(payload)
    log.warning("telemetry post failed - queued (depth=%d)", len(queue))


def flush_telemetry(
    backend: Backend,
    queue: Deque[Dict[str, Any]],
    now: float,
    last_flush: float,
    min_interval_s: float,
) -> float:
    """Retry at most ONE queued sample per ``min_interval_s`` window.

    Fresh reports already run at 240/min against a 300/min backend limit, so
    only ~60/min of retry headroom exists - flushing faster than ~1 sample
    per 1.5s would trade live samples for stale ones.  Returns the new
    ``last_flush`` timestamp (unchanged when nothing was sent or the backend
    is still down; the queue keeps its order either way).
    """
    if not queue or (now - last_flush) < min_interval_s:
        return last_flush
    if backend.post("/api/telemetry", queue[0]):
        queue.popleft()
        if queue:
            log.info("flushed 1 queued sample (depth=%d)", len(queue))
        else:
            log.info("flushed last queued sample - queue empty")
        return now
    log.debug("flush skipped - backend still unreachable")
    return last_flush


# ---------------------------------------------------------------- main -----
def run(args: argparse.Namespace) -> int:
    configure_logging(args.log_level, args.log_file)

    if args.list:
        ports = list_serial()
        if not ports:
            log.info("no serial ports found")
        for p in ports:
            log.info("  %s  %s", f"{p.device:8s}", p.description)
        return 0

    backend = Backend(f"http://{args.host}:{args.port}")
    device_id = args.device_id

    for _ in range(40):
        if backend.get("/api/health"):
            break
        time.sleep(0.25)
    else:
        log.error("backend not reachable at %s - start it first", backend.base_url)
        return 1

    port = pick_port(args.port_name)

    seq = 0
    pending_after = 0
    last_health = 0.0
    last_flush = 0.0
    tx_buf = ""
    reconnect_delay = 1.0
    # Samples the backend refused (outage / rate limit) - retried later at
    # a paced rate so retries never starve fresh telemetry.
    retry_queue: Deque[Dict[str, Any]] = deque(maxlen=args.queue_size)

    while True:  # reconnect loop: a USB blip must not kill the bridge
        try:
            ser = serial.Serial(port, 115200, timeout=2)
        except serial.SerialException as exc:
            log.error("cannot open %s: %s", port, exc)
            log.error("close the Arduino Serial Monitor first, then retry")
            return 1

        log.info("serial %s @115200 <-> %s", port, backend.base_url)
        log.info("device_id=%s queue<=%d flush every %.1fs",
                 device_id, args.queue_size, args.flush_interval)
        time.sleep(2.0)          # auto-reset after opening the port
        ser.reset_input_buffer()
        reconnect_delay = 1.0

        try:
            while True:
                now = time.monotonic()

                # ---- serial -> backend ------------------------------------
                try:
                    raw = ser.read(ser.in_waiting or 1).decode("utf-8", errors="replace")
                except serial.SerialException as exc:
                    raise _SerialLost(exc) from exc
                tx_buf += raw
                while "\n" in tx_buf:
                    line, tx_buf = tx_buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    if line.startswith("T,"):
                        payload = parse_telemetry(line.split(","), device_id)
                        if payload:
                            seq += 1
                            payload["seq"] = seq
                            if not backend.post("/api/telemetry", payload):
                                enqueue_telemetry(retry_queue, payload)
                    elif line.startswith("OK"):
                        _answer_pending(backend, "EXECUTED", line[2:].strip() or "accepted")
                    elif line.startswith("ERR"):
                        _answer_pending(backend, "REJECTED", line[3:].strip() or "rejected")

                # ---- retry previously dropped samples (paced) --------------
                last_flush = flush_telemetry(
                    backend, retry_queue, now, last_flush, args.flush_interval
                )

                # ---- backend -> serial (pending commands) -----------------
                data = backend.get(
                    f"/api/commands/pending?device_id={device_id}&after_id={pending_after}&limit=10"
                )
                for cmd in (data or {}).get("items", []):
                    serial_line = command_to_serial(cmd)
                    if serial_line is None:
                        # Unserialisable commands are rejected immediately and
                        # the cursor advances past them.
                        pending_after = max(pending_after, int(cmd["id"]))
                        backend.post(
                            f"/api/commands/{cmd['id']}/result",
                            {"status": "REJECTED", "reason": "unsupported command type"},
                        )
                        continue
                    log.info("-> #%s %s", cmd["id"], serial_line)
                    try:
                        ser.write((serial_line + "\n").encode())
                        ser.flush()
                    except serial.SerialException as exc:
                        # Do NOT advance pending_after: after the reconnect
                        # this command is re-fetched and re-sent.
                        raise _SerialLost(exc) from exc
                    _queue_ack(int(cmd["id"]))
                    pending_after = max(pending_after, int(cmd["id"]))

                # ---- watchdog: keep the device "online" even if idle ------
                if now - last_health > 5.0:
                    last_health = now
                    state = backend.get(f"/api/state?device_id={device_id}")
                    if state and not state.get("is_online"):
                        log.info("waiting for telemetry from the board...")

                time.sleep(args.poll)
        except _SerialLost as exc:
            log.warning("serial lost: %s - reconnecting in %.0fs",
                        exc.reason, reconnect_delay)
            _WAITING.clear()
            tx_buf = ""
        except KeyboardInterrupt:
            log.info("stopped")
            return 0
        finally:
            try:
                ser.close()
            except Exception:  # pragma: no cover - port already gone
                pass

        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 15.0)


class _SerialLost(Exception):
    """Internal signal: the serial port dropped; reconnect instead of exiting."""

    def __init__(self, reason: Exception) -> None:
        super().__init__(str(reason))
        self.reason = reason


def main() -> int:
    p = argparse.ArgumentParser(description="Arduino serial <-> backend bridge")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--port-name", default=None, help="e.g. COM5")
    p.add_argument("--device-id", default="solar-tracker-01")
    p.add_argument("--poll", type=float, default=0.25)
    p.add_argument("--list", action="store_true", help="list serial ports")
    p.add_argument("--log-file", default="logs/serial_bridge.log",
                   help="rotating log path (1 MB x 3); use '' to disable")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--queue-size", type=int, default=200,
                   help="ring buffer for telemetry the backend refused")
    p.add_argument("--flush-interval", type=float, default=1.5,
                   help="seconds between retries of queued telemetry "
                        "(keeps retries under the backend rate limit)")
    args = p.parse_args()
    if args.log_file == "":
        args.log_file = None
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
