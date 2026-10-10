"""
Practice (four scripted exercises, no model calls).

Steps come from ``practice.steps_ai`` / ``practice.steps_noai`` in config. Each step must be
completed before the next unlocks. Replies and panel text are scripted; the practice card uses the
same span/provenance engine as the real card, so the participant rehearses their own role.
Exercise 4 ends with a role check; a wrong answer sends the participant back to the role page.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..state_machine import SessionState
from .card_provenance import Card, CardError
from .chat_router import format_options, parse_message
from .session_manager import PortalError, SessionManager

PRACTICE_KEY = "practice"


class PracticeService:
    def __init__(self, mgr: SessionManager) -> None:
        self.m = mgr
        self.cfg = mgr.cfg

    # ---- definitions -----------------------------------------------------------
    def steps(self, state: SessionState) -> list[dict]:
        key = "steps_ai" if self.m.sm.condition(state).ai else "steps_noai"
        return self.cfg.practice[key]

    def current(self, state: SessionState) -> dict:
        steps = self.steps(state)
        return steps[min(state.practice_step, len(steps) - 1)]

    def _flags(self, state: SessionState) -> dict:
        return state.practice_flags

    def _require(self, pid: str) -> SessionState:
        state = self.m.check_active(pid)
        if state.page != "practice":
            raise PortalError("Practice is not open.", 409)
        return state

    # ---- practice card ------------------------------------------------------------
    def card(self, state: SessionState) -> Card:
        cond = self.m.sm.condition(state)
        if PRACTICE_KEY in state.cards:
            return Card.from_json(state.cards[PRACTICE_KEY], self.cfg.editor.send_back_needs_reason, self.cfg.editor.min_typed_share)
        role = cond.role
        card = Card(["practice"], role, self.cfg.editor.send_back_needs_reason, self.cfg.editor.min_typed_share)
        if role in ("passive", "evaluative"):
            for item in self.cfg.practice["card"]:
                card.add_ai_text("practice", item["text"], item["author"])
        return card

    def view(self, state: SessionState) -> dict:
        card = self.card(state)
        d = card.to_json(full=False)
        d["can_submit"], d["blocked_reason"] = True, ""
        d["send_back_needs_reason"] = self.cfg.editor.send_back_needs_reason
        return d

    def _save_card(self, state: SessionState, card: Card) -> None:
        state.cards[PRACTICE_KEY] = card.to_json()

    # ---- step bookkeeping -------------------------------------------------------------
    def _attempt(self, state: SessionState) -> int:
        sid = self.current(state)["id"]
        state.practice_attempts[sid] = state.practice_attempts.get(sid, 0) + 1
        return state.practice_attempts[sid]

    def _complete(self, state: SessionState) -> None:
        step = self.current(state)
        sid = step["id"]
        flags = self._flags(state)
        if flags.get(f"done:{sid}"):
            return
        flags[f"done:{sid}"] = True
        if state.practice_step >= len(self.steps(state)) - 1 and step["kind"] != "submit_and_check":
            flags["practice_complete"] = True
        attempts = max(1, state.practice_attempts.get(sid, 0))
        started = state.practice_step_started or state.page_entered_at
        self.m.logger(state).log("practice_step", "practice", None, step_id=sid, attempts=attempts,
                                 duration_ms=int((self.m.clock() - started) * 1000))

    def state_view(self, pid: str) -> dict:
        state = self.m.check_active(pid)
        steps = self.steps(state)
        step = self.current(state)
        flags = self._flags(state)
        cond = self.m.sm.condition(state)
        return {
            "step_index": state.practice_step, "n_steps": len(steps), "step": step,
            "step_done": bool(flags.get(f"done:{step['id']}")), "complete": bool(flags.get("practice_complete")),
            "chat": flags.get("chat", []), "card": self.view(state), "role": cond.role,
            "panel": self.cfg.practice["panel_text"].get(cond.panel_level) if cond.ai else None,
            "panel_visible": bool(flags.get("panel_visible")),
            "submitted": bool(flags.get("card_submitted")),
        }

    # ---- actions ------------------------------------------------------------------------------
    def chat(self, pid: str, text: str) -> dict:
        with self.m.lock(pid):
            state = self._require(pid)
            step = self.current(state)
            if step["kind"] not in ("chat_broadcast", "chat_mention", "chat_orchestrator"):
                raise PortalError("Chat is not part of this step.", 409)
            if self._flags(state).get(f"done:{step['id']}"):
                raise PortalError("This step is already done.", 409)
            route = parse_message(text, self.cfg.all_agent_names)
            if route.kind == "error":
                return {"ok": False, "warning": self.cfg.text("task.unknown_mention", name=route.unknown,
                                                               options=format_options(route.suggestions))}
            attempt = self._attempt(state)
            kind = step["kind"]
            orch = self.cfg.orchestrator_name
            ok = ((kind == "chat_broadcast" and route.kind == "broadcast") or
                  (kind == "chat_mention" and route.kind == "directed" and orch not in route.addressees) or
                  (kind == "chat_orchestrator" and route.kind == "directed" and orch in route.addressees))
            self.m.logger(state).log("message_sent", "practice", None, text=text, addressees=route.addressees or ["everyone"],
                                     mention_used=route.mention_used, char_count=len(text), practice=True)
            msgs = [{"sender": "Participant", "text": text}]
            if not ok:
                hint = {"chat_broadcast": "For this step, send a message without an @.",
                        "chat_mention": "For this step, start your message with @ and a specialist's name.",
                        "chat_orchestrator": "For this step, start your message with @Orchestrator."}[kind]
                self._flags(state).setdefault("chat", []).extend(msgs)
                self.m.save(state)
                return {"ok": False, "warning": hint, "messages": msgs, "attempts": attempt}
            sr = self.cfg.practice["scripted_replies"]
            replies = sr["team_broadcast"] if kind == "chat_broadcast" else [sr["mention"] if kind == "chat_mention" else sr["orchestrator"]]
            for r in replies:
                sender, _, body = r.partition(": ")
                msgs.append({"sender": sender, "text": body})
            self._flags(state).setdefault("chat", []).extend(msgs)
            self._complete(state)
            self.m.save(state)
            return {"ok": True, "messages": msgs}

    def panel_read(self, pid: str) -> dict:
        with self.m.lock(pid):
            state = self._require(pid)
            self._flags(state)["panel_visible"] = True
            if self.current(state)["kind"] == "panel_read":
                self._attempt(state)
                self._complete(state)
            self.m.save(state)
            return self.state_view(pid)

    def card_action(self, pid: str, action: str, span_id: str = "", reason: str = "", text: str = "") -> dict:
        with self.m.lock(pid):
            state = self._require(pid)
            step = self.current(state)
            card = self.card(state)
            cid = None
            try:
                if action in ("keep", "cut", "send_back"):
                    fid, span, evt = card.evaluate(span_id, action, reason)
                    self.m.logger(state).log("card_action", "practice", cid, practice=True, **evt)
                    seen = self._flags(state).setdefault("eval_actions", [])
                    if action not in seen:
                        seen.append(action)
                    if action == "send_back":
                        base = next((c["text"] for c in self.cfg.practice["card"] if c["author"] == span.author), span.text)
                        card.add_ai_text(fid, "Revised after your note: " + base, span.author, replaces=span.span_id)
                elif action == "type":
                    evts = card.set_field_text("practice", text, self.cfg.editor.max_span_chars)
                    for evt in evts:
                        self.m.logger(state).log("card_action", "practice", cid, practice=True, **evt)
                else:
                    raise PortalError("unknown card action")
            except CardError as exc:
                raise PortalError(str(exc), 400)
            self._save_card(state, card)
            kind = step["kind"]
            if kind == "role_action":
                role = self.m.sm.condition(state).role
                if role == "evaluative" and {"keep", "cut", "send_back"} <= set(self._flags(state).get("eval_actions", [])):
                    self._attempt(state)
                    self._complete(state)
                elif role == "generative" and len(card.full_text()["practice"].strip()) >= self.cfg.practice["generative_min_chars"]:
                    self._attempt(state)
                    self._complete(state)
            elif kind == "card_write" and len(card.full_text()["practice"].strip()) >= self.cfg.practice["generative_min_chars"]:
                self._attempt(state)
                self._complete(state)
            self.m.save(state)
            return self.state_view(pid)

    def done(self, pid: str) -> dict:
        """Participant presses 'Done' for steps that need no input (passive role action, pack open)."""
        with self.m.lock(pid):
            state = self._require(pid)
            step = self.current(state)
            kind = step["kind"]
            role = self.m.sm.condition(state).role
            if kind == "role_action" and role == "passive":
                self._attempt(state)
                self._complete(state)
            elif kind == "pack_open":
                self._attempt(state)
                if self._flags(state).get("pack_opened"):
                    self._complete(state)
                else:
                    self.m.save(state)
                    raise PortalError("Open one pack section first.", 409)
            else:
                raise PortalError("This step needs your input first.", 409)
            self.m.save(state)
            return self.state_view(pid)

    def next_step(self, pid: str) -> dict:
        with self.m.lock(pid):
            state = self._require(pid)
            step = self.current(state)
            if not self._flags(state).get(f"done:{step['id']}"):
                raise PortalError("Finish this step first.", 409)
            if state.practice_step + 1 >= len(self.steps(state)):
                raise PortalError("This is the last step.", 409)
            state.practice_step += 1
            state.practice_step_started = self.m.clock()
            self.m.save(state)
            return self.state_view(pid)

    def submit_card(self, pid: str) -> dict:
        with self.m.lock(pid):
            state = self._require(pid)
            if self.current(state)["kind"] != "submit_and_check":
                raise PortalError("Not the submit step.", 409)
            self._flags(state)["card_submitted"] = True
            self.m.save(state)
            return self.state_view(pid)

    def role_check(self, pid: str, answer: str) -> dict:
        with self.m.lock(pid):
            state = self._require(pid)
            step = self.current(state)
            if step["kind"] != "submit_and_check" or not self._flags(state).get("card_submitted"):
                raise PortalError("Submit the practice card first.", 409)
            role = self.m.sm.condition(state).role
            self._attempt(state)
            correct = answer == role
            self.m.logger(state).log("role_check", "practice", None, answer=answer, correct=correct, sent_back=not correct)
            if correct:
                self._complete(state)
                self._flags(state)["practice_complete"] = True
                self.m.save(state)
                return {"ok": True, "message": self.cfg.text("practice.role_check_right")}
            # wrong: back to the role page; the practice card submission must be repeated
            log = self.m.logger(state)
            log.log("page_leave", "practice", None, dwell_time_ms=int((self.m.clock() - state.page_entered_at) * 1000), reason="role_check_failed")
            state.page = "role"
            state.page_entered_at = self.m.clock()
            self._flags(state)["card_submitted"] = False
            state.role_check_failed = True
            log.log("page_enter", "role", None, reason="role_check_failed")
            self.m.save(state)
            return {"ok": False, "message": self.cfg.text("practice.role_check_wrong"), "sent_back": True}
