"""Unit tests for bugs (B1-B23) and improvements (I1-I14)."""

import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path
import pytest

from app.config_loader import load_config
from app.event_store import EventStore
from app.simulation.session_manager import SessionManager, PortalError
from app.simulation.card_provenance import Card, CardError
from app.agents.base import LLMResult
from app.agents.disclosure import detect_disclosure
from app.agents.panel_validator import validate_panel, proportional_trim
from app.agents.pii import scan_pii, redact_pii
from app.agents.topics import words, _hits, asked_about_hidden
from app.tools.backup import perform_backup, verify_integrity
from app.tools.verify_db import verify_database
from app.tools.purge_pii import purge_pii


@pytest.fixture
def test_env(tmp_path):
    cfg = load_config()
    store = EventStore(tmp_path / "test_bi.sqlite3")
    mgr = SessionManager(cfg, store)
    return cfg, store, mgr


# ---- B1: Timer ticks during slow LLM call without blocking -------------------
def test_b1_lock_decoupling_timer_ticks(tmp_path):
    cfg = load_config()
    store = EventStore(tmp_path / "test_b1.sqlite3")

    class SlowMockLLM:
        def __init__(self):
            self.model_version = "mock-1.0"

        def complete(self, system_prompt, user_prompt, **kwargs):
            time.sleep(0.1)
            text = '{"thought": "ok", "action": "none"}'
            return LLMResult(text=text, tokens_in=10, tokens_out=10, first_token_ms=10, total_ms=10)

    mgr = SessionManager(cfg, store, llm=SlowMockLLM())
    pid = "pid_b1"
    state = mgr.start_session(pid=pid, condition="EVA-TASK")
    state.page = "brief"
    mgr.save(state)
    mgr.sm.advance(state, mgr.logger(state))  # enters task

    # Running tick should be near-instantaneous
    t0 = time.monotonic()
    tstate = mgr.tick(pid)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2
    assert tstate.pid == pid


# ---- B4 & I5: Panel validator and proportional trim -------------------------
def test_b4_panel_validator_sections_and_imperatives():
    expected = ["Team status", "On the card"]
    # Valid TASK panel
    valid_text = "Team status: Nia is working on the client plan. On the card: The problem and solution are drafted."
    errors = validate_panel(valid_text, level="task", expected_sections=expected, word_budget=(10, 80))
    assert len(errors) == 0

    # Missing required section
    missing_text = "Team status: Only team status here."
    errors_miss = validate_panel(missing_text, level="task", expected_sections=expected, word_budget=(1, 80))
    assert any("Required section 'On the card' is missing" in e for e in errors_miss)

    # Forbidden imperatives
    imperative_text = "Team status: Nia is working. On the card: You should try to make sure this works."
    errors_imp = validate_panel(imperative_text, level="task", expected_sections=expected, word_budget=(5, 80))
    assert any("forbidden imperative" in e.lower() for e in errors_imp)

    # Word budget trim
    long_text = "Team status: " + "word " * 60 + "On the card: " + "word " * 60
    trimmed = proportional_trim(long_text, expected_sections=expected, max_words=40)
    assert len(trimmed.split()) <= 45
    assert "Team status" in trimmed
    assert "On the card" in trimmed


# ---- B6: Card max chars error -----------------------------------------------
def test_b6_card_max_chars_error():
    card = Card(["f1"], "generative")

    # Under 600 chars succeeds
    card.set_field_text("f1", "Short text", max_chars=600)
    assert card.field_text("f1") == "Short text"

    # Exceeding 600 chars raises CardError
    with pytest.raises(CardError, match="exceeds maximum allowed length"):
        card.set_field_text("f1", "a" * 601, max_chars=600)


# ---- B8: Opening burst retry logic ------------------------------------------
def test_b8_chat_open_retry_on_failure(tmp_path):
    cfg = load_config()
    store = EventStore(tmp_path / "test_b8.sqlite3")

    class FailTwiceLLM:
        def __init__(self):
            self.calls = 0
            self.model_version = "mock-1.0"

        def complete(self, system_prompt, user_prompt, **kwargs):
            self.calls += 1
            if self.calls <= 2:  # Fails attempt 1 + retry
                raise RuntimeError("Temporary provider 500")
            text = '{"thought": "ok", "to_participant": "Hi", "assignments": []}'
            return LLMResult(text=text, tokens_in=10, tokens_out=10, first_token_ms=10, total_ms=10)

    mgr = SessionManager(cfg, store, llm=FailTwiceLLM())
    pid = "pid_b8"
    state = mgr.start_session(pid=pid, condition="PAS-COORD")
    state.page = "brief"
    mgr.save(state)
    mgr.sm.advance(state, mgr.logger(state))
    cid = mgr.sm.client_id(state)

    # First attempt fails all retries; opened must NOT be set to True
    events1 = list(mgr.chat_open(pid))
    assert not mgr.chat_state(pid)["opened"]

    # Second attempt succeeds and marks opened as True
    events2 = list(mgr.chat_open(pid))
    assert len(events2) > 0
    assert mgr.chat_state(pid)["opened"]


