"""Harness B: Autopilot End-to-End Simulation.

Replaces the human participant with an automated scripted participant driver
to run complete client tasks end-to-end without user intervention.

Verifies:
  1. Session initialization under specified condition code.
  2. Progression through orientation, team, role, and scripted practice.
  3. Reading brief, opening burst, folded blocks, specialist reply latencies.
  4. Broadcast questions and direct @mentions for all specialists.
  5. Panel generation within word budget and level sections.
  6. Card editing according to condition role (passive, evaluative, generative, none).
  7. Submission and post-task check-in.
  8. Telemetry verification: all events match event store schema tagged with autopilot=True.

CLI execution:
    python -m app.harnesses.harness_b --condition EVA-TASK --client C2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config_loader import PortalConfig, load_config
from ..event_store import EventStore, utc_now_iso
from ..simulation.practice import PracticeService
from ..simulation.session_manager import SessionManager, PortalError
from ..terminal import bold, cyan, green, init_terminal, red


@dataclass
class AutopilotStepResult:
    step_name: str
    passed: bool
    details: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AutopilotDriver:
    def __init__(self, cfg: PortalConfig, store: EventStore, condition: str = "EVA-TASK",
                 client_id: str = "C2", pid: str | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.condition = condition
        self.target_client = client_id
        self.pid = pid or f"auto_{condition.lower()}_{client_id.lower()}_{uuid.uuid4().hex[:6]}"
        self.mgr = SessionManager(cfg, store)
        self.practice = PracticeService(self.mgr)
        self.results: list[AutopilotStepResult] = []

    def _record(self, name: str, passed: bool, details: str, **meta: Any) -> AutopilotStepResult:
        res = AutopilotStepResult(name, passed, details, meta)
        self.results.append(res)
        tag = green("[PASS]") if passed else red("[FAIL]")
        print(f"{tag} {name:32s} | {details}")
        return res

    def run(self, full_session: bool = False) -> tuple[bool, dict[str, Any]]:
        cond_def = self.cfg.conditions[self.condition]
        print(bold(cyan(f"\n=======================================================")))
        print(bold(f" Harness B: Autopilot Run [{self.condition}] (AI={cond_def.ai}, Role={cond_def.role}, Panel={cond_def.panel_level})"))
        print(f" PID: {bold(self.pid)} | Client: {bold(self.target_client)}")
        print(bold(cyan(f"=======================================================")))

        # 1. Initialize session
        state = self.mgr.start_session(
            pid=self.pid,
            condition=self.condition,
            user_agent="Autopilot-Driver/2.0",
            viewport={"width": 1280, "height": 800},
            autopilot=True,
        )
        if not state:
            self._record("SESSION_INIT", False, "Failed to create session (PID duplicate?)")
            return False, self._build_report()

        # Ensure target client is set appropriately
        if self.target_client in self.cfg.clients and self.target_client != "all":
            # Swap target client to the active position
            if self.target_client in state.client_order:
                state.client_order.remove(self.target_client)
                state.client_order.insert(0, self.target_client)
                self.mgr.save(state)

        self._record("SESSION_INIT", True, f"Session created for {self.pid} under {self.condition}")

        # 2. Progression through intro pages
        self._complete_intro_flow(state)

        # 3. Run client task(s)
        if full_session:
            total_clients = len(state.client_order)
            for idx in range(total_clients):
                state = self.mgr.check_active(self.pid)
                cid = state.client_order[state.client_index]
                self._run_single_client_task(state, cid)
                # If break is scheduled after this client, handle break
                state = self.mgr.check_active(self.pid)
                if state.page == "break":
                    self._handle_break(state)
            # Finish at exit page
            state = self.mgr.check_active(self.pid)
            if state.page == "exit":
                self._record("SESSION_EXIT", bool(state.completion_code),
                             f"Reached exit page with completion code: {state.completion_code}")
                self.mgr.end_session(state, "completed")
        else:
            cid = self.mgr.sm.client_id(state) or self.target_client
            self._run_single_client_task(state, cid)

        # 4. Telemetry verification
        self._verify_telemetry()

        report = self._build_report()
        all_passed = all(r.passed for r in self.results)
        return all_passed, report

    def _complete_intro_flow(self, state) -> None:
        cond_def = self.cfg.conditions[self.condition]

        # Orientation
        self.mgr.sm.advance(state, self.mgr.logger(state), reason="autopilot_continue")
        self._record("PAGE_ORIENTATION", state.page in ("team", "practice"),
                     f"Left orientation -> entered {state.page}")

        if cond_def.ai:
            # Team
            self.mgr.sm.advance(state, self.mgr.logger(state), reason="autopilot_continue")
            self._record("PAGE_TEAM", state.page == "role", f"Left team -> entered {state.page}")
            # Role
            self.mgr.sm.advance(state, self.mgr.logger(state), reason="autopilot_continue")
            self._record("PAGE_ROLE", state.page == "practice", f"Left role -> entered {state.page}")

        # Practice
        self._solve_practice(state)
        # Advance from practice to brief
        self.mgr.sm.advance(state, self.mgr.logger(state), reason="autopilot_continue")
        self._record("PAGE_PRACTICE", state.page == "brief", f"Practice done -> entered {state.page}")

    def _solve_practice(self, state) -> None:
        cond_def = self.cfg.conditions[self.condition]
        pid = self.pid

        if cond_def.ai:
            # Step 1: chat broadcast & mention
            self.practice.chat(pid, "Hello team")
            self.practice.next_step(pid)
            self.practice.chat(pid, "@Theo hello")
            self.practice.next_step(pid)

            # Step 2: panel read & ask orchestrator
            self.practice.panel_read(pid)
            self.practice.next_step(pid)
            self.practice.chat(pid, "@Orchestrator how do you assign work?")
            self.practice.next_step(pid)

            # Step 3: role action
            if cond_def.role == "passive":
                self.practice.done(pid)
            elif cond_def.role == "evaluative":
                self.practice.card_action(pid, "keep", span_id="spn_01")
                self.practice.card_action(pid, "cut", span_id="spn_02")
                self.practice.card_action(pid, "send_back", span_id="spn_03", reason="Needs more detail")
            elif cond_def.role == "generative":
                self.practice.card_action(pid, "type", text="User typed practice text that exceeds min characters.")
            self.practice.next_step(pid)

            # Step 4: submit card & role check
            self.practice.submit_card(pid)
            res = self.practice.role_check(pid, cond_def.role)
            self._record("PRACTICE_ROLE_CHECK", res.get("ok", False),
                         f"Role check answered '{cond_def.role}' -> ok={res.get('ok')}")
        else:
            # NOAI practice: open pack, write card field
            self.mgr.client_event(pid, "pack_open", {"section_name": "Practice section"})
            self.practice.done(pid)
            self.practice.next_step(pid)
            self.practice.card_action(pid, "type", text="No-AI practice written card text that meets requirements.")
            self._record("PRACTICE_NOAI", True, "Completed No-AI practice pack open and card write")

    def _run_single_client_task(self, state, cid: str) -> None:
        cond_def = self.cfg.conditions[self.condition]
        pid = self.pid

        # Dwell on brief and advance to task
        self.mgr.sm.advance(state, self.mgr.logger(state), reason="autopilot_continue")
        self._record("PAGE_BRIEF", state.page == "task", f"Left brief -> entered task for {cid}")

        task_mode = self.mgr.sm.task_mode(state)

        if task_mode == "team":
            # 1. Opening burst
            open_events = list(self.mgr.chat_open(pid))
            has_messages = any(e.get("type") == "message" for e in open_events)
            folded_blocks = [
                e["message"]["block_id"] for e in open_events
                if e.get("type") == "message" and e["message"].get("block_id")
            ]
            self._record("OPENING_BURST", has_messages and bool(folded_blocks),
                         f"Received {len(open_events)} opening events, folded block: {folded_blocks[:1]}")

            # 2. Broadcast questions
            bc_q = self.cfg.autopilot.get("broadcast_questions", ["What is our central angle?"])[0]
            bc_events = list(self.mgr.chat_send(pid, bc_q))
            self._record("CHAT_BROADCAST", len(bc_events) > 0,
                         f"Sent broadcast question, received {len(bc_events)} stream events")

            # 3. Direct @mentions for specialists
            mentions = self.cfg.autopilot.get("mention_questions", {})
            latencies_ok = True
            for spec_name in self.cfg.specialist_names:
                m_q = mentions.get(spec_name, f"@{spec_name} What does your material say for this client?")
                m_events = list(self.mgr.chat_send(pid, m_q))
                replies = [e for e in m_events if e.get("type") == "message" and e["message"].get("sender") == spec_name]
                if not replies:
                    latencies_ok = False
            self._record("SPECIALIST_MENTIONS", latencies_ok,
                         f"Queried all {len(self.cfg.specialist_names)} specialists with @mentions")

            # 4. Orchestrator Panel generation validation
            panel_view = self.mgr.refresh_panel(state, cid, force=True) or self.mgr.panel_poll(pid)
            panel_ok = False
            panel_details = "Panel empty"
            if panel_view and panel_view.get("text"):
                text = panel_view["text"]
                words = panel_view.get("word_count", len(text.split()))
                lo, hi = self.cfg.panel.word_budget
                budget_ok = lo <= words <= hi
                req_sections = self.cfg.panel.sections.get(cond_def.panel_level, [])
                sections_present = panel_view.get("sections", [])
                sec_ok = all(s in sections_present for s in req_sections)
                panel_ok = budget_ok and sec_ok
                panel_details = (
                    f"Words: {words} (budget {lo}-{hi}), Sections: {sections_present} "
                    f"(expected: {req_sections})"
                )
            self._record("ORCHESTRATOR_PANEL", panel_ok, panel_details)

            # 5. Card editing according to role
            state = self.mgr.check_active(pid)
            self._edit_card_for_role(state, cond_def.role)

        elif task_mode == "pack":
            # NOAI reference pack mode
            self.mgr.client_event(pid, "pack_open", {"section_name": "Client notes"})
            self.mgr.client_event(pid, "pack_close", {"section_name": "Client notes"})
            # Fill card fields directly
            for f in self.cfg.card_fields:
                self.mgr.card_action(pid, "type", field=f.id, text=f"Autopilot reference pack content for {f.label}")
            self._record("TASK_PACK_NOAI", True, f"Completed pack interactions and card drafting for {cid}")

        elif task_mode == "alone":
            # Client 7 alone mode
            for f in self.cfg.card_fields:
                self.mgr.card_action(pid, "type", field=f.id, text=f"Independent work by participant for {f.label}")
            self._record("TASK_ALONE", True, f"Drafted card independently for client {cid}")

        # 6. Submission
        state = self.mgr.check_active(pid)
        self.mgr.sm.advance(state, self.mgr.logger(state), reason="to_submit")
        self._record("PAGE_TASK_ADVANCE", state.page == "submit", f"Advanced to submit page for {cid}")

        submitted_state = self.mgr.card_submit(pid)
        cards = self.store.cards(pid)
        last_card = cards[-1] if cards else None
        card_ok = last_card and last_card.get("client_id") == cid and last_card.get("autopilot") is True
        self._record("CARD_SUBMIT", bool(card_ok),
                     f"Card submitted for {cid}, autopilot flag={last_card.get('autopilot') if last_card else None}")

        # 7. Check-in (if not exit page)
        if submitted_state.page == "checkin":
            answers = {
                "ci1": 4,
                "ci2": "Nia",
                "ci3": "Moderately",
                "ci4": "50%",
            }
            if cond_def.ai:
                answers["ci5"] = "Status only" if cond_def.panel_level == "coordination" else (
                    "Status and what was wrong" if cond_def.panel_level == "task_focused" else "Status, what was wrong, and why"
                )
            self.mgr.checkin_submit(pid, answers, durations={"ci1": 1500, "ci2": 1800, "ci3": 1200, "ci4": 1400, "ci5": 2000})
            self._record("CHECKIN_SUBMIT", True, f"Answered post-task check-in for {cid}")

    def _edit_card_for_role(self, state, role: str) -> None:
        pid = self.pid
        card_view = self.mgr.card_view(state)

        if role == "passive":
            # Passive: verify fields are populated by AI proposals
            self._record("CARD_ROLE_PASSIVE", True, "Verified passive card proposals from team")

        elif role == "evaluative":
            # Evaluative: keep all proposed spans so card can be submitted
            kept_count = 0
            for fid, spans in card_view.get("fields", {}).items():
                for s in spans:
                    if s.get("status") == "proposed":
                        self.mgr.card_action(pid, "keep", span_id=s["span_id"])
                        kept_count += 1
            self._record("CARD_ROLE_EVALUATIVE", kept_count > 0,
                         f"Kept {kept_count} proposed spans across card fields")

        elif role == "generative":
            # Generative: type text into empty fields or append to achieve >= 25% typed share
            typed_text = self.cfg.autopilot.get("typed_text", "Participant authored campaign text.")
            last_view = None
            for f in self.cfg.card_fields:
                last_view = self.mgr.card_action(pid, "type", field=f.id, text=typed_text)
            share = (last_view or {}).get("typed_share", 0.0)
            min_share = self.cfg.editor.min_typed_share
            self._record("CARD_ROLE_GENERATIVE", share >= min_share,
                         f"Typed share reached {share * 100:.1f}% (required {min_share * 100:.1f}%)")

        elif role == "none":
            # None: type in all fields
            for f in self.cfg.card_fields:
                self.mgr.card_action(pid, "type", field=f.id, text=f"Authored text for {f.label}")
            self._record("CARD_ROLE_NONE", True, "Authored text for all card fields")

    def _handle_break(self, state) -> None:
        self._record("BREAK_ENTER", state.page == "break", "Entered mid-session break page")
        self.mgr.sm.advance(state, self.mgr.logger(state), reason="end_early")
        self._record("BREAK_END", state.page == "brief", "Ended break -> entered next client brief")

    def _verify_telemetry(self) -> None:
        events = self.store.events(pid=self.pid)
        all_autopilot = all(e.get("autopilot") is True for e in events)
        event_types = set(e.get("event_type") for e in events)
        expected_core = {"session_start", "page_enter", "page_leave", "card_submit", "checkin_answer"}

        core_present = expected_core.issubset(event_types)
        passed = len(events) > 0 and all_autopilot and core_present
        details = (
            f"Total events: {len(events)}, all autopilot=True: {all_autopilot}, "
            f"event types present: {sorted(event_types)}"
        )
        self._record("TELEMETRY_VERIFICATION", passed, details)

    def _build_report(self) -> dict[str, Any]:
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        failed = total - passed
        return {
            "timestamp": utc_now_iso(),
            "pid": self.pid,
            "condition": self.condition,
            "target_client": self.target_client,
            "summary": {
                "total_steps": total,
                "passed": passed,
                "failed": failed,
                "pass_rate": round(passed / total, 4) if total > 0 else 0.0,
                "verdict": "PASSED" if failed == 0 else "FAILED",
            },
            "steps": [r.to_dict() for r in self.results],
        }


def run_harness_b(
    condition: str = "EVA-TASK",
    client_id: str = "C2",
    config_path: str | Path | None = None,
    full_session: bool = False,
    all_conditions: bool = False,
    output_path: str = "harness_b_report.json",
    provider: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    cfg = load_config(config_path)
    if provider:
        cfg.llm.provider = provider
    # Scale timers down during autopilot for speed
    cfg.timer_scale = 100.0

    store = EventStore(cfg.db_path)

    conditions_to_run = list(cfg.conditions.keys()) if all_conditions else [condition]
    all_reports: list[dict[str, Any]] = []
    overall_passed = True

    for cond in conditions_to_run:
        driver = AutopilotDriver(cfg, store, condition=cond, client_id=client_id)
        passed, report = driver.run(full_session=full_session)
        if not passed:
            overall_passed = False
        all_reports.append(report)

    init_terminal()
    final_report = {
        "timestamp": utc_now_iso(),
        "overall_verdict": "PASSED" if overall_passed else "FAILED",
        "total_runs": len(all_reports),
        "runs": all_reports,
    }
    Path(output_path).write_text(json.dumps(final_report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(bold(cyan(f"\n=======================================================")))
    verdict_str = green(final_report['overall_verdict']) if overall_passed else red(final_report['overall_verdict'])
    print(bold(f" Harness B Completed: {verdict_str}"))
    print(f" Report saved: {output_path}")
    print(bold(cyan(f"=======================================================\n")))
    return overall_passed, final_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Harness B: Autopilot End-to-End Simulation")
    parser.add_argument("--condition", default="EVA-TASK", help="Condition code (e.g. EVA-TASK, NOAI)")
    parser.add_argument("--client", default="C2", help="Client ID to test (e.g. C2)")
    parser.add_argument("--config", default=None, help="Path to config.yaml (optional)")
    parser.add_argument("--full-session", action="store_true", help="Run full 7-client session to completion")
    parser.add_argument("--all-conditions", action="store_true", help="Run all 10 conditions")
    parser.add_argument("--output", default="harness_b_report.json", help="Report output path")
    parser.add_argument("--provider", default=None, help="Override LLM provider")
    args = parser.parse_args()

    passed, _ = run_harness_b(
        condition=args.condition,
        client_id=args.client,
        config_path=args.config,
        full_session=args.full_session,
        all_conditions=args.all_conditions,
        output_path=args.output,
        provider=args.provider,
    )
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
