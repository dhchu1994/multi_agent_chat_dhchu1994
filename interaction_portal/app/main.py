"""
Interaction Portal: FastAPI application.

Run (standalone test mode, no Qualtrics, no API key):

    cd interaction_portal
    PORTAL_STANDALONE=1 python -m app.main

or  uvicorn app.main:create_app --factory   (after `cd interaction_portal`)

Routes are thin: every rule lives in ``SessionManager`` / ``PracticeService`` / ``StateMachine``.
"""

from __future__ import annotations

import hmac
import html
import json
import queue
import re
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from .config_loader import PortalConfig, load_config
from .event_store import EventStore
from .simulation.practice import PracticeService
from .simulation.session_manager import PortalError, SessionManager

HERE = Path(__file__).resolve().parent
PID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MOBILE_RE = re.compile(r"Mobi|Android|iPhone|iPad|iPod|Tablet|Silk|Kindle|webOS|BlackBerry|Windows Phone", re.I)
COOKIE = "portal_session"


def create_app(cfg: PortalConfig | None = None, store: EventStore | None = None, llm: Any = None) -> FastAPI:
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
        return templates.TemplateResponse(request, "entry_error.html", {"message": message, "title": cfg.study_title}, status_code=status)

    def cookie_pid(request: Request) -> str | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            return signer.loads(raw)
        except BadSignature:
            return None

    def set_cookie(request: Request, resp: Response, pid: str) -> None:
        resp.set_cookie(COOKIE, signer.dumps(pid), httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", max_age=60 * 60 * 24)

    def need_pid(request: Request) -> str:
        pid = cookie_pid(request)
        if not pid:
            raise PortalError(cfg.text("errors.no_session"), 401)
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
    def entry(request: Request, pid: str = "", condition: str = ""):
        pid, condition = pid.strip(), condition.strip().upper()
        if not pid and not condition:
            if cfg.standalone:
                return templates.TemplateResponse(request, "launcher.html", {
                    "conditions": list(cfg.conditions.values()), "title": cfg.study_title})
            return error_page(request, cfg.text("errors.missing_link"))
        if cfg.standalone and not pid:
            pid = "test-" + uuid.uuid4().hex[:8]
        if not pid or not PID_RE.match(pid):
            return error_page(request, cfg.text("errors.missing_link"))
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
            return error_page(request, cfg.text("errors.mobile"))
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
        ctx: dict[str, Any] = {
            "template": page, "title": cfg.study_title, "pid": state.pid, "state": state, "cond": cond, "page": page,
            "client": client, "mode": mode, "last": last, "n": state.client_index + 1, "total": n_total,
            "role": role, "role_cfg": role_cfg, "standalone": cfg.standalone,
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

    @app.post("/api/event")
    async def client_event(request: Request):
        data = await body(request)
        mgr.client_event(need_pid(request), str(data.get("type", "")), data.get("metadata") or {})
        return {"ok": True}

    @app.post("/api/viewport")
    async def viewport(request: Request):
        data = await body(request)
        try:
            vp = {"width": int(data.get("width", 0)), "height": int(data.get("height", 0))}
        except (TypeError, ValueError):
            vp = {"width": 0, "height": 0}
        ok = mgr.record_viewport(need_pid(request), vp)
        return {"ok": ok, "message": None if ok else cfg.text("errors.small_viewport")}

    # ------------------------------------------------------------------ chat
    def ndjson(gen):
        """Run the generator to completion in ONE worker thread (the session lock is thread-bound) and
        stream its events as NDJSON. If the browser disconnects the exchange still finishes and is stored."""
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
        return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})

    @app.post("/api/chat/open")
    async def chat_open(request: Request):
        return ndjson(mgr.chat_open(need_pid(request)))

    @app.post("/api/chat/send")
    async def chat_send(request: Request):
        data = await body(request)
        return ndjson(mgr.chat_send(need_pid(request), str(data.get("text", ""))))

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
        view = mgr.card_action(need_pid(request), str(d.get("action", "")), str(d.get("span_id", "")),
                               str(d.get("reason", "")), str(d.get("field", "")), str(d.get("text", "")))
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

    # ------------------------------------------------------------------ admin exports (token protected)
    def admin_ok(request: Request) -> bool:
        supplied = request.headers.get("x-admin-token") or request.query_params.get("token", "")
        return bool(cfg.admin_token) and hmac.compare_digest(supplied.encode(), cfg.admin_token.encode())

    @app.get("/admin/export/events.csv")
    def export_events(request: Request):
        if not admin_ok(request):
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(store.events_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=events.csv"})

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
