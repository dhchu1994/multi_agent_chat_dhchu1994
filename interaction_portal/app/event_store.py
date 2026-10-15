"""
Append-only telemetry store for the Interaction Portal (replaces conversation_logger.py).

Every row of ``events`` carries ``pid``, a UTC timestamp with millisecond
precision, the client, the page, the event type and event-specific metadata.
Rows are only ever inserted: the table has triggers that reject UPDATE and
DELETE. Session records live in a separate ``sessions`` table (one row per pid,
mutable state such as the current page); chat transcripts, panel texts and
submitted cards are stored in their own append-only tables.

SQLite (WAL mode) is used with one short-lived connection per call and a
process-wide write lock, which is plenty for the planned 30 concurrent sessions.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    received_at TEXT,
    client_id TEXT,
    page_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    autopilot INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_pid ON events(pid, id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TABLE IF NOT EXISTS sessions (
    pid TEXT PRIMARY KEY,
    condition_code TEXT NOT NULL,
    config_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_state TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0,
    specialist_order TEXT NOT NULL DEFAULT '[]',
    client_order TEXT NOT NULL DEFAULT '[]',
    completion_code TEXT,
    autopilot INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 0,
    resume_token TEXT,
    verify_count INTEGER NOT NULL DEFAULT 0,
    config_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid TEXT NOT NULL, client_id TEXT, timestamp TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_pid ON transcripts(pid, client_id, id);

CREATE TABLE IF NOT EXISTS panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid TEXT NOT NULL, client_id TEXT, timestamp TEXT NOT NULL,
    level TEXT NOT NULL, text TEXT NOT NULL, word_count INTEGER NOT NULL, sections_present TEXT NOT NULL,
    validation_errors TEXT NOT NULL DEFAULT '[]',
    retried INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid TEXT NOT NULL, client_id TEXT NOT NULL, timestamp TEXT NOT NULL,
    autopilot INTEGER NOT NULL DEFAULT 0, card TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    counter INTEGER NOT NULL,
    assigned_at TEXT NOT NULL,
    pid TEXT NOT NULL,
    condition_code TEXT NOT NULL,
    client_order TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    """ISO 8601 UTC timestamp with millisecond precision, e.g. 2026-10-07T14:20:00.123Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class EventStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._mem: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._mem = sqlite3.connect(":memory:", check_same_thread=False)
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Schema migrations for existing databases
            cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)").fetchall()}
            if "version" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
            if "resume_token" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN resume_token TEXT")
            if "verify_count" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN verify_count INTEGER NOT NULL DEFAULT 0")
            if "config_sha256" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN config_sha256 TEXT")
            e_cols = {r["name"] for r in c.execute("PRAGMA table_info(events)").fetchall()}
            if "received_at" not in e_cols:
                c.execute("ALTER TABLE events ADD COLUMN received_at TEXT")
            p_cols = {r["name"] for r in c.execute("PRAGMA table_info(panels)").fetchall()}
            if "validation_errors" not in p_cols:
                c.execute("ALTER TABLE panels ADD COLUMN validation_errors TEXT NOT NULL DEFAULT '[]'")
            if "retried" not in p_cols:
                c.execute("ALTER TABLE panels ADD COLUMN retried INTEGER NOT NULL DEFAULT 0")
            if "truncated" not in p_cols:
                c.execute("ALTER TABLE panels ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0")

    # ---- connection handling -------------------------------------------
    def _conn(self):
        class _Ctx:
            def __init__(s, outer):
                s.o = outer
            def __enter__(s):
                if s.o._mem is not None:
                    s.c = s.o._mem
                else:
                    s.c = sqlite3.connect(s.o.db_path, timeout=30)
                    s.c.execute("PRAGMA journal_mode=WAL")
                s.c.row_factory = sqlite3.Row
                return s.c
            def __exit__(s, et, ev, tb):
                if et is None:
                    s.c.commit()
                else:
                    s.c.rollback()
                if s.o._mem is None:
                    s.c.close()
        return _Ctx(self)

    # ---- events ---------------------------------------------------------
    def log(self, pid: str, event_type: str, page_id: str, client_id: str | None = None,
            metadata: dict[str, Any] | None = None, autopilot: bool = False,
            timestamp: str | None = None, received_at: str | None = None) -> None:
        rec_at = received_at or utc_now_iso()
        ts = timestamp or rec_at
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO events(pid,timestamp,received_at,client_id,page_id,event_type,autopilot,metadata) VALUES (?,?,?,?,?,?,?,?)",
                (pid, ts, rec_at, client_id, page_id, event_type, int(autopilot),
                 json.dumps(metadata or {}, ensure_ascii=False)),
            )

    def log_batch(self, events: list[dict]) -> None:
        if not events:
            return
        now = utc_now_iso()
        rows = []
        for e in events:
            rec_at = e.get("received_at") or now
            ts = e.get("timestamp") or rec_at
            rows.append((
                e["pid"], ts, rec_at, e.get("client_id"), e["page_id"], e["event_type"],
                int(bool(e.get("autopilot", False))),
                json.dumps(e.get("metadata") or {}, ensure_ascii=False)
            ))
        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT INTO events(pid,timestamp,received_at,client_id,page_id,event_type,autopilot,metadata) VALUES (?,?,?,?,?,?,?,?)",
                rows
            )

    def events(self, pid: str | None = None, event_type: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM events WHERE 1=1", []
        if pid:
            q += " AND pid=?"; args.append(pid)
        if event_type:
            q += " AND event_type=?"; args.append(event_type)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        return [self._event_dict(r) for r in rows]

    @staticmethod
    def _event_dict(r: sqlite3.Row) -> dict:
        d = {"pid": r["pid"], "timestamp": r["timestamp"], "client_id": r["client_id"],
             "page_id": r["page_id"], "event_type": r["event_type"], "autopilot": bool(r["autopilot"]),
             "metadata": json.loads(r["metadata"])}
        if "received_at" in r.keys() and r["received_at"]:
            d["received_at"] = r["received_at"]
        return d

    # ---- sessions -------------------------------------------------------
    def create_session(self, row: dict) -> bool:
        """Atomically create a session. Returns False if the pid already exists (single use)."""
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        with self._lock, self._conn() as c:
            try:
                c.execute(f"INSERT INTO sessions({cols}) VALUES ({qs})", list(row.values()))
            except sqlite3.IntegrityError:
                return False
        return True

    def get_session(self, pid: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM sessions WHERE pid=?", (pid,)).fetchone()
        return dict(r) if r else None

    def get_session_by_completion_code(self, code: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM sessions WHERE completion_code=?", (code,)).fetchone()
        return dict(r) if r else None

    def get_session_by_resume_token(self, token: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM sessions WHERE resume_token=?", (token,)).fetchone()
        return dict(r) if r else None

    def update_session(self, pid: str, expected_version: int | None = None, **fields: Any) -> bool:
        if not fields:
            return True
        sets = ",".join(f"{k}=?" for k in fields)
        args = list(fields.values())
        where = "pid=?"
        args.append(pid)
        if expected_version is not None:
            where += " AND (version=? OR version=0)"
            args.append(expected_version)
        with self._lock, self._conn() as c:
            cur = c.execute(f"UPDATE sessions SET {sets} WHERE {where}", args)
            return cur.rowcount > 0

    def sessions(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()
        return [dict(r) for r in rows]

    # ---- transcript / panels / cards -----------------------------------
    def add_transcript(self, pid: str, client_id: str | None, message: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO transcripts(pid,client_id,timestamp,message) VALUES (?,?,?,?)",
                      (pid, client_id, message.get("timestamp") or utc_now_iso(), json.dumps(message, ensure_ascii=False)))

    def transcript(self, pid: str, client_id: str | None = None) -> list[dict]:
        q, args = "SELECT message FROM transcripts WHERE pid=?", [pid]
        if client_id is not None:
            q += " AND client_id=?"; args.append(client_id)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        return [json.loads(r["message"]) for r in rows]

    def add_panel(self, pid: str, client_id: str | None, level: str, text: str, word_count: int,
                  sections_present: list[str], validation_errors: list[str] | None = None,
                  retried: bool = False, truncated: bool = False) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO panels(pid,client_id,timestamp,level,text,word_count,sections_present,validation_errors,retried,truncated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, client_id, utc_now_iso(), level, text, word_count, json.dumps(sections_present),
                 json.dumps(validation_errors or []), int(retried), int(truncated)),
            )

    def panels(self, pid: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM panels WHERE pid=? ORDER BY id", (pid,)).fetchall()
        return [{**dict(r),
                 "sections_present": json.loads(r["sections_present"]),
                 "validation_errors": json.loads(r["validation_errors"]) if "validation_errors" in r.keys() and r["validation_errors"] else []}
                for r in rows]

    def add_card(self, pid: str, client_id: str, card: dict, autopilot: bool = False) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO cards(pid,client_id,timestamp,autopilot,card) VALUES (?,?,?,?,?)",
                      (pid, client_id, utc_now_iso(), int(autopilot), json.dumps(card, ensure_ascii=False)))

    def cards(self, pid: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM cards", []
        if pid:
            q += " WHERE pid=?"; args.append(pid)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        return [{"pid": r["pid"], "client_id": r["client_id"], "timestamp": r["timestamp"],
                 "autopilot": bool(r["autopilot"]), "card": json.loads(r["card"])} for r in rows]

    # ---- exports --------------------------------------------------------
    def events_csv(self) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["pid", "timestamp", "received_at", "client_id", "page_id", "event_type", "autopilot", "metadata"])
        for e in self.events():
            w.writerow([e["pid"], e["timestamp"], e.get("received_at", ""), e["client_id"] or "", e["page_id"], e["event_type"],
                        "true" if e["autopilot"] else "false", json.dumps(e["metadata"], ensure_ascii=False)])
        return out.getvalue()

    def panels_csv(self) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["pid", "client_id", "timestamp", "level", "word_count", "sections_present", "retried", "truncated", "validation_errors", "text"])
        with self._conn() as c:
            rows = c.execute("SELECT * FROM panels ORDER BY id").fetchall()
            for r in rows:
                w.writerow([r["pid"], r["client_id"] or "", r["timestamp"], r["level"], r["word_count"],
                            r["sections_present"], r["retried"] if "retried" in r.keys() else 0,
                            r["truncated"] if "truncated" in r.keys() else 0,
                            r["validation_errors"] if "validation_errors" in r.keys() else "[]",
                            r["text"]])
        return out.getvalue()

    def transcripts_csv(self) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["pid", "client_id", "timestamp", "msg_id", "sender", "to", "kind", "block_id", "text"])
        with self._conn() as c:
            rows = c.execute("SELECT * FROM transcripts ORDER BY id").fetchall()
            for r in rows:
                try:
                    m = json.loads(r["message"])
                except Exception:
                    m = {}
                w.writerow([r["pid"], r["client_id"] or "", r["timestamp"], m.get("id", ""),
                            m.get("sender", ""), ", ".join(m.get("to") or []), m.get("kind", ""),
                            m.get("block_id") or "", m.get("text", "")])
        return out.getvalue()

    def card_spans_csv(self) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["pid", "client_id", "timestamp", "autopilot", "field", "span_id", "author", "status", "parent", "text"])
        for c_entry in self.cards():
            pid = c_entry["pid"]
            cid = c_entry["client_id"]
            ts = c_entry["timestamp"]
            ap = "true" if c_entry["autopilot"] else "false"
            fields = c_entry.get("card", {}).get("fields", {})
            for fid, spans in fields.items():
                for s in spans:
                    w.writerow([pid, cid, ts, ap, fid, s.get("span_id", ""), s.get("author", ""),
                                s.get("status", ""), s.get("parent") or "", s.get("text", "")])
        return out.getvalue()

    def checkins_csv(self) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["pid", "timestamp", "client_id", "item_id", "value", "duration_ms"])
        for e in self.events(event_type="checkin_answer"):
            m = e.get("metadata") or {}
            w.writerow([e["pid"], e["timestamp"], e["client_id"] or "", m.get("item_id", ""),
                        m.get("value", ""), m.get("duration_ms", "")])
        return out.getvalue()

    def sessions_csv(self) -> str:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["pid", "condition_code", "config_version", "model_version", "started_at", "ended_at",
                    "end_state", "resume_count", "completion_code", "autopilot", "version"])
        for s in self.sessions():
            w.writerow([s["pid"], s["condition_code"], s["config_version"], s["model_version"],
                        s["started_at"], s.get("ended_at") or "", s.get("end_state") or "",
                        s["resume_count"], s.get("completion_code") or "",
                        "true" if s["autopilot"] else "false", s.get("version", 0)])
        return out.getvalue()

    def export_cards_json(self) -> str:
        return json.dumps(self.cards(), ensure_ascii=False, indent=2)

    def export_all_json(self) -> str:
        pids = [s["pid"] for s in self.sessions()]
        return json.dumps({
            "sessions": self.sessions(),
            "events": self.events(),
            "cards": self.cards(),
            "transcripts": {p: self.transcript(p) for p in pids},
            "panels": {p: self.panels(p) for p in pids},
        }, ensure_ascii=False, indent=2)

    def backup(self, dest: str | Path) -> None:
        """Consistent copy of the database (run nightly from cron: see README)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            src = self._mem or sqlite3.connect(self.db_path)
            dst = sqlite3.connect(dest)
            src.backup(dst)
            dst.close()
            if self._mem is None:
                src.close()


class SessionLogger:
    """Binds an EventStore to one pid so callers only supply page, client and event type."""

    def __init__(self, store: EventStore, pid: str, autopilot: bool = False) -> None:
        self.store, self.pid, self.autopilot = store, pid, autopilot

    def log(self, event_type: str, page_id: str, client_id: str | None = None,
            timestamp: str | None = None, **metadata: Any) -> None:
        self.store.log(self.pid, event_type, page_id, client_id, metadata, autopilot=self.autopilot, timestamp=timestamp)
