"""
Interaction Portal: FastAPI application.

Run (standalone test mode, no Qualtrics, no API key):

    cd interaction_portal
    PORTAL_STANDALONE=1 python -m app.main

or  uvicorn app.main:create_app --factory   (after `cd interaction_portal`)

Routes are thin: every rule lives in ``SessionManager`` / ``PracticeService`` / ``StateMachine``.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from .agents.pii import scan_pii
from .config_loader import PortalConfig, load_config
from .event_store import EventStore
from .simulation.practice import PracticeService
from .simulation.session_manager import PortalError, SessionManager

HERE = Path(__file__).resolve().parent
PID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MOBILE_RE = re.compile(r"Mobi|Android|iPhone|iPad|iPod|Tablet|Silk|Kindle|webOS|BlackBerry|Windows Phone", re.I)
COOKIE = "portal_session"
APP_START_MONO = time.monotonic()


def create_app(cfg: PortalConfig | None = None, store: EventStore | None = None, llm: Any = None) -> FastAPI:
    wc = os.environ.get("WEB_CONCURRENCY")
    if wc and int(wc) > 1:
        raise RuntimeError("Portal requires single-process execution (in-memory state/locks). WEB_CONCURRENCY must be 1 or unset.")

    cfg = cfg or load_config()
    store = store or EventStore(cfg.db_path)
    mgr = SessionManager(cfg, store, llm=llm)
    practice = PracticeService(mgr)
    signer = URLSafeSerializer(cfg.secret_key, salt="portal-session")

    app = FastAPI(title=cfg.study_title, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg, app.state.store, app.state.mgr, app.state.practice = cfg, store, mgr, practice
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(t=cfg.text, cfg=cfg, fmt_time=fmt_time)

    # ------------------------------------------------------------------ helpers
    def error_page(request: Request, message: str, status: int = 400) -> HTMLResponse:
        theme = request.cookies.get("portal_theme_global") or "system"
        return templates.TemplateResponse(request, "entry_error.html", {"message": message, "title": cfg.study_title, "theme": theme}, status_code=status)

    def cookie_pid(request: Request) -> str | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            return signer.loads(raw)
        except BadSignature:
            return None

    def set_cookie(request: Request, resp: Response, pid: str) -> None:
        cookie_sec = os.environ.get("PORTAL_COOKIE_SECURE")
        sec = (cookie_sec.lower() in ("1", "true", "yes")) if cookie_sec is not None else (request.url.scheme == "https")
        resp.set_cookie(COOKIE, signer.dumps(pid), httponly=True, samesite="lax",
                        secure=sec, max_age=60 * 60 * 24)

    def need_pid(request: Request, check_viewport: bool = True) -> str:
        pid = cookie_pid(request)
        if not pid:
            raise PortalError(cfg.text("errors.no_session"), 401)
        if check_viewport and not cfg.allow_mobile:
            row = store.get_session(pid)
            if row and row.get("state"):
                try:
                    s_data = json.loads(row["state"])
                    if not s_data.get("viewport_allowed", True):
                        raise PortalError(cfg.text("errors.mobile"), 403)
                except PortalError:
                    raise
                except Exception:
                    pass
        return pid

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @app.exception_handler(PortalError)
    async def portal_error_handler(request: Request, exc: PortalError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status)
        return error_page(request, exc.message, exc.status)

    # ------------------------------------------------------------------ entry
    @app.get("/", response_class=HTMLResponse)
    def entry(request: Request, pid: str = "", condition: str = "", sig: str = "", resume: str = ""):
        pid, condition, sig, resume = pid.strip(), condition.strip().upper(), sig.strip(), resume.strip()

        # Resume token recovery (I1)
        if resume:
            row = store.get_session_by_resume_token(resume)
            if not row:
                return error_page(request, "Invalid or expired resume link.", 404)
            r_pid = row["pid"]
            state, why = mgr.resume_or_expire(r_pid)
            if state is None:
                if why == "ended" and row["end_state"] == "completed":
                    resp = RedirectResponse(f"/session/{r_pid}/screen", status_code=303)
                    set_cookie(request, resp, r_pid)
                    return resp
                return error_page(request, cfg.text("errors.timeout" if why == "timeout" else "errors.ended"), 410)
            resp = RedirectResponse(f"/session/{r_pid}/screen", status_code=303)
            set_cookie(request, resp, r_pid)
            return resp

        if not pid and not condition:
            if cfg.standalone:
                theme = request.cookies.get("portal_theme_global") or "system"
                return templates.TemplateResponse(request, "launcher.html", {
                    "conditions": list(cfg.conditions.values()), "title": cfg.study_title, "theme": theme})
            return error_page(request, cfg.text("errors.missing_link"))
        if cfg.standalone and not pid:
            pid = "test-" + uuid.uuid4().hex[:8]
        if not pid or not PID_RE.match(pid):
            return error_page(request, cfg.text("errors.missing_link"))

        # Verify entry link signature (I1)
        if cfg.require_entry_sig or (sig and cfg.entry_link_secret):
            expected = hmac.new(cfg.entry_link_secret.encode(), f"{pid}:{condition}".encode(), hashlib.sha256).hexdigest()[:16]
            if not sig or not hmac.compare_digest(sig, expected):
                return error_page(request, "Invalid entry link signature.", 403)

        ua = request.headers.get("user-agent", "")
        existing = store.get_session(pid)
        if existing:
            if cookie_pid(request) != pid:
                mgr.logger_for(pid).log("entry_rejected", "entry", None, reason="already_used")
                return error_page(request, cfg.text("errors.already_used"), 403)
            state, why = mgr.resume_or_expire(pid)
            if state is None:
                if why == "ended" and existing["end_state"] == "completed":
                    return RedirectResponse(f"/session/{pid}/screen", status_code=303)
                return error_page(request, cfg.text("errors.timeout" if why == "timeout" else "errors.ended"), 410)
            return RedirectResponse(f"/session/{pid}/screen", status_code=303)
        if condition not in cfg.conditions:
            return error_page(request, cfg.text("errors.bad_condition"))
        if MOBILE_RE.search(ua) and not cfg.allow_mobile:
            return error_page(request, cfg.text("errors.mobile"), 403)
        state = mgr.start_session(pid, condition, user_agent=ua)
        if state is None:       # lost a race: somebody else created it first
            return error_page(request, cfg.text("errors.already_used"), 403)
        resp = RedirectResponse(f"/session/{pid}/screen", status_code=303)
        set_cookie(request, resp, pid)
        return resp

    @app.get("/session/{pid}")
    def session_root(pid: str):
        return RedirectResponse(f"/session/{pid}/screen", status_code=303)

    # ------------------------------------------------------------------ screens
    @app.get("/session/{pid}/screen", response_class=HTMLResponse)
    def screen(request: Request, pid: str):
        if cookie_pid(request) != pid:
            return error_page(request, cfg.text("errors.already_used") if store.get_session(pid) else cfg.text("errors.no_session"), 403)
        state = mgr.tick(pid)
        ctx = build_context(request, state)
        return templates.TemplateResponse(request, f"{ctx['template']}.html", ctx,
                                          headers={"Cache-Control": "no-store"})

    def build_context(request: Request, state) -> dict:
        sm = mgr.sm
        cond = sm.condition(state)
        page = state.page
        n_total = len(state.client_order) - 1
        cid = sm.client_id(state)
        client = cfg.clients[cid] if cid else None
        mode = sm.task_mode(state) if cid else None
        last = bool(cid) and sm.is_last_client(state)
        role = sm.card_role(state) if cid else cond.role
        role_cfg = cfg.text("role.roles")[cond.role if cond.role in cfg.text("role.roles") else "none"]
        theme = request.cookies.get(f"portal_theme_{state.pid}") or request.cookies.get("portal_theme_global") or "system"
        ctx: dict[str, Any] = {
            "template": page, "title": cfg.study_title, "pid": state.pid, "state": state, "cond": cond, "page": page,
            "client": client, "mode": mode, "last": last, "n": state.client_index + 1, "total": n_total,
            "role": role, "role_cfg": role_cfg, "standalone": cfg.standalone, "theme": theme,
            "unlock": int(round(sm.unlock_remaining(state))), "typing_delay": cfg.llm.live_typing_delay,
            "min_viewport": cfg.min_viewport_width, "allow_mobile": cfg.allow_mobile,
            "specialists": ordered_specialists(state), "orchestrator": cfg.orchestrator,
            "card_fields": cfg.card_fields,
            "agents_json": {s.name: {"role": s.role_label, "color": s.color} for s in cfg.specialists},
            "fields_json": [{"id": f.id, "label": f.label, "hint": f.hint} for f in cfg.card_fields],
        }
        if page == "team":
            ctx["orch_description"] = cfg.orchestrator["description_by_level"].get(cond.panel_level, "")
        if page == "role":
            ctx["role_message"] = state.role_check_failed
        if page == "practice":
            ctx["practice"] = practice.state_view(state.pid)
            ctx["steps"] = practice.steps(state)
            ctx["role_check"] = cfg.practice["role_check"]
            ctx["noai_pack"] = [dict(sec, html=text_to_html(sec["text"])) for sec in cfg.practice["noai_pack"]]
        if page in ("task", "submit", "brief"):
            ctx["agent_names"] = cfg.all_agent_names
        if page == "task":
            ctx["task_remaining"] = int(round(sm.task_remaining(state)))
            ctx["pack"] = [dict(sec, html=text_to_html(sec["text"])) for sec in mgr.reference_pack(state)] if mode == "pack" else None
            ctx["card"] = mgr.card_view(state)
            ctx["panel_sections"] = cfg.panel.sections.get(cond.panel_level, [])
            ctx["panel_on"] = cond.ai and cond.panel_level != "none" and mode == "team"
        if page == "submit":
            ctx["card"] = mgr.card_view(state)
        if page == "checkin":
            ctx["items"] = mgr.checkin_items(state)
        if page == "break":
            ctx["break_remaining"] = int(round(sm.break_remaining(state)))
        if page == "exit":
            code = state.completion_code or sm.completion_code_for(state.pid)
            ctx["code"] = code
            ctx["exit_url"] = None
            if not cfg.standalone:
                base = cfg.qualtrics.get("exit_survey_url", "")
                sep = "&" if "?" in base else "?"
                ctx["exit_url"] = base + sep + urlencode({"pid": state.pid, "code": code})
        return ctx

    def ordered_specialists(state) -> list:
        order = {n: i for i, n in enumerate(state.specialist_order)}
        return sorted(cfg.specialists, key=lambda s: order.get(s.name, 99))

    # ------------------------------------------------------------------ generic API
    @app.get("/api/timer")
    def timer(request: Request):
        pid = need_pid(request)
        state = mgr.tick(pid)
        sm = mgr.sm
        return {"page": state.page, "task_remaining": sm.task_remaining(state), "break_remaining": sm.break_remaining(state),
                "unlock_remaining": sm.unlock_remaining(state)}

    @app.post("/api/page/continue")
    async def page_continue(request: Request):
        state = mgr.advance(need_pid(request))
        return {"ok": True, "page": state.page}

    @app.post("/api/task/finish")
    async def task_finish(request: Request):
        state = mgr.go_to_submission(need_pid(request))
        return {"ok": True, "page": state.page}

    @app.post("/api/break/end")
    async def break_end(request: Request):
        state = mgr.end_break(need_pid(request))
        return {"ok": True, "page": state.page}

    _rate_limits: dict[str, list[float]] = {}
    _rate_lock = threading.Lock()

    def check_rate_limit(pid: str, limit: int = 25) -> bool:
        now = time.monotonic()
        with _rate_lock:
            times = _rate_limits.setdefault(pid, [])
            _rate_limits[pid] = [t for t in times if now - t < 1.0]
            if len(_rate_limits[pid]) >= limit:
                return False
            _rate_limits[pid].append(now)
            return True

    @app.post("/api/event")
    async def client_event(request: Request):
        cl = int(request.headers.get("content-length") or 0)
        if cl > 4096:
            raise PortalError("Payload too large", 413)
        pid = need_pid(request)
        if not check_rate_limit(pid):
            return JSONResponse({"ok": False, "error": "Rate limit exceeded"}, status_code=429)
        data = await body(request)
        mgr.client_event(pid, str(data.get("type", "")), data.get("metadata") or {})
        return {"ok": True}

    @app.post("/api/events")
    async def client_events_batch(request: Request):
        cl = int(request.headers.get("content-length") or 0)
        if cl > 65536:
            raise PortalError("Payload too large", 413)
        pid = need_pid(request)
        if not check_rate_limit(pid):
            return JSONResponse({"ok": False, "error": "Rate limit exceeded"}, status_code=429)
        try:
            raw = await request.json()
        except Exception:
            raw = []
        events_list = raw if isinstance(raw, list) else (raw.get("events") or [])
        if isinstance(events_list, list):
            mgr.client_events_batch(pid, events_list)
        return {"ok": True, "count": len(events_list) if isinstance(events_list, list) else 0}

    @app.post("/api/viewport")
    async def viewport(request: Request):
        data = await body(request)
        try:
            vp = {
                "width": int(data.get("width", 0)),
                "height": int(data.get("height", 0)),
                "maxTouchPoints": int(data.get("maxTouchPoints", 0)),
                "pointer_coarse": bool(data.get("pointer_coarse", False)),
                "is_mobile": bool(data.get("is_mobile", False))
            }
        except (TypeError, ValueError):
            vp = {"width": 0, "height": 0, "maxTouchPoints": 0, "pointer_coarse": False, "is_mobile": False}
        ua = request.headers.get("user-agent", "")
        pid = need_pid(request, check_viewport=False)
        ok = mgr.record_viewport(pid, vp, user_agent=ua)
        return {"ok": ok, "message": None if ok else cfg.text("errors.small_viewport")}

    @app.get("/api/theme")
    def get_theme(request: Request):
        pid = cookie_pid(request)
        theme = "system"
        if pid:
            theme = request.cookies.get(f"portal_theme_{pid}") or request.cookies.get("portal_theme_global") or "system"
        else:
            theme = request.cookies.get("portal_theme_global") or "system"
        return {"ok": True, "theme": theme, "pid": pid}

    @app.post("/api/theme")
    async def set_theme(request: Request):
        data = await body(request)
        new_theme = str(data.get("theme", "")).strip().lower()
        if new_theme not in ("system", "light", "dark"):
            new_theme = "system"
        pid = data.get("pid") or cookie_pid(request)
        resp = JSONResponse({"ok": True, "theme": new_theme, "pid": pid})
        if pid:
            resp.set_cookie(f"portal_theme_{pid}", new_theme, max_age=60*60*24*365, samesite="lax")
            if store.get_session(pid):
                try:
                    mgr.logger_for(pid).log("theme_change", "screen", None, theme=new_theme)
                except Exception:
                    pass
        resp.set_cookie("portal_theme_global", new_theme, max_age=60*60*24*365, samesite="lax")
        return resp

    # ------------------------------------------------------------------ chat
    def ndjson(gen):
        """Run the generator to completion in ONE worker thread and stream its events as NDJSON."""
        q: queue.Queue = queue.Queue()

        def produce() -> None:
            try:
                for ev in gen:
                    q.put(ev)
            except PortalError as exc:
                q.put({"type": "error", "text": exc.message})
            except Exception:
                q.put({"type": "error", "text": cfg.text("task.agent_failed", name="The team")})
            finally:
                q.put(None)

        threading.Thread(target=produce, daemon=True).start()

        def stream():
            while True:
                ev = q.get()
                if ev is None:
                    return
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        )

    @app.post("/api/chat/open")
    async def chat_open(request: Request):
        return ndjson(mgr.chat_open(need_pid(request)))

    @app.post("/api/chat/send")
    async def chat_send(request: Request):
        pid = need_pid(request)
        data = await body(request)
        text = str(data.get("text", ""))
        pii_entities = scan_pii(text)
        if pii_entities:
            try:
                state = mgr.get(pid)
                mgr.logger(state).log("pii_flag", "task", mgr.sm.client_id(state),
                                     entities=[e.entity_type for e in pii_entities],
                                     count=len(pii_entities))
            except Exception:
                pass
        return ndjson(mgr.chat_send(pid, text))

    @app.get("/api/chat/state")
    def chat_state(request: Request):
        return mgr.chat_state(need_pid(request))

    @app.get("/api/panel")
    def panel(request: Request):
        return {"panel": mgr.panel_poll(need_pid(request))}

    # ------------------------------------------------------------------ card
    @app.post("/api/card/action")
    async def card_action(request: Request):
        d = await body(request)
        pasted = bool(d.get("pasted") or (isinstance(d.get("paste_chars"), (int, float)) and d.get("paste_chars") > 0))
        view = mgr.card_action(need_pid(request), str(d.get("action", "")), str(d.get("span_id", "")),
                               str(d.get("reason", "")), str(d.get("field", "")), str(d.get("text", "")),
                               pasted=pasted)
        return {"ok": True, "card": view}

    @app.get("/api/card")
    def card_get(request: Request):
        state = mgr.tick(need_pid(request))
        return {"card": mgr.card_view(state)}

    @app.post("/api/card/submit")
    async def card_submit(request: Request):
        state = mgr.card_submit(need_pid(request))
        return {"ok": True, "page": state.page}

    @app.post("/api/checkin/submit")
    async def checkin_submit(request: Request):
        d = await body(request)
        state = mgr.checkin_submit(need_pid(request), d.get("answers") or {}, d.get("durations") or {})
        return {"ok": True, "page": state.page}

    # ------------------------------------------------------------------ practice
    @app.get("/api/practice/state")
    def practice_state(request: Request):
        return practice.state_view(need_pid(request))

    @app.post("/api/practice/chat")
    async def practice_chat(request: Request):
        d = await body(request)
        return practice.chat(need_pid(request), str(d.get("text", "")))

    @app.post("/api/practice/panel-read")
    async def practice_panel(request: Request):
        return practice.panel_read(need_pid(request))

    @app.post("/api/practice/card-action")
    async def practice_card(request: Request):
        d = await body(request)
        return practice.card_action(need_pid(request), str(d.get("action", "")), str(d.get("span_id", "")),
                                    str(d.get("reason", "")), str(d.get("text", "")))

    @app.post("/api/practice/done")
    async def practice_done(request: Request):
        return practice.done(need_pid(request))

    @app.post("/api/practice/next")
    async def practice_next(request: Request):
        return practice.next_step(need_pid(request))

    @app.post("/api/practice/submit-card")
    async def practice_submit(request: Request):
        return practice.submit_card(need_pid(request))

    @app.post("/api/practice/role-check")
    async def practice_role_check(request: Request):
        d = await body(request)
        return practice.role_check(need_pid(request), str(d.get("answer", "")))

    # ------------------------------------------------------------------ verification (I2)
    @app.get("/verify")
    @app.get("/api/verify")
    def verify_code(code: str = ""):
        code = code.strip()
        if not code:
            return JSONResponse({"valid": False, "reason": "missing_code"}, status_code=400)
        row = store.get_session_by_completion_code(code)
        if not row:
            return JSONResponse({"valid": False, "reason": "not_found"}, status_code=404)
        return {
            "valid": True,
            "completion_code": code,
            "condition": row["condition_code"],
            "pid": row["pid"],
            "completed_at": row.get("ended_at"),
            "end_state": row.get("end_state")
        }

    # ------------------------------------------------------------------ admin (token protected)
    _admin_failed_attempts: list[float] = []
    _admin_lock = threading.Lock()

    def admin_ok(request: Request) -> bool:
        now = time.time()
        with _admin_lock:
            nonlocal _admin_failed_attempts
            _admin_failed_attempts = [t for t in _admin_failed_attempts if now - t < 60]
            if len(_admin_failed_attempts) >= 10:
                return False

        supplied = request.headers.get("x-admin-token", "")
        auth = request.headers.get("authorization", "")
        if not supplied and auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()

        ok = bool(cfg.admin_token) and bool(supplied) and hmac.compare_digest(supplied.encode(), cfg.admin_token.encode())
        if not ok:
            with _admin_lock:
                _admin_failed_attempts.append(now)
        return ok

    @app.get("/admin/health")
    def admin_health(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        db_stat = Path(cfg.db_path).stat() if Path(cfg.db_path).exists() else None
        db_size = db_stat.st_size if db_stat else 0
        active = sum(1 for s in store.sessions() if not s.get("end_state"))
        return {
            "status": "healthy",
            "db_ok": True,
            "db_size_bytes": db_size,
            "active_sessions": active,
            "config_hash": cfg.config_sha256,
            "config_version": cfg.config_version,
            "model_version": mgr.llm.model_version,
            "uptime_seconds": round(time.monotonic() - APP_START_MONO, 1)
        }

    @app.get("/admin", response_class=HTMLResponse)
    def admin_dashboard(request: Request):
        if not admin_ok(request):
            return HTMLResponse("<h1>403 Forbidden</h1><p>Provide valid x-admin-token or Authorization: Bearer token.</p>", status_code=403)
        sessions = store.sessions()
        total = len(sessions)
        completed = sum(1 for s in sessions if s.get("end_state") == "completed")
        active = sum(1 for s in sessions if not s.get("end_state"))
        dropped = total - completed - active
        return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head><title>Portal Admin Dashboard</title><style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; background: #f8fafc; color: #1e293b; }}
.card {{ background: white; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.stats {{ display: flex; gap: 1rem; }}
.stat-box {{ flex: 1; background: #f1f5f9; padding: 1rem; border-radius: 6px; text-align: center; }}
.stat-box h3 {{ margin: 0; font-size: 2rem; color: #0284c7; }}
.btn {{ display: inline-block; padding: 0.5rem 1rem; background: #0284c7; color: white; text-decoration: none; border-radius: 4px; margin-right: 0.5rem; margin-bottom: 0.5rem; }}
.btn:hover {{ background: #0369a1; }}
</style></head>
<body>
<h1>Interaction Portal Admin</h1>
<div class="card">
  <h2>Study Overview</h2>
  <div class="stats">
    <div class="stat-box"><h3>{total}</h3><p>Total Sessions</p></div>
    <div class="stat-box"><h3>{completed}</h3><p>Completed</p></div>
    <div class="stat-box"><h3>{active}</h3><p>Active</p></div>
    <div class="stat-box"><h3>{dropped}</h3><p>Dropped/Timed out</p></div>
  </div>
</div>
<div class="card">
  <h2>Flat Data Exports</h2>
  <p>Download clean researcher-facing datasets:</p>
  <a class="btn" href="/admin/export/events.csv">Events CSV</a>
  <a class="btn" href="/admin/export/panels.csv">Panels CSV</a>
  <a class="btn" href="/admin/export/transcripts.csv">Transcripts CSV</a>
  <a class="btn" href="/admin/export/card_spans.csv">Card Spans CSV</a>
  <a class="btn" href="/admin/export/checkins.csv">Check-ins CSV</a>
  <a class="btn" href="/admin/export/sessions.csv">Sessions CSV</a>
  <a class="btn" href="/admin/export/all.json">Full Dump (JSON)</a>
</div>
</body></html>""")

    @app.get("/admin/export/events.csv")
    def export_events(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(store.events_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=events.csv"})

    @app.get("/admin/export/panels.csv")
    def export_panels(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(store.panels_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=panels.csv"})

    @app.get("/admin/export/transcripts.csv")
    def export_transcripts(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(store.transcripts_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=transcripts.csv"})

    @app.get("/admin/export/card_spans.csv")
    def export_card_spans(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(store.card_spans_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=card_spans.csv"})

    @app.get("/admin/export/checkins.csv")
    def export_checkins(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(store.checkins_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=checkins.csv"})

    @app.get("/admin/export/sessions.csv")
    def export_sessions(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(store.sessions_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=sessions.csv"})

    @app.get("/admin/export/cards.json")
    def export_cards(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return Response(store.export_cards_json(), media_type="application/json")

    @app.get("/admin/export/all.json")
    def export_all(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return Response(store.export_all_json(), media_type="application/json")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "config_version": cfg.config_version, "standalone": cfg.standalone}

    return app


def text_to_html(text: str) -> str:
    """Escape plain text and render '- ' bullet lines as a list (reference pack and practice pack)."""
    out: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            flush()
        elif line.startswith(("- ", "* ")):
            bullets.append(html.escape(line[2:].strip()))
        else:
            flush()
            out.append(f"<p>{html.escape(line)}</p>")
    flush()
    return "".join(out)


def fmt_time(seconds: float | int) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def main() -> None:
    import uvicorn
    from .terminal import init_terminal, supports_color
    can_color = init_terminal()
    cfg = load_config()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, use_colors=can_color)


if __name__ == "__main__":
    main()
