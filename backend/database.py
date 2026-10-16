"""SQLite persistence for the Automatic Solar Tracking System.

Tables
------
* ``device_state``        - live snapshot of the tracker
* ``light_samples``       - every LDR reading reported by the device
* ``orientation_history`` - every panel angle / mode / state transition
* ``commands``            - remote movement commands, accepted or rejected
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent / "solar_tracker.db"

# keep the database bounded so a long running demo never fills the disk
MAX_ROWS = {
    "light_samples": 20_000,
    "orientation_history": 10_000,
    "commands": 5_000,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_state (
    device_id      TEXT PRIMARY KEY,
    mode           TEXT NOT NULL,
    tracking_state TEXT NOT NULL,
    angle          REAL NOT NULL,
    ldr_left       INTEGER,
    ldr_right      INTEGER,
    difference     INTEGER,
    fault          TEXT,
    is_online      INTEGER NOT NULL DEFAULT 0,
    last_seen      TEXT,
    boot_id        TEXT,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS light_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    ts_ms      INTEGER,
    ldr_left   INTEGER NOT NULL,
    ldr_right  INTEGER NOT NULL,
    difference INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_light_ts ON light_samples(ts);
CREATE INDEX IF NOT EXISTS idx_light_device ON light_samples(device_id, id);

CREATE TABLE IF NOT EXISTS orientation_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      TEXT NOT NULL,
    ts             TEXT NOT NULL,
    angle          REAL NOT NULL,
    mode           TEXT NOT NULL,
    tracking_state TEXT NOT NULL,
    reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_orient_ts ON orientation_history(ts);
CREATE INDEX IF NOT EXISTS idx_orient_device ON orientation_history(device_id, id);

CREATE TABLE IF NOT EXISTS commands (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    source     TEXT NOT NULL,
    status     TEXT NOT NULL,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmd_status ON commands(status, id);
CREATE INDEX IF NOT EXISTS idx_cmd_ts ON commands(ts);
CREATE INDEX IF NOT EXISTS idx_cmd_device_status ON commands(device_id, status, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        # Ensure the parent directory exists so a bare ``docker run`` (no
        # volume mounted at /app/data) does not crash at import time.
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Wait up to 5s for a write lock instead of failing instantly with
            # SQLITE_BUSY when a second writer (worker / CLI) contends.
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # device state
    # ------------------------------------------------------------------
    def upsert_state(self, state: Dict[str, Any]) -> None:
        self._exec(
            """
            INSERT INTO device_state
              (device_id, mode, tracking_state, angle, ldr_left, ldr_right,
               difference, fault, is_online, last_seen, boot_id, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
              mode=excluded.mode,
              tracking_state=excluded.tracking_state,
              angle=excluded.angle,
              ldr_left=excluded.ldr_left,
              ldr_right=excluded.ldr_right,
              difference=excluded.difference,
              fault=excluded.fault,
              is_online=excluded.is_online,
              last_seen=excluded.last_seen,
              boot_id=excluded.boot_id,
              updated_at=excluded.updated_at
            """,
            (
                state["device_id"],
                state["mode"],
                state["tracking_state"],
                state["angle"],
                state.get("ldr_left"),
                state.get("ldr_right"),
                state.get("difference"),
                state.get("fault"),
                1 if state.get("is_online") else 0,
                state.get("last_seen"),
                state.get("boot_id"),
                state.get("updated_at") or utcnow(),
            ),
        )

    def get_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM device_state WHERE device_id=?", (device_id,))

    def set_online(self, device_id: str, online: bool) -> None:
        self._exec(
            "UPDATE device_state SET is_online=?, updated_at=? WHERE device_id=?",
            (1 if online else 0, utcnow(), device_id),
        )

    def mark_offline_before(self, cutoff_iso: str) -> int:
        """Flip ``is_online`` to 0 for every device silent since ``cutoff_iso``."""
        cur = self._exec(
            """
            UPDATE device_state
               SET is_online=0, updated_at=?
             WHERE is_online=1 AND last_seen IS NOT NULL AND last_seen < ?
            """,
            (utcnow(), cutoff_iso),
        )
        return cur.rowcount

    # ------------------------------------------------------------------
    # telemetry
    # ------------------------------------------------------------------
    def add_light_sample(self, sample: Dict[str, Any]) -> int:
        cur = self._exec(
            """
            INSERT INTO light_samples
              (device_id, ts, ts_ms, ldr_left, ldr_right, difference)
            VALUES (?,?,?,?,?,?)
            """,
            (
                sample["device_id"],
                sample["ts"],
                sample.get("ts_ms"),
                sample["ldr_left"],
                sample["ldr_right"],
                sample["difference"],
            ),
        )
        return int(cur.lastrowid or 0)

    def add_orientation(self, row: Dict[str, Any]) -> int:
        cur = self._exec(
            """
            INSERT INTO orientation_history
              (device_id, ts, angle, mode, tracking_state, reason)
            VALUES (?,?,?,?,?,?)
            """,
            (
                row["device_id"],
                row["ts"],
                row["angle"],
                row["mode"],
                row["tracking_state"],
                row.get("reason"),
            ),
        )
        return int(cur.lastrowid or 0)

    def recent_light(self, limit: int = 300, device_id: Optional[str] = None) -> List[Dict]:
        if device_id:
            return self._query(
                "SELECT * FROM light_samples WHERE device_id=? ORDER BY id DESC LIMIT ?",
                (device_id, limit),
            )[::-1]
        return self._query(
            "SELECT * FROM light_samples ORDER BY id DESC LIMIT ?", (limit,)
        )[::-1]

    def recent_orientation(self, limit: int = 200) -> List[Dict]:
        return self._query(
            "SELECT * FROM orientation_history ORDER BY id DESC LIMIT ?", (limit,)
        )[::-1]

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def create_command(
        self,
        device_id: str,
        cmd_type: str,
        payload: Dict[str, Any],
        source: str,
        status: str,
        reason: Optional[str],
    ) -> Dict[str, Any]:
        now = utcnow()
        cur = self._exec(
            """
            INSERT INTO commands
              (device_id, ts, updated_at, type, payload, source, status, reason)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (device_id, now, now, cmd_type, json.dumps(payload), source, status, reason),
        )
        return self.get_command(int(cur.lastrowid or 0))  # type: ignore[return-value]

    def get_command(self, cmd_id: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM commands WHERE id=?", (cmd_id,))
        return self._decode(row)

    def list_commands(self, limit: int = 100, status: Optional[str] = None) -> List[Dict]:
        if status:
            rows = self._query(
                "SELECT * FROM commands WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [self._decode(r) for r in rows]

    def pending_commands(self, device_id: str, after_id: int = 0, limit: int = 20) -> List[Dict]:
        rows = self._query(
            """
            SELECT * FROM commands
             WHERE device_id=? AND status='PENDING' AND id>?
             ORDER BY id ASC LIMIT ?
            """,
            (device_id, after_id, limit),
        )
        return [self._decode(r) for r in rows]

    def update_command(
        self, cmd_id: int, status: str, reason: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        # State machine: only a PENDING command may be moved to a terminal
        # status.  The WHERE guard makes this atomic even if two report
        # requests race (e.g. a slow device vs. the expiry reaper).
        self._exec(
            "UPDATE commands SET status=?, reason=?, updated_at=? "
            "WHERE id=? AND status='PENDING'",
            (status, reason, utcnow(), cmd_id),
        )
        return self.get_command(cmd_id)

    def expire_stale_commands(self, older_than_iso: str) -> int:
        cur = self._exec(
            """
            UPDATE commands
               SET status='EXPIRED',
                   reason='device did not acknowledge the command in time',
                   updated_at=?
             WHERE status='PENDING' AND ts < ?
            """,
            (utcnow(), older_than_iso),
        )
        return cur.rowcount

    @staticmethod
    def _decode(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        row = dict(row)
        try:
            row["payload"] = json.loads(row.get("payload") or "{}")
        except json.JSONDecodeError:
            row["payload"] = {}
        return row

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    def counts(self) -> Dict[str, int]:
        out = {}
        for table in MAX_ROWS:
            row = self._one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        row = self._one("SELECT COUNT(*) AS n FROM commands WHERE status='REJECTED'")
        out["rejected_commands"] = int(row["n"]) if row else 0
        row = self._one("SELECT COUNT(*) AS n FROM commands WHERE status='EXECUTED'")
        out["executed_commands"] = int(row["n"]) if row else 0
        return out

    def prune(self) -> None:
        for table, keep in MAX_ROWS.items():
            if table == "commands":
                # Never drop PENDING commands: they are queued work that has
                # not been executed yet, and silently deleting them would lose
                # actuator commands and leave a hole in the audit trail.
                self._exec(
                    """
                    DELETE FROM commands
                     WHERE status != 'PENDING'
                       AND id NOT IN (
                             SELECT id FROM commands
                              WHERE status != 'PENDING'
                              ORDER BY id DESC LIMIT ?
                       )
                    """,
                    (keep,),
                )
            else:
                self._exec(
                    f"""
                    DELETE FROM {table}
                     WHERE id NOT IN (
                           SELECT id FROM {table} ORDER BY id DESC LIMIT ?
                     )
                    """,
                    (keep,),
                )

    def recent_faults(self, limit: int = 100) -> List[Dict]:
        """Return orientation_history rows where tracking_state = SENSOR_FAULT."""
        return self._query(
            "SELECT * FROM orientation_history "
            "WHERE tracking_state='SENSOR_FAULT' ORDER BY id DESC LIMIT ?",
            (limit,)
        )[::-1]

    def clear_history(self) -> Dict[str, int]:
        """Delete light_samples, orientation_history, and non-PENDING commands.

        Returns the deleted row counts per table.
        """
        counts = {}
        for table in ["light_samples", "orientation_history"]:
            cur = self._exec(f"DELETE FROM {table}")
            counts[table] = cur.rowcount
        cur = self._exec("DELETE FROM commands WHERE status != 'PENDING'")
        counts["commands"] = cur.rowcount
        return counts

    def cancel_pending_commands(self, device_id: str) -> int:
        """Cancel all PENDING commands for a device. Returns count."""
        cur = self._exec(
            "UPDATE commands SET status='EXPIRED', reason='manually cancelled', "
            "updated_at=? WHERE device_id=? AND status='PENDING'",
            (utcnow(), device_id)
        )
        return cur.rowcount

    def efficiency_stats(self, device_id: str) -> Dict[str, Any]:
        """Compute efficiency metrics from stored history.

        Per-row deltas are computed with window functions in a subquery and
        aggregated in the outer query, so the table is never materialised in
        Python.  This runs on every /api/summary call.
        """
        agg = self._one(
            """
            SELECT
              COALESCE(SUM(ABS(angle - prev_angle)), 0) AS total_degrees,
              COALESCE(SUM(CASE WHEN mode='AUTO'
                THEN MAX(0.0, (julianday(next_ts) - julianday(ts)) * 86400.0)
                ELSE 0 END), 0) AS auto_seconds,
              COALESCE(SUM(CASE WHEN mode='MANUAL'
                THEN MAX(0.0, (julianday(next_ts) - julianday(ts)) * 86400.0)
                ELSE 0 END), 0) AS manual_seconds,
              COALESCE(SUM(CASE WHEN mode='AUTO' THEN 1 ELSE 0 END), 0) AS auto_events,
              COALESCE(SUM(CASE WHEN mode='MANUAL' THEN 1 ELSE 0 END), 0) AS manual_events,
              COALESCE(SUM(CASE WHEN tracking_state='SENSOR_FAULT' THEN 1 ELSE 0 END), 0)
                AS fault_events,
              MAX(id) AS last_id
            FROM (
              SELECT
                id, angle, mode, tracking_state, ts,
                LAG(angle) OVER (ORDER BY id) AS prev_angle,
                LEAD(ts)   OVER (ORDER BY id) AS next_ts
              FROM orientation_history
              WHERE device_id=?
            )
            """,
            (device_id,),
        ) or {}

        total_degrees = float(agg.get("total_degrees") or 0.0)
        auto_seconds = float(agg.get("auto_seconds") or 0.0)
        manual_seconds = float(agg.get("manual_seconds") or 0.0)

        # The newest row has no LEAD() partner, so its interval is unknown
        # until the session ends.  If the device is still online we extend
        # that final interval up to "now".
        last_id = agg.get("last_id")
        state = self.get_state(device_id)
        if last_id is not None and state and state.get("is_online"):
            last = self._one(
                "SELECT mode, ts FROM orientation_history WHERE id=?", (last_id,)
            )
            if last:
                try:
                    started = datetime.fromisoformat(last["ts"])
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    seconds = max(
                        0.0, (datetime.now(timezone.utc) - started).total_seconds()
                    )
                    if last["mode"] == "AUTO":
                        auto_seconds += seconds
                    elif last["mode"] == "MANUAL":
                        manual_seconds += seconds
                except (TypeError, ValueError):
                    pass

        total_seconds = auto_seconds + manual_seconds
        auto_pct = round(auto_seconds / total_seconds * 100, 1) if total_seconds else 0.0

        return {
            "total_degrees_moved": round(total_degrees, 1),
            "auto_events": int(agg.get("auto_events") or 0),
            "manual_events": int(agg.get("manual_events") or 0),
            "auto_pct": auto_pct,
            "time_in_auto_seconds": round(auto_seconds, 1),
            "time_in_manual_seconds": round(manual_seconds, 1),
            "fault_events": int(agg.get("fault_events") or 0),
        }

    def clear_all(self) -> None:
        for table in list(MAX_ROWS) + ["device_state"]:
            self._exec(f"DELETE FROM {table}")
