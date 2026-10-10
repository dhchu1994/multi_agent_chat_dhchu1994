"""Tests for PracticeService scripted progression and role checking."""

import pytest
from app.config_loader import load_config
from app.event_store import EventStore
from app.simulation.practice import PracticeService
from app.simulation.session_manager import SessionManager, PortalError


@pytest.fixture
def practice_env(tmp_path):
    cfg = load_config()
    store = EventStore(tmp_path / "practice_test.sqlite3")
    mgr = SessionManager(cfg, store)
    practice = PracticeService(mgr)
    return cfg, store, mgr, practice


def test_ai_practice_steps_and_wrong_role_check(practice_env):
    cfg, store, mgr, practice = practice_env
    pid = "pid_prac_ai"
    state = mgr.start_session(pid=pid, condition="EVA-TASK", autopilot=True)
    # Advance to practice
    mgr.sm.advance(state, mgr.logger(state))  # orientation -> team
    mgr.sm.advance(state, mgr.logger(state))  # team -> role
    mgr.sm.advance(state, mgr.logger(state))  # role -> practice

    # Step 1: chat broadcast & mention
    res = practice.chat(pid, "hello team")
    assert res["ok"] is True
    practice.next_step(pid)

    res2 = practice.chat(pid, "@Theo hi")
    assert res2["ok"] is True
    practice.next_step(pid)

    # Step 2: panel read & ask orchestrator
    practice.panel_read(pid)
    practice.next_step(pid)
    practice.chat(pid, "@Orchestrator how do you assign?")
    practice.next_step(pid)

    # Step 3: evaluative role actions
    practice.card_action(pid, "keep", span_id="spn_01")
    practice.card_action(pid, "cut", span_id="spn_02")
    practice.card_action(pid, "send_back", span_id="spn_03", reason="Too brief")
    practice.next_step(pid)

    # Step 4: submit card
    practice.submit_card(pid)

    # Wrong role check sends back to role page!
    wrong_res = practice.role_check(pid, "passive")
    assert wrong_res["ok"] is False
    assert wrong_res.get("sent_back") is True
    st_reloaded = mgr.check_active(pid)
    assert st_reloaded.page == "role"

    # Re-advance to practice and submit correct check
    mgr.sm.advance(st_reloaded, mgr.logger(st_reloaded))
    practice.submit_card(pid)
    right_res = practice.role_check(pid, "evaluative")
    assert right_res["ok"] is True
    view = practice.state_view(pid)
    assert view["complete"] is True
