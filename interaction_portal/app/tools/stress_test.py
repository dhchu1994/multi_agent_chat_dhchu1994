"""Concurrent Participant Simulation Stress Test Tool.

Simulates N concurrent participants (e.g. 30, 100, or 300) progressing through
the Interaction Portal simultaneously with configurable simulated LLM latency,
verifying thread-pool scaling, SQLite WAL throughput, and response times.

Usage:
    python -m app.tools.stress_test --participants 30 --llm-delay-ms 500
    python -m app.tools.stress_test --participants 100 --llm-delay-ms 500
    python -m app.tools.stress_test --participants 300 --llm-delay-ms 1000
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..agents.base import LLMResult
from ..agents.mock_llm import MockClient
from ..config_loader import PortalConfig, load_config
from ..event_store import EventStore
from ..simulation.practice import PracticeService
from ..simulation.session_manager import SessionManager
from .verify_db import verify_database


class DelayedMockClient(MockClient):
    """Wraps MockClient with realistic simulated network latency per LLM call."""

    def __init__(self, cfg: PortalConfig, delay_ms: int = 500, jitter_pct: float = 0.25) -> None:
        super().__init__(cfg)
        self.delay_sec = max(0.0, delay_ms / 1000.0)
        self.jitter_pct = jitter_pct
        self.model_version = f"mock-stress-{delay_ms}ms"

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int,
                 json_mode: bool = True, meta: dict[str, Any] | None = None) -> LLMResult:
        t0 = time.monotonic()
        if self.delay_sec > 0:
            factor = 1.0 + random.uniform(-self.jitter_pct, self.jitter_pct)
            time.sleep(max(0.001, self.delay_sec * factor))
        res = super().complete(
            system, user, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode, meta=meta
        )
        total_ms = int((time.monotonic() - t0) * 1000)
        first_ms = max(1, total_ms // 3)
        return LLMResult(
            text=res.text,
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            first_token_ms=first_ms,
            total_ms=total_ms,
        )


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def simulate_one_participant(
    idx: int,
    condition: str,
    mgr: SessionManager,
    practice: PracticeService,
) -> dict[str, Any]:
    pid = f"stress_{idx:04d}_{condition.lower()}_{uuid.uuid4().hex[:6]}"
    fast_latencies_ms: list[float] = []
    llm_latencies_ms: list[float] = []
    t_start = time.monotonic()

    try:
        t0 = time.monotonic()
        state = mgr.start_session(
            pid=pid,
            condition=condition,
            user_agent="StressTest/2.0 (Macintosh; Intel Mac OS X)",
            viewport={"width": 1440, "height": 900},
            autopilot=True,
        )
        if state is None:
            return {"pid": pid, "ok": False, "error": "start_session returned None"}
        mgr.record_viewport(pid, {"width": 1440, "height": 900, "maxTouchPoints": 0, "pointer_coarse": False, "is_mobile": False})
        fast_latencies_ms.append((time.monotonic() - t0) * 1000.0)

        cond_def = mgr.cfg.conditions[condition]

        t0 = time.monotonic()
        mgr.advance(pid)
        if cond_def.ai:
            mgr.advance(pid)
            mgr.advance(pid)
        fast_latencies_ms.append((time.monotonic() - t0) * 1000.0)

        t0 = time.monotonic()
        if cond_def.ai:
            practice.chat(pid, "Hello team")
            practice.next_step(pid)
            practice.chat(pid, "@Theo hello")
            practice.next_step(pid)
            practice.panel_read(pid)
            practice.next_step(pid)
            practice.chat(pid, "@Orchestrator how do you assign work?")
            practice.next_step(pid)
            if cond_def.role == "passive":
                practice.done(pid)
            elif cond_def.role == "evaluative":
                practice.card_action(pid, "keep", span_id="spn_01")
                practice.card_action(pid, "cut", span_id="spn_02")
                practice.card_action(pid, "send_back", span_id="spn_03", reason="Needs detail")
            elif cond_def.role == "generative":
                practice.card_action(pid, "type", text="Participant practice text that meets minimum character length.")
            practice.next_step(pid)
            practice.submit_card(pid)
            practice.role_check(pid, cond_def.role)
        else:
            mgr.client_events_batch(pid, [{"type": "pack_open", "metadata": {"section_name": "Practice section"}}])
            practice.done(pid)
            practice.next_step(pid)
            practice.card_action(pid, "type", text="Participant practice text for No-AI condition.")
        mgr.advance(pid)  # Practice -> Brief
        mgr.advance(pid)  # Brief -> Task
        fast_latencies_ms.append((time.monotonic() - t0) * 1000.0)

        state = mgr.check_active(pid)
        mode = mgr.sm.task_mode(state)

        if mode == "team":
            t0 = time.monotonic()
            list(mgr.chat_open(pid))
            llm_latencies_ms.append((time.monotonic() - t0) * 1000.0)

            t0 = time.monotonic()
            list(mgr.chat_send(pid, "What is our central campaign angle?"))
            llm_latencies_ms.append((time.monotonic() - t0) * 1000.0)

            t0 = time.monotonic()
            list(mgr.chat_send(pid, "@Nia What unstated priority or hidden client notes do you have?"))
            llm_latencies_ms.append((time.monotonic() - t0) * 1000.0)

            t0 = time.monotonic()
            mgr.panel_poll(pid)
            llm_latencies_ms.append((time.monotonic() - t0) * 1000.0)

        t0 = time.monotonic()
        mgr.tick(pid)
        mgr.client_events_batch(pid, [
            {"type": "window_focus", "metadata": {}},
            {"type": "scroll_sample", "metadata": {"scroll_top": 120, "scroll_height": 800, "viewport_height": 600}},
        ])
        fast_latencies_ms.append((time.monotonic() - t0) * 1000.0)

        t0 = time.monotonic()
        state = mgr.check_active(pid)
        card_view = mgr.card_view(state)
        if cond_def.role == "evaluative" and mode == "team":
            for fid, spans in card_view.get("fields", {}).items():
                for s in spans:
                    if s.get("status") == "proposed":
                        mgr.card_action(pid, "keep", span_id=s["span_id"])
        elif cond_def.role in ("generative", "none"):
            for f in mgr.cfg.card_fields:
                mgr.card_action(pid, "type", field=f.id, text=f"Stress test campaign text for {f.label} written by {pid}.")
        fast_latencies_ms.append((time.monotonic() - t0) * 1000.0)

        t0 = time.monotonic()
        mgr.go_to_submission(pid)
        st_after = mgr.card_submit(pid)
        if st_after.page == "checkin":
            answers = {"ci1": 4, "ci2": "Nia", "ci3": "Moderately", "ci4": "50%"}
            if cond_def.ai:
                answers["ci5"] = "Status only"
            mgr.checkin_submit(pid, answers, durations={"ci1": 1000, "ci2": 1000, "ci3": 1000, "ci4": 1000, "ci5": 1000})
        fast_latencies_ms.append((time.monotonic() - t0) * 1000.0)

        return {
            "pid": pid,
            "ok": True,
            "total_s": time.monotonic() - t_start,
            "fast_ms": fast_latencies_ms,
            "llm_ms": llm_latencies_ms,
        }
    except Exception as exc:
        return {"pid": pid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_stress_test(
    participants: int = 50,
    llm_delay_ms: int = 300,
    db_path: str | None = None,
) -> bool:
    cfg = load_config()
    cfg.timer_scale = 10000.0
    cfg.max_concurrent_participants = max(cfg.max_concurrent_participants, participants)

    temp_dir = None
    if db_path:
        target_db = Path(db_path)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="portal_stress_")
        target_db = Path(temp_dir.name) / "stress_portal.sqlite3"

    print("=================================================================")
    print(f" Interaction Portal Stress Test: {participants} Concurrent Participants")
    print(f" Simulated LLM Latency: {llm_delay_ms} ms (+/-25%) | DB: {target_db}")
    print("=================================================================")

    store = EventStore(target_db)
    llm = DelayedMockClient(cfg, delay_ms=llm_delay_ms)
    mgr = SessionManager(cfg, store, llm=llm)
    practice = PracticeService(mgr)

    conditions = list(cfg.conditions.keys())
    t_wall_start = time.monotonic()

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(512, max(32, participants))) as pool:
        futs = [
            pool.submit(simulate_one_participant, i, conditions[i % len(conditions)], mgr, practice)
            for i in range(participants)
        ]
        for fut in as_completed(futs):
            results.append(fut.result())

    wall_s = time.monotonic() - t_wall_start
    succeeded = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]

    all_fast = sorted(ms for r in succeeded for ms in r.get("fast_ms", []))
    all_llm = sorted(ms for r in succeeded for ms in r.get("llm_ms", []))

    all_events = store.events()
    queue_waits = sorted(
        int(e["metadata"].get("queue_wait_ms", 0))
        for e in all_events
        if e["event_type"] == "agent_reply" and "queue_wait_ms" in e["metadata"]
    )

    print(f"\n--- Stress Test Results ({wall_s:.2f}s total wall time) ---")
    print(f"  Participants Completed: {len(succeeded)} / {participants} ({len(succeeded) / max(1, participants) * 100:.1f}%)")
    print(f"  Participants Failed:    {len(failed)}")
    if failed:
        for f in failed[:5]:
            print(f"    - {f['pid']}: {f.get('error')}")

    if all_fast:
        print(f"  Fast Action Latency (DB/State/Card): p50={_percentile(all_fast, 50):.1f}ms | p95={_percentile(all_fast, 95):.1f}ms | p99={_percentile(all_fast, 99):.1f}ms")
    if all_llm:
        print(f"  LLM Turn Latency (Multi-Agent):      p50={_percentile(all_llm, 50):.1f}ms | p95={_percentile(all_llm, 95):.1f}ms | p99={_percentile(all_llm, 99):.1f}ms")
    if queue_waits:
        print(f"  Thread Pool Queue Wait (ms):         p50={_percentile(queue_waits, 50):.1f}ms | p95={_percentile(queue_waits, 95):.1f}ms | max={max(queue_waits)}ms")

    print(f"  Total Events Logged:                 {len(all_events)} ({len(all_events) / max(0.001, wall_s):.1f} events/sec)")
    print(f"  Total Cards Submitted:               {len(store.cards())}")

    db_ok = verify_database(target_db)
    if temp_dir is not None:
        temp_dir.cleanup()

    passed = (len(failed) == 0) and db_ok
    print("=================================================================")
    print(" Stress Test Verdict:", "PASSED" if passed else "FAILED")
    print("=================================================================")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description="Run concurrent participant stress test.")
    parser.add_argument("--participants", type=int, default=50, help="Number of concurrent virtual participants (e.g. 30, 100, 300)")
    parser.add_argument("--llm-delay-ms", type=int, default=300, help="Simulated LLM network delay in milliseconds per call")
    parser.add_argument("--db", type=str, default=None, help="Optional database path (defaults to temporary file)")
    args = parser.parse_args()

    ok = run_stress_test(participants=args.participants, llm_delay_ms=args.llm_delay_ms, db_path=args.db)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
