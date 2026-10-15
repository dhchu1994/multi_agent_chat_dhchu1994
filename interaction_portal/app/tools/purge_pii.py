"""PII purge and redaction tool for interaction portal database.

Usage:
    python -m app.tools.purge_pii [--db data/portal.db] [--pid PID] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..agents.pii import redact_pii, scan_pii


DROP_TRIGGERS_SQL = """
DROP TRIGGER IF EXISTS events_no_update;
DROP TRIGGER IF EXISTS events_no_delete;
"""

RECREATE_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
"""


def purge_pii(db_path: Path, target_pid: str | None = None, dry_run: bool = False) -> int:
    if not db_path.exists():
        print(f"ERROR: Database file not found: {db_path}", file=sys.stderr)
        return 1

    print(f"Scanning {db_path} for PII (dry_run={dry_run}, pid={target_pid or 'ALL'})...")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()

        # 1. Scan transcripts
        q_trans = "SELECT id, pid, client_id, message FROM transcripts WHERE 1=1"
        args_trans = []
        if target_pid:
            q_trans += " AND pid=?"
            args_trans.append(target_pid)
        transcripts = cur.execute(q_trans, args_trans).fetchall()

        trans_updates = []
        for r in transcripts:
            try:
                msg_obj = json.loads(r["message"])
                txt = msg_obj.get("text", "")
                findings = scan_pii(txt)
                if findings:
                    cleaned_text = redact_pii(txt)
                    msg_obj["text"] = cleaned_text
                    trans_updates.append((r["id"], r["pid"], json.dumps(msg_obj, ensure_ascii=False), findings))
            except Exception:
                pass

        # 2. Scan events metadata
        q_events = "SELECT id, pid, event_type, metadata FROM events WHERE 1=1"
        args_events = []
        if target_pid:
            q_events += " AND pid=?"
            args_events.append(target_pid)
        events = cur.execute(q_events, args_events).fetchall()

        event_updates = []
        for r in events:
            try:
                meta = json.loads(r["metadata"] or "{}")
                changed = False
                event_findings = []
                for k, v in meta.items():
                    if isinstance(v, str):
                        f = scan_pii(v)
                        if f:
                            cleaned_v = redact_pii(v)
                            meta[k] = cleaned_v
                            event_findings.extend(f)
                            changed = True
                if changed:
                    event_updates.append((r["id"], r["pid"], json.dumps(meta, ensure_ascii=False), event_findings))
            except Exception:
                pass

        print(f"\nScan results:")
        print(f"  Transcripts with PII: {len(trans_updates)}")
        print(f"  Events with PII:      {len(event_updates)}")

        if dry_run:
            print("\n[DRY RUN] No database modifications made.")
            for tid, pid, _, findings in trans_updates[:10]:
                print(f"  Transcript #{tid} (pid={pid}): {[f['type'] for f in findings]}")
            for eid, pid, _, findings in event_updates[:10]:
                print(f"  Event #{eid} (pid={pid}): {[f['type'] for f in findings]}")
            return 0

        if not trans_updates and not event_updates:
            print("No PII records found to purge.")
            return 0

        # Perform updates inside transaction
        print("\nApplying redactions inside transaction...")
        conn.execute("BEGIN TRANSACTION;")
        try:
            # Drop append-only triggers
            conn.executescript(DROP_TRIGGERS_SQL)

            # Update transcripts
            for tid, pid, new_msg, _ in trans_updates:
                conn.execute("UPDATE transcripts SET message=? WHERE id=?;", (new_msg, tid))

            # Update events
            for eid, pid, new_meta, _ in event_updates:
                conn.execute("UPDATE events SET metadata=? WHERE id=?;", (new_meta, eid))

            # Log audit maintenance event
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            audit_meta = json.dumps({
                "transcripts_redacted": len(trans_updates),
                "events_redacted": len(event_updates),
                "target_pid": target_pid,
            })
            conn.execute(
                "INSERT INTO events(pid, timestamp, received_at, page_id, event_type, autopilot, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?);",
                (target_pid or "SYSTEM", now_iso, now_iso, "admin", "pii_purge_maintenance", 0, audit_meta)
            )

            # Recreate append-only triggers
            conn.executescript(RECREATE_TRIGGERS_SQL)

            conn.commit()
            print("[SUCCESS] PII redacted and append-only triggers re-established.")
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Transaction rolled back due to error: {e}", file=sys.stderr)
            return 1

    finally:
        conn.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge or redact PII from database.")
    parser.add_argument("--db", type=str, default="data/portal.db", help="Path to database file")
    parser.add_argument("--pid", type=str, default=None, help="Specific participant ID to purge")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, do not modify")
    args = parser.parse_args()

    return purge_pii(Path(args.db), target_pid=args.pid, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
