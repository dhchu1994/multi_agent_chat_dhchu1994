"""
Dynamic conversation logger.

Creates a plain-text transcript (and optionally a JSON sidecar) named with
the session start datetime, including the study-flow events (screen visits,
check attempts, tutorial steps, condition assignment) recorded by main.py.
Every message is flushed to disk immediately so that no data is lost if the
process crashes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class LogEntry:
    timestamp: str
    sender: str
    content: str
    turn_index: int


@dataclass
class ConversationLog:
    session_id: str
    scenario: str
    started_at: str
    entries: list[LogEntry] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


class ConversationLogger:
    """Persists every message and workflow event to a text file (and optional JSON) on disk."""

    def __init__(
        self,
        output_dir: str | Path,
        scenario: str,
        save_json: bool = True,
        agent_names: list[str] | None = None,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._save_json = save_json
        self._agent_names = agent_names or []

        now = datetime.now(tz=timezone.utc)
        self._session_id = now.strftime("%Y-%m-%d_%H-%M-%S")
        self._txt_path = self._output_dir / f"{self._session_id}.txt"
        self._json_path = self._output_dir / f"{self._session_id}.json"

        self._log = ConversationLog(
            session_id=self._session_id,
            scenario=scenario,
            started_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        )

        # Write the file header once.
        self._write_txt_header()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    def record(self, sender: str, content: str, turn_index: int) -> None:
        """Record a single message and flush to disk immediately."""
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        entry = LogEntry(
            timestamp=timestamp,
            sender=sender,
            content=content,
            turn_index=turn_index,
        )
        self._log.entries.append(entry)

        # Append to the text file.
        self._append_txt(entry)

        # Overwrite the JSON file with the full log (atomic snapshot).
        if self._save_json:
            self._write_json()

    def record_event(self, event_name: str, metadata: dict | None = None) -> str:
        """Record a workflow/UI event with timestamp and optional metadata."""
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        event_record: dict = {
            "timestamp": timestamp,
            "event": event_name,
        }
        if metadata:
            event_record.update(metadata)
        self._log.events.append(event_record)

        meta_str = f" | {json.dumps(metadata, ensure_ascii=False)}" if metadata else ""
        line = f"[{timestamp}] [EVENT] {event_name}{meta_str}\n\n"
        with self._txt_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

        if self._save_json:
            self._write_json()

        return f"{event_name}  —  {timestamp.split(' ')[-1]}"

    @property
    def txt_path(self) -> Path:
        return self._txt_path

    @property
    def json_path(self) -> Path:
        return self._json_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_txt_header(self) -> None:
        agents_str = ", ".join(self._agent_names) if self._agent_names else "N/A"
        header = (
            f"Started:  {self._log.started_at}\n"
            f"Agents:   {agents_str}\n"
            f"Scenario: {self._log.scenario}\n"
            f"{'=' * 72}\n\n"
        )
        self._txt_path.write_text(header, encoding="utf-8")

    def _append_txt(self, entry: LogEntry) -> None:
        line = (
            f"[{entry.timestamp}] "
            f"{entry.sender}:\n"
            f"  {entry.content}\n\n"
        )
        with self._txt_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _write_json(self) -> None:
        self._json_path.write_text(
            json.dumps(asdict(self._log), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )