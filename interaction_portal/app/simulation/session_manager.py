"""
SessionManager: the multi-client lifecycle of one participant.

It owns session creation, resume rules, authoritative timers, the team chat (routing, structured
specialist replies, orchestrator bursts and folded blocks), the orchestrator panel (throttled,
word-budgeted), the card editor, the check-in and the exit hand-off. It is deliberately UI-free:
the FastAPI routes in ``app.main`` and the autopilot in ``app.harnesses.harness_b`` both drive
exactly the same methods.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from ..agents.base import AgentCallFailed, LLMClient, make_llm
from ..agents.orchestrator import OrchestratorAgent
from ..agents.specialist import SpecialistAgent, SpecialistResult, Trigger
from ..config_loader import PortalConfig, build_reference_pack
from ..event_store import EventStore, SessionLogger, utc_now_iso
from ..state_machine import SessionState, StateMachine
from .card_provenance import Card, CardError
from .chat_router import Route, format_options, parse_message

_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="agent")

CLIENT_EVENTS = {
    "window_blur", "window_focus", "scroll_sample", "fold_open", "fold_close",
    "pack_open", "pack_close", "theme_change", "card_paste",
    "keystroke_summary", "paste", "cut", "drop", "focus_field", "blur_field",
}


class PortalError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


class SessionManager:
    def __init__(self, cfg: PortalConfig, store: EventStore, llm: LLMClient | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.store, self.clock = cfg, store, clock
        self.llm = llm or make_llm(cfg)
        self.sm = StateMachine(cfg, store, clock)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._last_touch: dict[str, float] = {}
        max_p = getattr(cfg, "max_concurrent_participants", 100) or 100
        desired_workers = min(512, max(32, max_p * 2))
        global _POOL
        if getattr(_POOL, "_max_workers", 16) < desired_workers:
            _POOL = ThreadPoolExecutor(max_workers=desired_workers, thread_name_prefix="agent")

    # ------------------------------------------------------------------ basics
    def lock(self, pid: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(pid, threading.RLock())

    def logger(self, state: SessionState) -> SessionLogger:
        return SessionLogger(self.store, state.pid, state.autopilot)

    def logger_for(self, pid: str) -> SessionLogger:
        return SessionLogger(self.store, pid, False)

    def get(self, pid: str) -> SessionState:
        st = self.sm.load(pid)
        if st is None:
            raise PortalError(self.cfg.text("errors.no_session"), 404)
        return st

    def save(self, state: SessionState) -> None:
        self.sm.save(state)

    # ------------------------------------------------------------------ entry / resume
    def start_session(self, pid: str, condition: str, user_agent: str = "", viewport: dict | None = None,
                      autopilot: bool = False, rng=None) -> SessionState | None:
        """Create the session atomically. Returns None if the pid was already used."""
        state = self.sm.new_state(pid, condition, autopilot, rng)
        now_iso = utc_now_iso()
        resume_token = secrets.token_urlsafe(24)
        row = {
            "pid": pid, "condition_code": condition, "config_version": self.cfg.config_version,
            "model_version": self.llm.model_version, "started_at": now_iso, "last_activity": now_iso,
            "specialist_order": json.dumps(state.specialist_order), "client_order": json.dumps(state.client_order),
            "autopilot": int(autopilot), "state": json.dumps(state.to_json()),
            "resume_token": resume_token, "config_sha256": self.cfg.config_sha256,
        }
        if not self.store.create_session(row):
            return None
        log = self.logger(state)
        log.log("session_start", "entry", None, condition_code=condition, config_version=self.cfg.config_version,
                model_version=self.llm.model_version, user_agent=user_agent, viewport=viewport,
                client_order=state.client_order, specialist_order=state.specialist_order)
        log.log("page_enter", "orientation", None)
        return state

    def resume_or_expire(self, pid: str) -> tuple[SessionState | None, str]:
        """Called when a known pid comes back: returns (state, "") to resume or (None, reason)."""
        row = self.store.get_session(pid)
        if not row:
            return None, "no_session"
        if row["end_state"]:
            return None, "ended"
        state = self.get(pid)
        gap = self._gap_seconds(row)
        if gap > self.cfg.scaled(self.cfg.timers.resume_gap_seconds):
            self.end_session(state, "abandoned_timeout")
            return None, "timeout"
        self.store.update_session(pid, resume_count=row["resume_count"] + 1, last_activity=utc_now_iso())
        self.logger(state).log("session_resume", state.page, self.sm.client_id(state), gap_seconds=round(gap, 1))
        return state, ""

    def _gap_seconds(self, row: dict) -> float:
        from datetime import datetime, timezone
        last = datetime.strptime(row["last_activity"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        return self.clock() - last.timestamp()

    def check_active(self, pid: str) -> SessionState:
        """Guard for every request: unknown/ended/timed-out sessions raise PortalError."""
        row = self.store.get_session(pid)
        if not row:
            raise PortalError(self.cfg.text("errors.no_session"), 404)
        if row["end_state"] and row["end_state"] != "completed":
            raise PortalError(self.cfg.text("errors.timeout"), 410)
        state = self.get(pid)
        if not row["end_state"] and self._gap_seconds(row) > self.cfg.scaled(self.cfg.timers.resume_gap_seconds):
            self.end_session(state, "abandoned_timeout")
            raise PortalError(self.cfg.text("errors.timeout"), 410)
        return state

    def end_session(self, state: SessionState, end_state: str) -> None:
        row = self.store.get_session(state.pid)
        if row and row["end_state"]:
            return
        total = round(self.clock() - state.started_at_epoch, 1)
        self.store.update_session(state.pid, ended_at=utc_now_iso(), end_state=end_state,
                                 completion_code=state.completion_code)
        self.logger(state).log("session_end", state.page, self.sm.client_id(state), end_state=end_state,
                               completion_code=state.completion_code, total_seconds=total)

    # ------------------------------------------------------------------ flow
    def tick(self, pid: str) -> SessionState:
        """Apply server-side timer expiry (task -> submission, break -> next brief)."""
        with self.lock(pid):
            state = self.check_active(pid)
            log = self.logger(state)
            if state.page == "task" and self.sm.task_remaining(state) <= 0 and not state.task_expired_logged:
                state.task_expired_logged = True
                log.log("timer_expired", "task", self.sm.client_id(state), timer_name="task")
                self.sm.advance(state, log, reason="timer_expired")
            elif state.page == "break" and self.sm.break_remaining(state) <= 0:
                log.log("break_end", "break", self.sm.client_id(state), ended_early=False,
                        seconds_elapsed=self.cfg.scaled(self.cfg.timers.break_seconds))
                log.log("timer_expired", "break", self.sm.client_id(state), timer_name="break")
                self.sm.advance(state, log, reason="timer_expired")
            self._after_advance(state)
            self.touch(state)
            return state

    def touch(self, state: SessionState, force: bool = False) -> None:
        now = self.clock()
        last = self._last_touch.get(state.pid, 0.0)
        if not force and (now - last) < 10.0:
            return
        self._last_touch[state.pid] = now
        self.store.update_session(state.pid, last_activity=utc_now_iso())

    def _after_advance(self, state: SessionState) -> None:
        if state.page == "exit" and not self.store.get_session(state.pid)["end_state"]:
            self.end_session(state, "completed")

    def advance(self, pid: str) -> SessionState:
        """The participant pressed Continue on a plain page (orientation, team, role, brief)."""
        with self.lock(pid):
            state = self.tick(pid)
            if state.page not in ("orientation", "team", "role", "brief", "practice"):
                raise PortalError("This page has its own action.", 400)
            ok, why = self.sm.can_advance(state)
            if not ok:
                raise PortalError("Please keep reading: Continue is not unlocked yet.", 409)
            if state.page == "practice" and not state.practice_flags.get("practice_complete"):
                raise PortalError("Finish every practice step first.", 409)
            self.sm.advance(state, self.logger(state))
            self._after_advance(state)
            return state

    def go_to_submission(self, pid: str) -> SessionState:
        with self.lock(pid):
            state = self.tick(pid)
            if state.page != "task":
                raise PortalError("Not on the task page.", 409)
            self.sm.advance(state, self.logger(state), reason="participant")
            return state

    def end_break(self, pid: str) -> SessionState:
        with self.lock(pid):
            state = self.tick(pid)
            if state.page != "break":
                raise PortalError("Not on the break page.", 409)
            log = self.logger(state)
            elapsed = self.cfg.scaled(self.cfg.timers.break_seconds) - self.sm.break_remaining(state)
            log.log("break_end", "break", self.sm.client_id(state), ended_early=True, seconds_elapsed=round(elapsed, 1))
            self.sm.advance(state, log, reason="ended_early")
            return state

    # ------------------------------------------------------------------ client events
    def client_event(self, pid: str, event_type: str, metadata: dict[str, Any]) -> None:
        if event_type not in CLIENT_EVENTS:
            raise PortalError("unknown event", 400)
        with self.lock(pid):
            state = self.check_active(pid)
            if event_type in ("pack_open", "pack_close") and state.page == "practice" and event_type == "pack_open":
                state.practice_flags["pack_opened"] = True
                self.save(state)
            clean = {}
            for k, v in metadata.items():
                if not isinstance(k, str) or len(k) > 64:
                    continue
                if isinstance(v, str):
                    clean[k] = v[:64]
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    clean[k] = v
                elif isinstance(v, bool):
                    clean[k] = v
            t_raw = metadata.get("timestamp") or metadata.get("t_client")
            t_client = None
            if isinstance(t_raw, (int, float)):
                try:
                    t_client = datetime.fromtimestamp(t_raw / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                except Exception:
                    pass
            elif isinstance(t_raw, str) and t_raw:
                t_client = t_raw[:64]
            self.logger(state).log(event_type, state.page, self.sm.client_id(state), timestamp=t_client, **clean)
            self.touch(state)

    def client_events_batch(self, pid: str, events: list[dict]) -> None:
        with self.lock(pid):
            state = self.check_active(pid)
            cid = self.sm.client_id(state)
            clean_events = []
            now_iso = utc_now_iso()
            pack_opened_now = False
            for raw in events:
                etype = str(raw.get("type") or raw.get("event_type") or "")
                if etype not in CLIENT_EVENTS:
                    continue
                if etype == "pack_open" and state.page == "practice":
                    pack_opened_now = True
                meta = raw.get("metadata") or {}
                clean_meta = {}
                for k, v in meta.items():
                    if not isinstance(k, str) or len(k) > 64:
                        continue
                    if isinstance(v, str):
                        clean_meta[k] = v[:64]
                    elif isinstance(v, (int, float)) and not isinstance(v, bool):
                        clean_meta[k] = v
                    elif isinstance(v, bool):
                        clean_meta[k] = v
                t_raw = raw.get("timestamp") or raw.get("t_client") or meta.get("t_client")
                if isinstance(t_raw, (int, float)):
                    try:
                        t_client = datetime.fromtimestamp(t_raw / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                    except Exception:
                        t_client = now_iso
                elif isinstance(t_raw, str) and t_raw:
                    t_client = t_raw[:64]
                else:
                    t_client = now_iso
                clean_events.append({
                    "pid": pid,
                    "timestamp": t_client,
                    "client_id": cid,
                    "page_id": state.page,
                    "event_type": etype,
                    "autopilot": state.autopilot,
                    "metadata": clean_meta
                })
            if pack_opened_now:
                state.practice_flags["pack_opened"] = True
                self.save(state)
            if clean_events:
                self.store.log_batch(clean_events)
            self.touch(state)

    def record_viewport(self, pid: str, viewport: dict, user_agent: str = "") -> bool:
        """Returns False when the viewport or device is blocked (tablet/iPad, coarse pointer, mobile)."""
        width = int(viewport.get("width", 0))
        max_touch = int(viewport.get("maxTouchPoints", 0))
        pointer_coarse = bool(viewport.get("pointer_coarse", False))
        is_mobile = bool(viewport.get("is_mobile", False))
        ua = user_agent.lower()

        allowed = True
        if not self.cfg.allow_mobile:
            if width < self.cfg.min_viewport_width:
                allowed = False
            elif pointer_coarse:
                allowed = False
            elif max_touch > 1 and ("macintosh" in ua or "mac os x" in ua):
                allowed = False
            elif is_mobile:
                allowed = False

        with self.lock(pid):
            state = self.check_active(pid)
            state.viewport = viewport
            state.viewport_allowed = allowed
            self.save(state)
            self.logger(state).log("client_info", state.page, None, viewport=viewport, allowed=allowed)
        return allowed

    # ------------------------------------------------------------------ card
    def card(self, state: SessionState, key: str | None = None) -> Card:
        key = key or state.client_order[state.client_index]
        if key in state.cards:
            return Card.from_json(state.cards[key], self.cfg.editor.send_back_needs_reason, self.cfg.editor.min_typed_share)
        role = self.sm.card_role(state)
        return Card(self.cfg.card_field_ids, role, self.cfg.editor.send_back_needs_reason, self.cfg.editor.min_typed_share)

    def _save_card(self, state: SessionState, card: Card, key: str | None = None) -> None:
        state.cards[key or state.client_order[state.client_index]] = card.to_json()

    def card_view(self, state: SessionState, card: Card | None = None) -> dict:
        card = card or self.card(state)
        d = card.to_json(full=False)
        d["min_typed_share"] = self.cfg.editor.min_typed_share if card.role == "generative" else None
        d["max_span_chars"] = self.cfg.editor.max_span_chars
        d["can_submit"], d["blocked_reason"] = card.can_submit()
        d["send_back_needs_reason"] = self.cfg.editor.send_back_needs_reason
        d["allow_paste"] = self.cfg.editor.allow_paste
        return d

    def _log_card_events(self, state: SessionState, events: list[dict]) -> None:
        cid = self.sm.client_id(state)
        for e in events:
            e = dict(e)
            if e["action"] == "created":
                e["action"] = "type"            # taxonomy: an AI author "types" a span onto the card
            self.logger(state).log("card_action", state.page, cid, **e)

    def _require_card_page(self, state: SessionState) -> None:
        if state.page not in ("task", "submit"):
            raise PortalError("The card is not open on this page.", 409)

    def card_action(self, pid: str, action: str, span_id: str = "", reason: str = "", field: str = "", text: str = "", pasted: bool = False) -> dict:
        fid = ""
        span = None
        with self.lock(pid):
            state = self.tick(pid)
            self._require_card_page(state)
            card = self.card(state)
            try:
                if action in ("keep", "cut", "send_back"):
                    fid, span, evt = card.evaluate(span_id, action, reason)
                    self._log_card_events(state, [evt])
                    self._save_card(state, card)
                    self.save(state)
                elif action == "type":
                    if not self.cfg.editor.allow_paste and pasted:
                        raise PortalError("Pasting is not allowed on this card.", 400)
                    evts = card.set_field_text(field, text, self.cfg.editor.max_span_chars, pasted=pasted)
                    self._log_card_events(state, evts)
                    self._save_card(state, card)
                    self.save(state)
                elif action == "insert":
                    _, _, evt = card.insert_suggestion(span_id)
                    self._log_card_events(state, [evt])
                    self._save_card(state, card)
                    self.save(state)
                else:
                    raise PortalError("unknown card action")
            except CardError as exc:
                raise PortalError(str(exc), 400)
            self.touch(state)

        if action == "send_back" and fid and span:
            self._revise_after_send_back(pid, fid, span, reason)

        with self.lock(pid):
            state = self.get(pid)
            return self.card_view(state, self.card(state))

    def _revise_after_send_back(self, pid: str, field_id: str, span, reason: str) -> None:
        """A sent-back span goes back to the specialist who wrote it, who writes a replacement."""
        with self.lock(pid):
            state = self.check_active(pid)
            cid = self.sm.client_id(state)
            if state.page != "task" or self.sm.task_mode(state) != "team" or span.author not in self.cfg.specialist_names:
                return
            client = self.cfg.clients[cid]
            agent = SpecialistAgent(self.cfg, self.cfg.specialist(span.author), client, self.llm)
            agent.on_error = self._on_error(state, cid)
            transcript = self.store.transcript(state.pid, cid)

        try:
            res = agent.reply(transcript, Trigger("revise", "Participant", "", {"field": field_id, "old_text": span.text, "reason": reason}))
        except AgentCallFailed:
            return

        with self.lock(pid):
            state = self.tick(pid)
            if state.page != "task" or self.sm.client_id(state) != cid or self.sm.task_remaining(state) <= 0:
                self.logger(state).log("agent_discarded_late", state.page, cid, reason="task_expired_or_moved")
                return
            card = self.card(state)
            if res.output.card_proposal:
                new, evt = card.add_ai_text(field_id, res.output.card_proposal.text, span.author, replaces=span.span_id)
                self._log_card_events(state, [evt])
                self._save_card(state, card)
                self.save(state)
                self.logger(state).log("agent_reply", "task", cid, agent=span.author, in_reply_to=span.span_id, text=res.output.card_proposal.text,
                        latency_first_token_ms=res.llm.first_token_ms, latency_total_ms=res.llm.total_ms,
                        tokens_in=res.llm.tokens_in, tokens_out=res.llm.tokens_out, disclosed_items=res.output.disclosed_hidden_items,
                        asked_for=False, declined=False, redirected_to=None, revision=True)
                self._mark_dirty(state, cid)

    def card_submit(self, pid: str) -> SessionState:
        with self.lock(pid):
            state = self.tick(pid)
            if state.page not in ("task", "submit"):
                raise PortalError("Nothing to submit here.", 409)
            card = self.card(state)
            ok, why = card.can_submit()
            if not ok:
                raise PortalError(self.cfg.text("task.submit_blocked_share", min=int(self.cfg.editor.min_typed_share * 100)), 409)
            if state.page == "task":
                self.sm.advance(state, self.logger(state), reason="participant")   # to submit
            cid = state.client_order[state.client_index]
            cj = card.to_json()
            self.logger(state).log("card_submit", "submit", cid, full_card_json=cj, typed_share=cj["typed_share"],
                                   fields_complete=cj["fields_complete"], provenance_summary=cj["provenance_summary"])
            self.store.add_card(pid, cid, cj, state.autopilot)
            self._save_card(state, card)
            self.sm.advance(state, self.logger(state), reason="submitted")
            self._after_advance(state)
            return state

    # ------------------------------------------------------------------ check-in
    def checkin_items(self, state: SessionState) -> list[dict]:
        cond = self.sm.condition(state)
        items = []
        for it in self.cfg.text("checkin.items"):
            if it.get("ai_only") and not cond.ai:
                continue
            it = dict(it)
            if it["type"] == "specialists":
                opts = list(state.specialist_order) + list(it.get("extra_options", []))
                it["options"] = opts
                it["options_order"] = opts
            items.append(it)
        return items

    def checkin_submit(self, pid: str, answers: dict[str, Any], durations: dict[str, Any] | None = None) -> SessionState:
        if not isinstance(answers, dict):
            raise PortalError("Answers must be a JSON object.", 400)
        with self.lock(pid):
            state = self.tick(pid)
            if state.page != "checkin":
                raise PortalError("Not on the check-in page.", 409)
            items = self.checkin_items(state)
            durations = durations if isinstance(durations, dict) else {}
            for it in items:
                v = answers.get(it["id"])
                if v in (None, ""):
                    raise PortalError(self.cfg.text("checkin.missing"), 400)
                if it["type"] == "scale":
                    if isinstance(v, bool):
                        raise PortalError("invalid answer", 400)
                    try:
                        iv = int(v)
                    except (TypeError, ValueError):
                        raise PortalError("invalid answer", 400)
                    if not it["scale_min"] <= iv <= it["scale_max"]:
                        raise PortalError("invalid answer", 400)
                elif v not in it["options"]:
                    raise PortalError("invalid answer", 400)
            log = self.logger(state)
            cid = self.sm.client_id(state)
            total_ms = int((self.clock() - state.checkin_started) * 1000)
            for it in items:
                raw_ms = durations.get(it["id"])
                if isinstance(raw_ms, (int, float)) and not isinstance(raw_ms, bool):
                    bounded_ms = max(0, min(3_600_000, int(raw_ms)))
                else:
                    bounded_ms = total_ms // max(1, len(items))
                meta = {"item_id": it["id"], "value": answers[it["id"]], "duration_ms": bounded_ms}
                if "options" in it:
                    meta["options_order"] = it["options"]
                log.log("checkin_answer", "checkin", cid, **meta)
            self.sm.advance(state, log, reason="submitted")
            return state

    # ------------------------------------------------------------------ reference pack
    def reference_pack(self, state: SessionState) -> list[dict]:
        return build_reference_pack(self.cfg, state.client_order[state.client_index])

    # ------------------------------------------------------------------ chat
    def _on_error(self, state: SessionState, cid: str | None):
        def cb(agent: str, error_type: str, retry: int) -> None:
            self.logger(state).log("agent_error", state.page, cid, agent=agent, error_type=error_type, retry_count=retry)
        return cb

    def _chat(self, state: SessionState, cid: str) -> dict:
        return state.chat.setdefault(cid, {"block_seq": 0, "opened": False, "panel_last_at": 0.0, "panel_dirty": False,
                                           "panel_text": "", "panel_words": 0, "panel_sections": [],
                                           "contributed": [], "asked": []})

    def _mark_dirty(self, state: SessionState, cid: str) -> None:
        self._chat(state, cid)["panel_dirty"] = True

    def _require_team_task(self, state: SessionState) -> str:
        if state.page != "task" or self.sm.task_mode(state) != "team":
            raise PortalError("The team chat is not available on this page.", 409)
        if self.sm.task_remaining(state) <= 0:
            raise PortalError("Time is up.", 409)
        return state.client_order[state.client_index]

    def _msg(self, state: SessionState, cid: str, sender: str, to: list[str], text: str, kind: str = "chat",
             block_id: str | None = None, **extra: Any) -> dict:
        m = {"id": "m_" + uuid.uuid4().hex[:8], "ts": utc_now_iso(), "sender": sender, "to": to, "text": text,
             "kind": kind, "block_id": block_id, "extra": extra}
        self.store.add_transcript(state.pid, cid, m)
        return m

    def chat_state(self, pid: str) -> dict:
        state = self.tick(pid)
        cid = state.client_order[state.client_index]
        out = {"messages": self.store.transcript(pid, cid) if self.sm.task_mode(state) == "team" else [],
               "panel": self._panel_view(state, cid), "opened": self._chat(state, cid)["opened"] if cid else False}
        return out

    def _agents(self, state: SessionState, cid: str) -> tuple[dict[str, SpecialistAgent], OrchestratorAgent]:
        client = self.cfg.clients[cid]
        cb = self._on_error(state, cid)
        specs = {}
        for s in self.cfg.specialists:
            a = SpecialistAgent(self.cfg, s, client, self.llm)
            a.on_error = cb
            specs[s.name] = a
        orch = OrchestratorAgent(self.cfg, client, self.llm)
        orch.on_error = cb
        return specs, orch

    def _log_reply(self, state: SessionState, cid: str, name: str, in_reply_to: str, res: SpecialistResult, text: str, queue_wait_ms: int = 0) -> None:
        o = res.output
        self.logger(state).log("agent_reply", "task", cid, agent=name, in_reply_to=in_reply_to, text=text,
                               latency_first_token_ms=res.llm.first_token_ms, latency_total_ms=res.llm.total_ms,
                               queue_wait_ms=queue_wait_ms,
                               tokens_in=res.llm.tokens_in, tokens_out=res.llm.tokens_out,
                               disclosed_items=o.disclosed_hidden_items, asked_for=res.asked_for,
                               declined=o.declined, redirected_to=o.redirect_to)

    def _apply_proposal(self, state: SessionState, name: str, res: SpecialistResult) -> bool:
        p = res.output.card_proposal
        if not p:
            return False
        card = self.card(state)
        _, evt = card.add_ai_text(p.field, p.text, name)
        self._log_card_events(state, [evt])
        self._save_card(state, card)
        return True

    def _failure_line(self, state: SessionState, cid: str, name: str) -> dict:
        return self._msg(state, cid, "System", ["everyone"], self.cfg.text("task.agent_failed", name=name), kind="system")

    def _run_burst(self, pid: str, cid: str, assignments: list, specs: dict[str, SpecialistAgent],
                   draft_card: bool = False) -> Iterator[dict]:
        """One orchestrator burst: assignments + specialist replies, all in ONE folded block."""
        with self.lock(pid):
            state = self.tick(pid)
            if state.page != "task" or self.sm.client_id(state) != cid or self.sm.task_remaining(state) <= 0:
                self.logger(state).log("agent_discarded_late", state.page, cid, reason="task_expired_or_moved")
                return
            chat = self._chat(state, cid)
            if not assignments and not draft_card:
                return
            chat["block_seq"] += 1
            block_id = f"blk_{chat['block_seq']:02d}"
            orch = self.cfg.orchestrator_name
            sent: dict[str, dict] = {}
            for a in assignments:
                m = self._msg(state, cid, orch, [a.to], a.text, kind="internal", block_id=block_id)
                sent[a.to] = m
                self.logger(state).log("orch_message", "task", cid, to_agent=a.to, text=a.text, block_id=block_id)
                yield {"type": "message", "message": m}
            targets = [s.name for s in self.cfg.specialists if s.name in sent or draft_card]
            snapshot = self.store.transcript(state.pid, cid)
            self.save(state)

        t_submit = time.monotonic()

        def call(name: str):
            q_wait = int((time.monotonic() - t_submit) * 1000)
            kind = "draft_card" if draft_card else "orchestrator"
            text_arg = sent[name]["text"] if name in sent else ""
            try:
                return name, specs[name].reply(snapshot, Trigger(kind, orch, text_arg)), q_wait
            except AgentCallFailed:
                return name, None, q_wait

        futs = [_POOL.submit(call, n) for n in targets]
        for fut in as_completed(futs):
            name, res, q_wait = fut.result()
            with self.lock(pid):
                state = self.tick(pid)
                if state.page != "task" or self.sm.client_id(state) != cid or self.sm.task_remaining(state) <= 0:
                    self.logger(state).log("agent_discarded_late", state.page, cid, reason="task_expired_or_moved")
                    return
                chat = self._chat(state, cid)
                if res is None:
                    yield {"type": "message", "message": self._failure_line(state, cid, name)}
                    self.save(state)
                    continue
                self._apply_proposal(state, name, res)
                text_out = res.output.reply_text
                in_reply = sent[name]["id"] if name in sent else ""
                if text_out:
                    m = self._msg(state, cid, name, [orch], text_out, kind="internal", block_id=block_id,
                                  disclosed=res.output.disclosed_hidden_items)
                    self._log_reply(state, cid, name, in_reply, res, text_out, queue_wait_ms=q_wait)
                    if name not in chat["contributed"]:
                        chat["contributed"].append(name)
                    yield {"type": "message", "message": m}
                self._mark_dirty(state, cid)
                self.save(state)

        with self.lock(pid):
            state = self.tick(pid)
            if state.page == "task" and self.sm.client_id(state) == cid and self.sm.task_remaining(state) > 0:
                yield {"type": "card", "card": self.card_view(state)}

    def chat_open(self, pid: str) -> Iterator[dict]:
        """Opening coordination burst when the task page is first shown for a client."""
        with self.lock(pid):
            state = self.tick(pid)
            cid = self._require_team_task(state)
            chat = self._chat(state, cid)
            if chat["opened"]:
                return
            specs, orch = self._agents(state, cid)
            snapshot = self.store.transcript(pid, cid)

        try:
            out, _ = orch.plan(snapshot, "opening")
        except AgentCallFailed:
            with self.lock(pid):
                state = self.check_active(pid)
                yield {"type": "message", "message": self._failure_line(state, cid, self.cfg.orchestrator_name)}
            return

        with self.lock(pid):
            state = self.tick(pid)
            if state.page != "task" or self.sm.client_id(state) != cid or self.sm.task_remaining(state) <= 0:
                self.logger(state).log("agent_discarded_late", state.page, cid, reason="task_expired_or_moved")
                return
            chat = self._chat(state, cid)
            chat["opened"] = True
            self.save(state)

        if out:
            yield from self._run_burst(pid, cid, out.assignments, specs, draft_card=True)

        panel = None
        with self.lock(pid):
            state = self.tick(pid)
            if state.page == "task" and self.sm.client_id(state) == cid and self.sm.task_remaining(state) > 0:
                panel = self.refresh_panel(state, cid, force=True)
                self.save(state)
        if panel:
            yield {"type": "panel", "panel": panel}

    def chat_send(self, pid: str, text: str) -> Iterator[dict]:
        text = (text or "").strip()
        with self.lock(pid):
            state = self.tick(pid)
            cid = self._require_team_task(state)
            if not text:
                return
            if len(text) > 2000:
                text = text[:2000]
            names = self.cfg.all_agent_names
            route = parse_message(text, names)
            if route.kind == "error":
                yield {"type": "warning", "text": self.cfg.text("task.unknown_mention", name=route.unknown,
                                                                options=format_options(route.suggestions))}
                return
            chat = self._chat(state, cid)
            addressees = route.addressees or ["everyone"]
            msg = self._msg(state, cid, "Participant", addressees, text)
            self.logger(state).log("message_sent", "task", cid, text=text, addressees=addressees,
                                   mention_used=route.mention_used, char_count=len(text))
            yield {"type": "message", "message": msg}
            specs, orch = self._agents(state, cid)
            snapshot = self.store.transcript(pid, cid)
            orch_name = self.cfg.orchestrator_name
            self.save(state)

        def spec_call(name: str, kind: str):
            try:
                return name, specs[name].reply(snapshot, Trigger(kind, "Participant", text))
            except AgentCallFailed:
                return name, None

        def orch_call(kind: str):
            try:
                return orch.plan(snapshot, kind, text)[0]
            except AgentCallFailed:
                return None

        plan_out = None
        if route.kind == "broadcast":
            fut = _POOL.submit(orch_call, "broadcast")
            results = list(_POOL.map(lambda n: spec_call(n, "broadcast"), self.cfg.specialist_names))
            plan_out = fut.result()
        else:
            spec_targets = [a for a in route.addressees if a != orch_name]
            fut = _POOL.submit(orch_call, "mention") if orch_name in route.addressees else None
            results = list(_POOL.map(lambda n: spec_call(n, "mention"), spec_targets))
            if fut:
                plan_out = fut.result()

        with self.lock(pid):
            state = self.tick(pid)
            if state.page != "task" or self.sm.client_id(state) != cid or self.sm.task_remaining(state) <= 0:
                self.logger(state).log("agent_discarded_late", state.page, cid, reason="task_expired_or_moved")
                return
            chat = self._chat(state, cid)
            if route.kind == "broadcast" and plan_out is None:
                yield {"type": "message", "message": self._failure_line(state, cid, orch_name)}
            elif route.kind != "broadcast" and orch_name in route.addressees and plan_out is None:
                yield {"type": "message", "message": self._failure_line(state, cid, orch_name)}

            # specialists' public replies (never folded)
            for name, res in results:
                if res is None:
                    yield {"type": "message", "message": self._failure_line(state, cid, name)}
                    continue
                self._apply_proposal(state, name, res)
                if route.kind == "directed" and name not in chat["asked"]:
                    chat["asked"].append(name)
                if res.output.reply_text:
                    m = self._msg(state, cid, name, ["Participant"], res.output.reply_text,
                                  disclosed=res.output.disclosed_hidden_items, in_reply_to=msg["id"])
                    self._log_reply(state, cid, name, msg["id"], res, res.output.reply_text)
                    for lst in (chat["contributed"], chat["asked"]):
                        if name not in lst:
                            lst.append(name)
                    yield {"type": "message", "message": m}

            if plan_out and plan_out.to_participant:
                m = self._msg(state, cid, orch_name, ["Participant"], plan_out.to_participant, in_reply_to=msg["id"])
                yield {"type": "message", "message": m}
            self.save(state)

        if plan_out and plan_out.assignments:
            yield from self._run_burst(pid, cid, plan_out.assignments, specs)

        with self.lock(pid):
            state = self.tick(pid)
            if state.page == "task" and self.sm.client_id(state) == cid and self.sm.task_remaining(state) > 0:
                self._mark_dirty(state, cid)
                self.save(state)
                yield {"type": "card", "card": self.card_view(state)}
                panel = self.refresh_panel(state, cid)
                if panel:
                    yield {"type": "panel", "panel": panel}
                self.save(state)
                self.touch(state)

    # ------------------------------------------------------------------ panel
    def _team_status(self, state: SessionState, cid: str) -> str:
        chat = self._chat(state, cid)
        names = self.cfg.specialist_names
        replied = [n for n in names if n in chat["contributed"]]
        unasked = [n for n in names if n not in chat["asked"] and n not in chat["contributed"]]

        def join(xs: list[str]) -> str:
            return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]

        parts = []
        if replied:
            parts.append(f"{join(replied)} {'has' if len(replied) == 1 else 'have'} contributed.")
        else:
            parts.append("No specialist has contributed yet.")
        if unasked and self.cfg.panel.name_unasked:
            parts.append(f"{join(unasked)} {'has' if len(unasked) == 1 else 'have'} not been asked.")
        return " ".join(parts)

    def _card_status(self, state: SessionState) -> str:
        card = self.card(state)
        texts = card.full_text()
        lines = [f"{sum(1 for t in texts.values() if t.strip())} of {len(texts)} card fields have content."]
        for f in self.cfg.card_fields:
            lines.append(f"- {f.label}: {texts[f.id] or '(empty)'}")
        pending = sum(1 for spans in card.fields.values() for s in spans if s.status == "proposed")
        if card.role == "evaluative" and pending:
            lines.append(f"{pending} proposed part(s) are not yet judged by the participant.")
        return "\n".join(lines)

    def _panel_view(self, state: SessionState, cid: str | None) -> dict | None:
        if not cid:
            return None
        chat = self._chat(state, cid)
        if not chat["panel_text"]:
            return None
        return {"text": chat["panel_text"], "word_count": chat["panel_words"], "sections": chat["panel_sections"],
                "level": self.sm.condition(state).panel_level}

    def refresh_panel(self, state: SessionState, cid: str, force: bool = False) -> dict | None:
        """Regenerate the panel when team status changed, at most once per refresh_min_seconds."""
        cond = self.sm.condition(state)
        if not cond.ai or cond.panel_level == "none" or self.sm.task_mode(state) != "team":
            return None
        chat = self._chat(state, cid)
        now = self.clock()
        due = now - chat["panel_last_at"] >= self.cfg.scaled(self.cfg.panel.refresh_min_seconds)
        if not force and not (chat["panel_dirty"] and (due or not chat["panel_text"])):
            return None
        _, orch = self._agents(state, cid)
        unasked = [n for n in self.cfg.specialist_names if n not in chat["asked"] and n not in chat["contributed"]]
        try:
            res = orch.generate_panel(cond.panel_level, self.store.transcript(state.pid, cid),
                                      self._team_status(state, cid), self._card_status(state),
                                      unasked_specialists=unasked)
        except AgentCallFailed:
            return None
        changed = res.text != chat["panel_text"]
        chat.update(panel_last_at=now, panel_dirty=False, panel_text=res.text, panel_words=res.word_count,
                    panel_sections=res.sections_present)
        if changed:
            self.store.add_panel(state.pid, cid, cond.panel_level, res.text, res.word_count, res.sections_present,
                                 validation_errors=res.validation_errors, retried=res.retried, truncated=res.truncated)
            self.logger(state).log("panel_shown", "task", cid, level=cond.panel_level, text=res.text,
                                   word_count=res.word_count, sections_present=res.sections_present,
                                   retried=res.retried, truncated=res.truncated, validation_errors=res.validation_errors)
        return self._panel_view(state, cid)

    def panel_poll(self, pid: str) -> dict | None:
        with self.lock(pid):
            state = self.tick(pid)
            if state.page != "task":
                return None
            cid = state.client_order[state.client_index]
            panel = self.refresh_panel(state, cid)
            self.save(state)
            return panel or self._panel_view(state, cid)
