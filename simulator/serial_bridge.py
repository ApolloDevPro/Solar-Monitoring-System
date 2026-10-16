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
import time
from collections import deque
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
            print(f"[bridge] auto-selected {p.device} ({p.description})")
            return p.device
    print(f"[bridge] auto-selected {ports[0].device} ({ports[0].description})")
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
        print(f"[bridge] <- #{stale['cmd_id']} ack timed out")
    if not _WAITING:
        return
    entry = _WAITING.popleft()
    backend.post(f"/api/commands/{entry['cmd_id']}/result", {"status": status, "reason": reason})
    print(f"[bridge] <- #{entry['cmd_id']} {status}: {reason}")


# ---------------------------------------------------------------- main -----
def run(args: argparse.Namespace) -> int:
    if args.list:
        ports = list_serial()
        if not ports:
            print("no serial ports found")
        for p in ports:
            print(f"  {p.device:8s} {p.description}")
        return 0

    backend = Backend(f"http://{args.host}:{args.port}")
    device_id = args.device_id

    for _ in range(40):
        if backend.get("/api/health"):
            break
        time.sleep(0.25)
    else:
        print(f"[bridge] backend not reachable at {backend.base_url} - start it first")
        return 1

    port = pick_port(args.port_name)

    seq = 0
    pending_after = 0
    last_health = 0.0
    tx_buf = ""
    reconnect_delay = 1.0

    while True:  # reconnect loop: a USB blip must not kill the bridge
        try:
            ser = serial.Serial(port, 115200, timeout=2)
        except serial.SerialException as exc:
            print(f"[bridge] cannot open {port}: {exc}")
            print("[bridge] close the Arduino Serial Monitor first, then retry")
            return 1

        print(f"[bridge] serial {port} @115200 <-> {backend.base_url}")
        print(f"[bridge] device_id={device_id}")
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
                                print("[bridge] telemetry post failed")
                    elif line.startswith("OK"):
                        _answer_pending(backend, "EXECUTED", line[2:].strip() or "accepted")
                    elif line.startswith("ERR"):
                        _answer_pending(backend, "REJECTED", line[3:].strip() or "rejected")

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
                    print(f"[bridge] -> #{cmd['id']} {serial_line}")
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
                        print("[bridge] waiting for telemetry from the board...")

                time.sleep(args.poll)
        except _SerialLost as exc:
            print(f"[bridge] serial lost: {exc.reason} - reconnecting in {reconnect_delay:.0f}s")
            _WAITING.clear()
            tx_buf = ""
        except KeyboardInterrupt:
            print("\n[bridge] stopped")
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
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
