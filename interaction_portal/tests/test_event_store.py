"""Tests for EventStore append-only guarantees, schemas, atomic sessions and exports."""

import json
import sqlite3
import pytest
from app.event_store import EventStore, SessionLogger, utc_now_iso


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "test.sqlite3")


def test_append_only_trigger_blocks_update_and_delete(store):
    store.log("pid1", "session_start", "entry", metadata={"foo": "bar"})
    events = store.events("pid1")
    assert len(events) == 1
    event_id = 1

    with pytest.raises(sqlite3.DatabaseError, match="events is append-only"):
        with store._lock, store._conn() as c:
            c.execute("UPDATE events SET page_id='hacked' WHERE id=?", (event_id,))

    with pytest.raises(sqlite3.DatabaseError, match="events is append-only"):
        with store._lock, store._conn() as c:
            c.execute("DELETE FROM events WHERE id=?", (event_id,))


def test_single_use_pid_enforced_by_primary_key(store):
    session_row = {
        "pid": "pid-unique",
        "condition_code": "EVA-TASK",
        "config_version": "2.0.0",
        "model_version": "mock-1",
        "started_at": utc_now_iso(),
        "last_activity": utc_now_iso(),
        "autopilot": 0,
        "state": "{}",
    }
    assert store.create_session(session_row) is True
    # Attempting to create the same pid again must return False (single-use)
    assert store.create_session(session_row) is False


def test_logging_and_session_logger(store):
    logger = SessionLogger(store, "pid_logger", autopilot=True)
    logger.log("page_enter", "orientation")
    logger.log("page_leave", "orientation", dwell_time_ms=5000)

    events = store.events("pid_logger")
    assert len(events) == 2
    assert events[0]["event_type"] == "page_enter"
    assert events[0]["autopilot"] is True
    assert events[1]["metadata"]["dwell_time_ms"] == 5000


def test_transcripts_and_panels(store):
    store.add_transcript("pid_chat", "C1", {"sender": "Nia", "text": "Hello"})
    t = store.transcript("pid_chat", "C1")
    assert len(t) == 1
    assert t[0]["sender"] == "Nia"

    store.add_panel("pid_chat", "C1", "task_focused", "Panel text", 2, ["Team status", "On the card"])
    p = store.panels("pid_chat")
    assert len(p) == 1
    assert p[0]["level"] == "task_focused"
    assert p[0]["sections_present"] == ["Team status", "On the card"]


def test_cards_and_exports(store):
    card_data = {"fields": {"audience_angle": []}, "typed_share": 0.5}
    store.add_card("pid_card", "C1", card_data, autopilot=True)
    cards = store.cards("pid_card")
    assert len(cards) == 1
    assert cards[0]["autopilot"] is True
    assert cards[0]["card"]["typed_share"] == 0.5

    # Test CSV export
    csv_str = store.events_csv()
    assert "pid,timestamp,client_id,page_id,event_type,autopilot,metadata" in csv_str

    # Test JSON exports
    cards_json = store.export_cards_json()
    assert "pid_card" in cards_json

    all_json = store.export_all_json()
    parsed = json.loads(all_json)
    assert "sessions" in parsed
    assert "events" in parsed
    assert "cards" in parsed