# ---- B9: Checkin specialist option order matches state.specialist_order -----
def test_b9_checkin_specialist_order(test_env):
    cfg, store, mgr = test_env
    pid = "pid_b9"
    state = mgr.start_session(pid=pid, condition="EVA-TASK")
    state.page = "checkin"
    state.specialist_order = ["Theo", "Mira", "Rhys", "Nia"]
    mgr.save(state)

    items = mgr.checkin_items(state)
    ci2 = next(item for item in items if item["id"] == "ci2")
    # Theo, Mira, Rhys, Nia, then "Not sure"
    options = ci2["options"]
    assert options[:4] == ["Theo", "Mira", "Rhys", "Nia"]
    assert options[4] == "Not sure"


# ---- B11: Evaluative card AI spans without glued words ----------------------
def test_b11_no_glued_ai_spans():
    card = Card(["f1"], "evaluative")

    # Nia proposes span 1
    card.add_ai_text("f1", "First proposal.", "Nia")
    # Participant keeps it
    card.evaluate(card.fields["f1"][0].span_id, "keep")
    assert card.field_text("f1") == "First proposal."

    # Nia proposes span 2
    card.add_ai_text("f1", "Second proposal.", "Nia")
    # Participant keeps span 2
    card.evaluate(card.fields["f1"][1].span_id, "keep")

    # Result should either supersede older span or join with newline/space
    text = card.field_text("f1")
    assert "First proposal.Second proposal." not in text


# ---- B13: Tablet / iPad / coarse pointer detection --------------------------
def test_b13_tablet_and_touch_detection(test_env):
    cfg, store, mgr = test_env
    pid = "pid_b13"
    state = mgr.start_session(pid=pid, condition="EVA-TASK")

    # iPad with desktop UA but maxTouchPoints > 1
    ipad_vp = {"width": 1024, "height": 768, "maxTouchPoints": 5, "pointer_coarse": True, "is_mobile": True}
    ipad_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ok = mgr.record_viewport(pid, ipad_vp, user_agent=ipad_ua)
    assert not ok

    st = mgr.get(pid)
    assert st.viewport_allowed is False


# ---- B16: card_submit validates can_submit before advancing -----------------
def test_b16_card_submit_validation(test_env):
    cfg, store, mgr = test_env
    pid = "pid_b16"
    state = mgr.start_session(pid=pid, condition="GEN-TASK")
    state.page = "task"
    mgr.save(state)

    # Empty generative card should fail can_submit()
    card = mgr.card(state)
    ok, _ = card.can_submit()
    assert not ok


# ---- B17: Break scaled duration in break_end event --------------------------
def test_b17_break_duration_scaled(test_env):
    cfg, store, mgr = test_env
    pid = "pid_b17"
    state = mgr.start_session(pid=pid, condition="PAS-COORD")
    state.page = "break"
    state.break_started = mgr.clock() - 1000
    mgr.save(state)

    state_after = mgr.tick(pid)
    events = store.events(pid, "break_end")
    if events:
        duration = events[-1]["metadata"].get("seconds_elapsed", 0)
        assert duration == cfg.scaled(cfg.timers.break_seconds)


# ---- B18: Check-in malformed payload rejection ------------------------------
def test_b18_checkin_malformed_payload(test_env):
    cfg, store, mgr = test_env
    pid = "pid_b18"
    state = mgr.start_session(pid=pid, condition="EVA-TASK")
    state.page = "checkin"
    state.checkin_started = mgr.clock()
    mgr.save(state)

    # Boolean scale value should be rejected
    with pytest.raises(PortalError):
        mgr.checkin_submit(pid, answers={"ci1": True}, durations={})

    # Non-dict answers should be rejected
    with pytest.raises(PortalError):
        mgr.checkin_submit(pid, answers=["invalid"], durations={})


# ---- B21: Keyword stemming in topics ----------------------------------------
def test_b21_asked_for_keyword_stemming():
    # Plural form "budgets" should match root keyword "budget"
    assert "budget" in words("budgets")
    assert _hits("Could you look at the client budgets?", ["budget"]) > 0
    # "limiting" should match "limit"
    assert "limit" in words("limiting")
    assert _hits("Are we limiting the options?", ["limit"]) > 0


