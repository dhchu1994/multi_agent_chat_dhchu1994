"""
Session state, forward-only page progression and authoritative server timers.

All timers are deadlines persisted in the session record (wall-clock epoch seconds), so a
browser reload, a second tab or a server restart can never reset or extend them. The flow is:

    entry -> orientation -> [team -> role]* -> practice
          -> (brief -> task -> submit -> checkin) x 6, with a break after the third
          -> brief -> task -> submit (client 7, alone) -> exit
    * skipped in the NOAI condition

Progression is strictly forward; there is no jump-back.
"""

from __future__ import annotations

import hashlib
import json
import hmac
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .config_loader import PortalConfig
from .event_store import EventStore, SessionLogger, utc_now_iso

PAGES = ["entry", "orientation", "team", "role", "practice", "brief", "task", "submit", "checkin", "break", "exit"]


@dataclass
class SessionState:
    pid: str
    condition: str
    autopilot: bool = False
    page: str = "orientation"
    client_order: list[str] = field(default_factory=list)       # seven ids, last one fixed
    specialist_order: list[str] = field(default_factory=list)
    client_index: int = 0
    page_entered_at: float = 0.0                                 # epoch seconds
    task_deadline: float | None = None
    break_deadline: float | None = None
    task_expired_logged: bool = False
    practice_step: int = 0
    practice_attempts: dict[str, int] = field(default_factory=dict)
    practice_step_started: float = 0.0
    role_check_failed: bool = False
    practice_done_steps: list[str] = field(default_factory=list)
    practice_flags: dict[str, Any] = field(default_factory=dict)
    submitted: list[str] = field(default_factory=list)
    checkin_started: float = 0.0
    completion_code: str | None = None
    cards: dict[str, dict] = field(default_factory=dict)         # client_id (or "practice") -> card json
    chat: dict[str, dict] = field(default_factory=dict)          # client_id -> chat bookkeeping
    started_at_epoch: float = 0.0
    viewport: dict | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "SessionState":
        names = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in names})


class StateMachine:
    def __init__(self, cfg: PortalConfig, store: EventStore, clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.store, self.clock = cfg, store, clock

    # ---- helpers -----------------------------------------------------------
    def condition(self, state: SessionState):
        return self.cfg.conditions[state.condition]

    def client_id(self, state: SessionState) -> str | None:
        if state.page in ("brief", "task", "submit", "checkin") or (state.page == "break"):
            return state.client_order[min(state.client_index, len(state.client_order) - 1)]
        return None

    def is_last_client(self, state: SessionState) -> bool:
        return state.client_index >= len(state.client_order) - 1

    def task_mode(self, state: SessionState) -> str:
        """team | pack | alone: what the middle column of the task page shows."""
        cond = self.condition(state)
        if not cond.ai:
            return "pack"
        if self.is_last_client(state):
            return "pack" if self.cfg.seventh_client_pack else "alone"
        return "team"

    def card_role(self, state: SessionState) -> str:
        """Editor rules: the condition's role with the team; the participant writes it otherwise."""
        cond = self.condition(state)
        if cond.ai and not self.is_last_client(state):
            return cond.role
        return "none"

    def min_seconds(self, page: str) -> float:
        t = self.cfg.timers
        base = {"orientation": t.orientation_min_seconds, "team": t.team_min_seconds,
                "role": t.role_min_seconds, "brief": t.brief_min_seconds}.get(page, 0.0)
        return self.cfg.scaled(base)

    def unlock_remaining(self, state: SessionState) -> float:
        need = self.min_seconds(state.page)
        return max(0.0, need - (self.clock() - state.page_entered_at))

    def task_remaining(self, state: SessionState) -> float:
        if state.task_deadline is None:
            return 0.0
        return max(0.0, state.task_deadline - self.clock())

    def break_remaining(self, state: SessionState) -> float:
        if state.break_deadline is None:
            return 0.0
        return max(0.0, state.break_deadline - self.clock())

    # ---- creation / persistence -----------------------------------------------
    def new_state(self, pid: str, condition: str, autopilot: bool = False, rng: random.Random | None = None) -> SessionState:
        rng = rng or random.Random(uuid.uuid4().int)
        order = list(self.cfg.randomised_clients)
        rng.shuffle(order)
        order.append(self.cfg.fixed_last_client)
        spec = self.cfg.specialist_names
        spec = rng.sample(spec, len(spec))
        now = self.clock()
        return SessionState(pid=pid, condition=condition, autopilot=autopilot, page="orientation",
                            client_order=order, specialist_order=spec, page_entered_at=now, started_at_epoch=now)

    def completion_code_for(self, pid: str) -> str:
        secret = self.cfg.qualtrics.get("completion_code_secret", "")
        digest = hmac.new(secret.encode(), pid.encode(), hashlib.sha256).hexdigest()[:8].upper()
        return f"{self.cfg.qualtrics.get('completion_code_prefix', 'IP')}-{digest}"

    def save(self, state: SessionState) -> None:
        self.store.update_session(state.pid, state=json.dumps(state.to_json()),
                                  last_activity=utc_now_iso())

    def load(self, pid: str) -> SessionState | None:
        row = self.store.get_session(pid)
        if not row:
            return None
        return SessionState.from_json(json.loads(row["state"]))

    # ---- flow -----------------------------------------------------------------
    def _next_page(self, state: SessionState) -> str:
        cond = self.condition(state)
        p = state.page
        if p == "orientation":
            return "team" if cond.ai else "practice"
        if p == "team":
            return "role"
        if p == "role":
            return "practice"
        if p == "practice":
            return "brief"
        if p == "brief":
            return "task"
        if p == "task":
            return "submit"
        if p == "submit":
            return "exit" if self.is_last_client(state) else "checkin"
        if p == "checkin":
            if state.client_index + 1 == self.cfg.break_after_position:
                return "break"
            return "brief"
        if p == "break":
            return "brief"
        raise ValueError(f"no page after {p}")

    def can_advance(self, state: SessionState) -> tuple[bool, str]:
        if state.page in ("orientation", "team", "role", "brief"):
            if self.unlock_remaining(state) > 0:
                return False, "locked"
        if state.page == "task" and self.task_remaining(state) > 0:
            return True, ""      # the participant may go to submission early
        return True, ""

    def advance(self, state: SessionState, log: SessionLogger, reason: str = "continue") -> SessionState:
        """Move to the next page, logging page_leave / page_enter. Caller must have validated."""
        now = self.clock()
        leaving, cid = state.page, self.client_id(state)
        log.log("page_leave", leaving, cid, dwell_time_ms=int((now - state.page_entered_at) * 1000), reason=reason)
        new = self._next_page(state)
        # per-page side effects
        if (leaving == "checkin" and new == "brief") or leaving == "break":
            state.client_index += 1
        if leaving == "submit":
            cid_done = state.client_order[state.client_index]
            if cid_done not in state.submitted:
                state.submitted.append(cid_done)
        state.page = new
        state.page_entered_at = now
        ncid = self.client_id(state)
        if new == "task":
            state.task_deadline = now + self.cfg.scaled(self.cfg.timers.task_seconds)
            state.task_expired_logged = False
        if new == "break":
            state.break_deadline = now + self.cfg.scaled(self.cfg.timers.break_seconds)
            log.log("break_start", "break", ncid)
        if new == "checkin":
            state.checkin_started = now
        if new == "exit":
            state.completion_code = self.completion_code_for(state.pid)
        log.log("page_enter", new, ncid, **({"client_drawn": ncid} if new == "brief" else {}))
        self.save(state)
        return state
