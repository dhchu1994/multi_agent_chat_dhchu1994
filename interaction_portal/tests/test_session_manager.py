"""Tests for SessionManager chat, card actions, check-ins, and session timeouts."""

import pytest
from app.config_loader import load_config
from app.event_store import EventStore
from app.simulation.session_manager import SessionManager, PortalError


@pytest.fixture
def sm_env(tmp_path):
    cfg = load_config()
    store = EventStore(tmp_path / "sm_mgr_test.sqlite3")
    mgr = SessionManager(cfg, store)
    return cfg, store, mgr


def test_resume_and_timeout_gap(sm_env):
    cfg, store, mgr = sm_env
    pid = "pid_resume"
    state = mgr.start_session(pid=pid, condition="PAS-COORD")

    # Immediate resume succeeds
    resumed, reason = mgr.resume_or_expire(pid)
    assert resumed is not None
    assert reason == ""

    # Simulated gap beyond cutoff (15 mins)
    row = store.get_session(pid)
    # Manually backdate last_activity in session row
    with store._lock, store._conn() as c:
        c.execute("UPDATE sessions SET last_activity='2020-01-01T00:00:00.000Z' WHERE pid=?", (pid,))

    resumed_late, reason_late = mgr.resume_or_expire(pid)
    assert resumed_late is None
    assert reason_late == "timeout"


def test_chat_open_and_stream(sm_env):
    cfg, store, mgr = sm_env
    pid = "pid_chat"
    state = mgr.start_session(pid=pid, condition="PAS-COORD")
    # Advance to brief then task
    state.page = "brief"
    mgr.save(state)
    mgr.sm.advance(state, mgr.logger(state))  # enters task

    # Chat open returns generator of events
    open_events = list(mgr.chat_open(pid))
    assert len(open_events) > 0
    types = {e["type"] for e in open_events}
    assert "message" in types
    assert "card" in types

    # Chat send broadcast
    send_events = list(mgr.chat_send(pid, "What should we do?"))
    assert len(send_events) > 0


def test_checkin_validation(sm_env):
    cfg, store, mgr = sm_env
    pid = "pid_checkin"
    state = mgr.start_session(pid=pid, condition="EVA-TASK")
    state.page = "checkin"
    state.checkin_started = mgr.clock()
    mgr.save(state)

    valid_answers = {
        "ci1": 5,
        "ci2": "Nia",
        "ci3": "Very",
        "ci4": "75%",
        "ci5": "Status and what was wrong",
    }
    # Valid submission advances state
    res_state = mgr.checkin_submit(pid, valid_answers, durations={})
    assert res_state.page in ("brief", "break")