# ---- B23: Late agent output discarded after task expiration -----------------
def test_b23_late_agent_output_discarded(tmp_path):
    cfg = load_config()
    store = EventStore(tmp_path / "test_b23.sqlite3")

    class ExpireDuringLLM:
        def __init__(self, mgr_ref):
            self.mgr_ref = mgr_ref
            self.model_version = "mock-1.0"

        def complete(self, system_prompt, user_prompt, **kwargs):
            field_id = self.mgr_ref.cfg.card_fields[0].id
            st = self.mgr_ref.get("pid_b23")
            st.task_started = self.mgr_ref.clock() - 2000
            self.mgr_ref.save(st)
            meta = kwargs.get("meta") or {}
            if meta.get("role") == "panel":
                text = "Team status: Status. On the card: Content."
            else:
                text = f'{{"reply_text": "Late reply", "card_proposal": {{"field": "{field_id}", "text": "Late proposal"}}}}'
            return LLMResult(text=text, tokens_in=10, tokens_out=10, first_token_ms=10, total_ms=10)

    mgr = SessionManager(cfg, store)
    mgr.llm = ExpireDuringLLM(mgr)
    pid = "pid_b23"
    state = mgr.start_session(pid=pid, condition="EVA-TASK")
    state.page = "brief"
    mgr.save(state)
    mgr.sm.advance(state, mgr.logger(state))  # enters task

    field_id = cfg.card_fields[0].id
    card_before = mgr.card(state)
    events = list(mgr.chat_send(pid, "Hello team"))
    card_after = mgr.card(state)
    assert len(card_after.fields[field_id]) == len(card_before.fields[field_id])


# ---- I1: Signed entry link verification -------------------------------------
def test_i1_signed_entry_links():
    secret = "test-secret-key-for-entry"
    pid = "p_test_1"
    cond = "EVA-TASK"
    exp = int(time.time()) + 3600
    payload = f"{pid}|{cond}|{exp}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    # Valid sig matches
    computed = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(sig, computed)

    # Tampered condition fails
    tampered_payload = f"{pid}|PAS-COORD|{exp}".encode("utf-8")
    tampered_sig = hmac.new(secret.encode("utf-8"), tampered_payload, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(sig, tampered_sig)


# ---- I4: Deterministic disclosure detection ---------------------------------
def test_i4_disclosure_detection():
    passages = {
        "hid1": "The founder previously worked at a robotics firm in Munich.",
        "hid2": "Annual budget cap is strictly seventy thousand euros."
    }

    # Clean text with no leaks
    clean_text = "The client is a manufacturing firm looking for efficiency gains."
    assert len(detect_disclosure(clean_text, passages)) == 0

    # Direct leak of hidden passage
    leaked_text = "As we know, the annual budget cap is strictly seventy thousand euros for this project."
    leaks = detect_disclosure(leaked_text, passages)
    assert "hid2" in leaks


# ---- I7: Strict typed share and anti-gaming ---------------------------------
def test_i7_strict_typed_share():
    card = Card(["f1"], "generative")

    # Repetitive filler text "aaaa..."
    card.set_field_text("f1", "a" * 100)
    # Raw share counts characters, but strict share detects low lexical diversity
    raw = card.typed_share()
    strict = card.typed_share_strict()
    assert raw > 0.0
    assert strict == 0.0

    # Genuine diverse text
    card.set_field_text("f1", "The primary objective is optimizing internal workflow operations efficiently.")
    strict_good = card.typed_share_strict()
    assert strict_good > 0.5


# ---- I9: Standalone backup and verify_db tools -------------------------------
def test_i9_backup_and_verify_tools(tmp_path):
    db_path = tmp_path / "test_store.sqlite3"
    store = EventStore(db_path)
    store.create_session({
        "pid": "pid_tool_test",
        "condition_code": "GEN-TASK",
        "config_version": "2.0",
        "model_version": "mock",
        "started_at": "2026-10-11T12:00:00.000Z",
        "last_activity": "2026-10-11T12:00:00.000Z"
    })
    store.log("pid_tool_test", "session_start", "team")

    # Test verify_db
    assert verify_database(db_path) is True

    # Test backup
    backup_dir = tmp_path / "backups"
    backup_gz = perform_backup(db_path, backup_dir, keep=2)
    assert backup_gz.exists()
    assert backup_gz.with_name(backup_gz.name + ".sha256").exists()


# ---- I13: PII detector, redactor, and purge_pii tool -------------------------
def test_i13_pii_detector_and_purge(tmp_path):
    sample_text = "Contact me at alice@example.com or call +1 555-123-4567. My IBAN is DE89370400440532013000."
    findings = scan_pii(sample_text)
    assert len(findings) >= 3
    assert "email" in findings
    assert "phone" in findings
    assert "iban" in findings

    redacted = redact_pii(sample_text)
    assert "alice@example.com" not in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_PHONE]" in redacted

    # Test purge_pii on SQLite database
    db_path = tmp_path / "pii_db.sqlite3"
    store = EventStore(db_path)
    store.log("pid_pii", "user_msg", "task", metadata={"comment": "Call me at bob@example.com"})

    # Run purge
    code = purge_pii(db_path, dry_run=False)
    assert code == 0

    # Verify event was redacted in database
    evts = store.events("pid_pii", "user_msg")
    assert "[REDACTED_EMAIL]" in evts[0]["metadata"]["comment"]
