"""Database verification and integrity check tool.

Usage:
    python -m app.tools.verify_db [--db data/portal.db]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


REQUIRED_TABLES = ["sessions", "events", "cards", "panels", "transcripts", "assignments"]
REQUIRED_TRIGGERS = [
    "events_no_update",
    "events_no_delete",
]


def verify_database(db_path: Path) -> bool:
    if not db_path.exists():
        print(f"ERROR: Database file not found: {db_path}", file=sys.stderr)
        return False

    print(f"Verifying database: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    all_ok = True

    try:
        cur = conn.cursor()

        # 1. PRAGMA integrity_check
        print("1. Running PRAGMA integrity_check...")
        res = cur.execute("PRAGMA integrity_check;").fetchall()
        if res and res[0][0] == "ok":
            print("   [OK] Database integrity check passed.")
        else:
            print(f"   [FAIL] Integrity check failed: {res}", file=sys.stderr)
            all_ok = False

        # 2. PRAGMA foreign_key_check
        print("2. Running PRAGMA foreign_key_check...")
        fk_violations = cur.execute("PRAGMA foreign_key_check;").fetchall()
        if not fk_violations:
            print("   [OK] Foreign key check passed.")
        else:
            print(f"   [FAIL] Foreign key violations found: {fk_violations}", file=sys.stderr)
            all_ok = False

        # 3. Check required tables
        print("3. Checking required tables in sqlite_master...")
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
        missing_tables = [t for t in REQUIRED_TABLES if t not in tables]
        if not missing_tables:
            print(f"   [OK] All {len(REQUIRED_TABLES)} required tables exist: {', '.join(REQUIRED_TABLES)}")
        else:
            print(f"   [FAIL] Missing required tables: {missing_tables}", file=sys.stderr)
            all_ok = False

        # 4. Check append-only triggers
        print("4. Checking append-only triggers in sqlite_master...")
        triggers = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='trigger';").fetchall()}
        missing_triggers = [t for t in REQUIRED_TRIGGERS if t not in triggers]
        if not missing_triggers:
            print(f"   [OK] All {len(REQUIRED_TRIGGERS)} append-only triggers exist: {', '.join(REQUIRED_TRIGGERS)}")
        else:
            print(f"   [FAIL] Missing required triggers: {missing_triggers}", file=sys.stderr)
            all_ok = False

        # 5. Row counts & statistics
        print("5. Table row counts:")
        for t in REQUIRED_TABLES:
            if t in tables:
                cnt = cur.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
                print(f"   - {t}: {cnt} row(s)")

        # 6. Monotonic events timeline check per pid
        print("6. Checking events ordering per session (monotonic timestamps)...")
        sessions = [r[0] for r in cur.execute("SELECT DISTINCT pid FROM events;").fetchall()]
        timeline_issues = 0
        for pid in sessions:
            evts = cur.execute(
                "SELECT id, timestamp, received_at FROM events WHERE pid=? ORDER BY id ASC;", (pid,)
            ).fetchall()
            last_ts = None
            for e in evts:
                ts = e["timestamp"]
                if last_ts and ts < last_ts:
                    timeline_issues += 1
                last_ts = ts
        if timeline_issues == 0:
            print(f"   [OK] Monotonic timeline verified across {len(sessions)} session(s).")
        else:
            print(f"   [WARN] Found {timeline_issues} event timestamp inversion(s) across sessions.")

    finally:
        conn.close()

    if all_ok:
        print("\nDatabase verification completed successfully! All checks PASSED.")
    else:
        print("\nDatabase verification FAILED with errors.", file=sys.stderr)

    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify SQLite portal database integrity.")
    parser.add_argument("--db", type=str, default="data/portal.db", help="Path to database file")
    args = parser.parse_args()

    ok = verify_database(Path(args.db))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
