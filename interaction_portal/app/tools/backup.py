"""Automated SQLite database backup tool.

Usage:
    python -m app.tools.backup --dest backups/ --keep 14 [--db data/portal.db]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def verify_integrity(db_path: Path) -> bool:
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        res = cur.execute("PRAGMA integrity_check;").fetchall()
        return bool(res and res[0][0] == "ok")
    finally:
        con.close()


def perform_backup(source_db: Path, dest_dir: Path, keep: int = 14) -> Path:
    if not source_db.exists():
        raise FileNotFoundError(f"Source database not found: {source_db}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    temp_db = dest_dir / f"backup_temp_{ts}.db"
    final_gz = dest_dir / f"backup_{ts}.db.gz"
    sha_file = dest_dir / f"backup_{ts}.db.gz.sha256"

    print(f"[{datetime.now()}] Starting online backup from {source_db} to {temp_db}...")
    src_conn = sqlite3.connect(str(source_db))
    dst_conn = sqlite3.connect(str(temp_db))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    print(f"[{datetime.now()}] Verifying backup integrity with PRAGMA integrity_check...")
    if not verify_integrity(temp_db):
        if temp_db.exists():
            temp_db.unlink()
        raise RuntimeError("Integrity check failed on backup database!")

    print(f"[{datetime.now()}] Compressing backup to {final_gz}...")
    with temp_db.open("rb") as f_in, gzip.open(final_gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    temp_db.unlink()

    print(f"[{datetime.now()}] Computing SHA-256 checksum...")
    checksum = compute_sha256(final_gz)
    sha_file.write_text(f"{checksum}  {final_gz.name}\n", encoding="utf-8")
    print(f"[{datetime.now()}] Backup created successfully: {final_gz} ({checksum})")

    if keep > 0:
        cleanup_old_backups(dest_dir, keep)

    return final_gz


def cleanup_old_backups(dest_dir: Path, keep: int) -> None:
    backups = sorted(dest_dir.glob("backup_*.db.gz"), key=lambda p: p.stat().st_mtime)
    if len(backups) > keep:
        to_delete = backups[:-keep]
        print(f"[{datetime.now()}] Rotating backups: removing {len(to_delete)} old backup(s)...")
        for old_b in to_delete:
            try:
                old_b.unlink()
                sha_peer = old_b.with_name(old_b.name + ".sha256")
                if sha_peer.exists():
                    sha_peer.unlink()
                print(f"  Deleted: {old_b.name}")
            except Exception as e:
                print(f"  Warning: could not delete {old_b.name}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup SQLite portal database.")
    parser.add_argument("--db", type=str, default="data/portal.db", help="Path to database file")
    parser.add_argument("--dest", type=str, default="backups/", help="Directory to store backups")
    parser.add_argument("--keep", type=int, default=14, help="Number of backups to keep (0 = keep all)")
    args = parser.parse_args()

    source = Path(args.db)
    dest = Path(args.dest)

    try:
        perform_backup(source, dest, keep=args.keep)
        return 0
    except Exception as exc:
        print(f"ERROR: Backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
