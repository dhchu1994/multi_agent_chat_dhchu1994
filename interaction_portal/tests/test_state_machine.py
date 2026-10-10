"""Tests for StateMachine page transitions, timers, and condition logic."""

import pytest
from app.config_loader import load_config
from app.event_store import EventStore, SessionLogger
from app.state_machine import StateMachine, SessionState


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    store = EventStore(tmp_path / "sm_test.sqlite3")
    current_time = [1000.0]

    def fake_clock():
        return current_time[0]

    sm = StateMachine(cfg, store, clock=fake_clock)
    return cfg, store, sm, current_time


def test_ai_page_flow(env):
    cfg, store, sm, clock = env
    logger = SessionLogger(store, "pid_ai")
    state = sm.new_state("pid_ai", "EVA-TASK")

    assert state.page == "orientation"
    state = sm.advance(state, logger)
    assert state.page == "team"
    state = sm.advance(state, logger)
    assert state.page == "role"
    state = sm.advance(state, logger)
    assert state.page == "practice"
    state = sm.advance(state, logger)
    assert state.page == "brief"
    assert sm.client_id(state) == state.client_order[0]
    state = sm.advance(state, logger)
    assert state.page == "task"
    assert sm.task_mode(state) == "team"
    state = sm.advance(state, logger)
    assert state.page == "submit"
    state = sm.advance(state, logger)
    assert state.page == "checkin"
    state = sm.advance(state, logger)
    assert state.page == "brief"
    assert state.client_index == 1


def test_noai_page_flow_skips_team_and_role(env):
    cfg, store, sm, clock = env
    logger = SessionLogger(store, "pid_noai")
    state = sm.new_state("pid_noai", "NOAI")

    assert state.page == "orientation"
    # NOAI skips team and role straight to practice
    state = sm.advance(state, logger)
    assert state.page == "practice"
    state = sm.advance(state, logger)
    assert state.page == "brief"
    state = sm.advance(state, logger)
    assert state.page == "task"
    assert sm.task_mode(state) == "pack"


def test_break_after_position_three(env):
    cfg, store, sm, clock = env
    logger = SessionLogger(store, "pid_break")
    state = sm.new_state("pid_break", "PAS-COORD")
    # Advance to client index 2 (third client: 0, 1, 2)
    state.page = "checkin"
    state.client_index = 2
    # Advance from checkin of 3rd client
    state = sm.advance(state, logger)
    assert state.page == "break"
    assert state.break_deadline is not None
    # Advance from break -> brief of client 4
    state = sm.advance(state, logger)
    assert state.page == "brief"
    assert state.client_index == 3


def test_seventh_client_alone_and_exit(env):
    cfg, store, sm, clock = env
    logger = SessionLogger(store, "pid_alone")
    state = sm.new_state("pid_alone", "GEN-DEV")
    state.client_index = 6  # 7th client
    state.page = "brief"
    state = sm.advance(state, logger)
    assert state.page == "task"
    assert sm.is_last_client(state) is True
    assert sm.task_mode(state) == "alone"
    assert sm.card_role(state) == "none"

    state = sm.advance(state, logger)
    assert state.page == "submit"
    state = sm.advance(state, logger)
    assert state.page == "exit"
    assert state.completion_code is not None
    assert state.completion_code.startswith("IP2-")


def test_wall_clock_timers_and_lockouts(env):
    cfg, store, sm, clock = env
    state = sm.new_state("pid_timers", "PAS-TASK")
    state.page = "orientation"
    state.page_entered_at = clock[0]

    cfg.timer_scale = 1.0
    # orientation_min_seconds is 15s
    assert sm.unlock_remaining(state) == 15.0
    can_adv, reason = sm.can_advance(state)
    assert can_adv is False
    assert reason == "locked"

    # Advance clock by 16s
    clock[0] += 16.0
    assert sm.unlock_remaining(state) == 0.0
    can_adv, reason = sm.can_advance(state)
    assert can_adv is True
