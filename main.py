"""
Gradio interface (beta v1) for the multi-agent behavioral simulation.

main.py contains only the *mechanics* of the application: screen navigation,
session state, button/event handlers, answer checking, the live multi-agent
chat (streaming, @mentions, pause/resume, poster saving) and event logging.
Everything a participant reads (titles, paragraphs, button captions,
check questions and answers, conditions, warnings, tips, ...) lives in the
`screens` section of config.yaml, and all agent names, roles, descriptions and
prompts live in the `agents`, `scenario` and `orchestrator` sections of
config.yaml (stable v0.5 prompts, see agents.py and orchestrator.py).

Study flow (Login followed by 10 screens):
0. Login (Task Description on the left, Name + Password in the centre)
1. Consent
2. Task Briefing (text built from ui.task_description)
3. Check 1: The Task (comprehension questions; review loop on failure)
4. Meet Your Team (Coordinator + counterbalanced specialist grid)
5. Check 2: Your Team (comprehension questions; review loop on failure)
6. Interactive Tutorial (4 guided steps: broadcast, @mention, image generation, Pause)
7. Manipulation: Leadership Role (conditions; reflection input where required)
8. Check 3: Reflection Scale (7-point Likert items)
9. Live Task (3-column workspace with collapsible Task/Agents/Tips panels, elapsed
   timer, real-time multi-agent streaming, @mentions, pause/resume, poster saving)
10. Debrief (Exit survey link with participant/session ID)

Every parameter and every text is read from config.yaml. Run with:
    python main.py
    python main.py --config path/to/custom_config.yaml
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import html as html_mod
import json
import random
import re
import threading
import time
import warnings

# Suppress Starlette deprecation warnings originating from Gradio internals
warnings.filterwarnings(
    "ignore",
    message=".*HTTP_422_UNPROCESSABLE_ENTITY.*",
)

import gradio as gr

from config_loader import AppConfig, ScreenTexts, load_config
from conversation_logger import ConversationLogger
from simulation import Simulation, prewarm_clients


# ---------------------------------------------------------------------------
# Constants (screen ids only; all wording comes from config.yaml -> screens)
# ---------------------------------------------------------------------------

SCREENS_ORDER: list[str] = [
    "consent",
    "briefing",
    "check1",
    "team",
    "check2",
    "tutorial",
    "manipulation",
    "check3",
    "live",
    "debrief",
]


def _normalize_layout(value: object, default: str) -> str:
    """Return "vertical" or "horizontal" for a `layout` value from config.yaml."""
    value = str(value or default).strip().lower()
    return value if value in ("vertical", "horizontal") else default


def _load_likert_items(texts: ScreenTexts) -> list[dict]:
    """Read the Check 3 items as ``{"id", "text", "layout"}``.

    An item may be a plain string (id lk<N>, horizontal layout) or a mapping."""
    items = []
    for idx, item in enumerate(texts.get("check3.items")):
        if isinstance(item, dict):
            items.append(
                {
                    "id": item.get("id") or f"lk{idx}",
                    "text": item["text"],
                    "layout": _normalize_layout(item.get("layout"), "horizontal"),
                }
            )
        else:
            items.append({"id": f"lk{idx}", "text": str(item), "layout": "horizontal"})
    return items


def _load_questions(texts: ScreenTexts, section: str) -> list[dict]:
    """Read a check's questions from config.yaml as
    ``{"id", "title", "options": [(text, option_id), ...], "correct"}``."""
    questions = []
    for q in texts.get(f"{section}.questions"):
        questions.append(
            {
                "id": q["id"],
                "title": q["title"],
                "options": [(opt["text"], opt["id"]) for opt in q["options"]],
                "correct": q["correct"],
                "shuffle": bool(q.get("shuffle", True)),
                "layout": _normalize_layout(q.get("layout"), "vertical"),
            }
        )
    return questions


def _load_conditions(texts: ScreenTexts) -> list[dict]:
    """Read the leadership-role conditions from config.yaml."""
    conditions = [
        {
            "id": c["id"],
            "name": c["name"],
            "text": c["text"],
            "reflection": bool(c.get("reflection", False)),
        }
        for c in texts.get("manipulation.conditions")
    ]
    if not conditions:
        raise ValueError("config.yaml: 'screens.manipulation.conditions' must contain at least one condition.")
    return conditions


@dataclass
class StudySessionState:
    """Per-participant session state across the study workflow."""
    user_name: str = "Participant"
    logger: ConversationLogger | None = None
    events: list[str] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=lambda: {"check1": 0, "check2": 0})
    check1_answers: dict[str, str | None] = field(default_factory=dict)
    check2_answers: dict[str, str | None] = field(default_factory=dict)
    completed_screens: set[str] = field(default_factory=set)
    unlocked_screens: set[str] = field(default_factory=set)
    tut1_done: bool = False
    tut2_done: bool = False
    tut3_done: bool = False
    tut4_paused: bool = False
    specialist_order: list[str] = field(default_factory=list)
    condition: dict = field(default_factory=dict)
    reflection_text: str = ""
    likert_responses: dict[str, int] = field(default_factory=dict)
    live_start_time: float | None = None
    current_screen: str = ""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _validate_and_format_name(
    username: str, max_length: int, texts: ScreenTexts
) -> tuple[bool, str, str]:
    """Validate username rules and format it with a capital first letter."""
    username = username.strip()
    if not username:
        return False, "", texts.text("login.errors.empty_name")
    if len(username) > max_length:
        return False, "", texts.text("login.errors.name_too_long", max_length=max_length)
    if any(char.isdigit() for char in username):
        return False, "", texts.text("login.errors.name_has_digits")
    if not any(char.isalpha() for char in username):
        return False, "", texts.text("login.errors.name_needs_letter")

    parts = username.split()
    if len(parts) >= 1:
        parts[0] = parts[0][0].upper() + parts[0][1:]
    if len(parts) >= 2:
        parts[-1] = parts[-1][0].upper() + parts[-1][1:]

    formatted_name = " ".join(parts)
    return True, formatted_name, ""


def _update_google_sheets(share_url: str, config: AppConfig) -> None:
    """Write *share_url* into the configured Google Sheets cell, if configured."""
    gs = config.google_sheets
    if not gs or not gs.spreadsheet_id:
        return

    try:
        import gspread  # type: ignore

        if gs.credentials_file:
            client = gspread.service_account(filename=gs.credentials_file)
        else:
            client = gspread.service_account()

        spreadsheet = client.open_by_key(gs.spreadsheet_id)
        worksheet = spreadsheet.worksheet(gs.sheet_name)
        worksheet.update_acell(gs.cell, share_url)
    except Exception:  # noqa: BLE001
        import traceback
        print(f"[google_sheets] Could not update sheet:\n{traceback.format_exc()}")


def _render_topbar(
    title: str,
    current_screen: str,
    completed_screens: set[str] | None = None,
    labels: dict[str, str] | None = None,
    unlocked_screens: set[str] | None = None,
) -> str:
    """Render the sticky topbar with 10-step progress buttons (green when completed, blue for the
    current/unlocked step; both are clickable, the not-yet-opened ones are disabled)."""
    completed = completed_screens or set()
    unlocked = unlocked_screens or set()
    labels = labels or {}
    steps_html = []
    for scr_id in SCREENS_ORDER:
        classes = ["step"]
        is_completed = scr_id in completed
        is_unlocked = scr_id in unlocked or scr_id == current_screen
        if is_completed:
            classes.append("completed")
            if scr_id == current_screen:
                classes.append("active")
        elif is_unlocked:
            classes.append("active")
        cls_str = " ".join(classes)
        is_clickable = is_completed or is_unlocked
        disabled_attr = "" if is_clickable else " disabled"
        steps_html.append(
            f'<button type="button" class="{cls_str}" data-screen="{html_mod.escape(scr_id)}"{disabled_attr}>'
            f"{html_mod.escape(labels.get(scr_id, scr_id))}"
            f"</button>"
        )
    return (
        f'<div class="topbar">'
        f'<h1 class="title">{html_mod.escape(title)}</h1>'
        f'<div class="progress">{"".join(steps_html)}</div>'
        f"</div>"
    )


def _render_eventlog(events: list[str], texts: ScreenTexts) -> str:
    """Render the bottom collapsible event log drawer."""
    if not events:
        rows = f"<div>{html_mod.escape(texts.text('event_log.empty'))}</div>"
    else:
        rows = "".join(f"<div>{html_mod.escape(e)}</div>" for e in reversed(events))
    return (
        '<details class="eventlog-details">'
        f'<summary class="eventlog-toggle">{html_mod.escape(texts.text("event_log.summary"))}</summary>'
        f'<div class="eventlog-body">{rows}</div>'
        '</details>'
    )


def _record_study_event(
    study_state: StudySessionState,
    event_name: str,
    metadata: dict | None = None,
) -> None:
    """Record an event in the session state and ConversationLogger."""
    if study_state.logger is not None:
        entry_str = study_state.logger.record_event(event_name, metadata=metadata)
    else:
        t_str = time.strftime("%H:%M:%S")
        entry_str = f"{event_name}  —  {t_str}"
    study_state.events.append(entry_str)


def _agent_card_html(name: str, role_label: str, description: str) -> str:
    initial = name[0].upper() if name else "A"
    return (
        '<div class="agent-card">'
        '<div class="agent-head">'
        f'<div class="avatar">{html_mod.escape(initial)}</div>'
        f'<div><p class="agent-name">{html_mod.escape(name)}</p>'
        f'<p class="agent-role">{html_mod.escape(role_label)}</p></div>'
        '</div>'
        f'<p class="agent-desc">{html_mod.escape(description)}</p>'
        '</div>'
    )


def _agent_card_text(agent) -> tuple[str, str]:
    """(role label, description) of an agent as defined in config.yaml.

    The card text is the agent's optional `card_description` (a shorter
    variant, so that all cards have a similar length) and otherwise its
    full `description`.
    """
    role_label = agent.title or agent.role
    description = agent.extra.get("card_description") or agent.description
    return role_label, description


def _specialist_names(config: AppConfig) -> list[str]:
    """Names of all agents except the always-on-top coordinator agent."""
    coordinator = config.screens.text("team.coordinator_agent")
    return [a.name for a in config.agents if a.name != coordinator]


def _render_team_cards_html(config: AppConfig, specialist_order: list[str]) -> str:
    """Render Screen 4 (Meet your team): Coordinator at top + specialist grid."""
    agents_by_name = {a.name: a for a in config.agents}

    top_html = ""
    coord = agents_by_name.get(config.screens.text("team.coordinator_agent"))
    if coord:
        top_html = f'<div class="team-top">{_agent_card_html(coord.name, *_agent_card_text(coord))}</div>'

    cards_html = []
    for name in specialist_order or _specialist_names(config):
        ag = agents_by_name.get(name)
        if ag:
            cards_html.append(_agent_card_html(ag.name, *_agent_card_text(ag)))

    return f'{top_html}<div class="team-grid">{"".join(cards_html)}</div>'


def _render_tut_dots(current_step: int) -> str:
    dots = []
    for i in range(1, 5):
        cls = "tut-dot"
        if i < current_step:
            cls += " done"
        elif i == current_step:
            cls += " current"
        dots.append(f'<div class="{cls}"></div>')
    return f'<div class="tut-progress">{"".join(dots)}</div>'


def _tut_bubble_html(initial: str, text: str, is_self: bool = False, raw_html: str = "") -> str:
    cls = "bubble self" if is_self else "bubble"
    inner = raw_html if raw_html else html_mod.escape(text)
    return (
        f'<div class="{cls}">'
        f'<div class="avatar">{html_mod.escape(initial)}</div>'
        f'<div class="text">{inner}</div>'
        f'</div>'
    )


def _result_box(kind: str, html: str) -> str:
    """Status box shown below a check ("ok" or "warn")."""
    return f'<div class="result-box {kind}">{html}</div>'


def _render_condition_html(cond: dict) -> str:
    return (
        f'<p class="cond-tag">{html_mod.escape(cond["name"])}</p>'
        f'<div class="cond-card">{html_mod.escape(cond["text"])}</div>'
    )


def _render_debrief_card(texts: ScreenTexts, exit_url: str, show_prototype_note: bool = True) -> str:
    paragraphs = "".join(f"<p>{p}</p>" for p in texts.get("debrief.paragraphs"))
    proto_html = (
        f'<div class="muted-note">{texts.text("debrief.prototype_note")}</div>'
        if show_prototype_note
        else ""
    )
    return (
        f'<h2>{html_mod.escape(texts.text("debrief.title"))}</h2>'
        f"{paragraphs}"
        f'<a class="debrief-link" href="{html_mod.escape(exit_url)}" target="_blank">'
        f'{html_mod.escape(texts.text("debrief.link_text"))}</a>'
        f"{proto_html}"
    )


def _render_briefing_html(texts: ScreenTexts, task_description: str) -> str:
    """Briefing text from config.yaml; {task_description} becomes one paragraph per line."""
    task_html = "".join(
        f"<p>{html_mod.escape(line.strip())}</p>"
        for line in task_description.splitlines()
        if line.strip()
    )
    return (
        f'<h2>{html_mod.escape(texts.text("briefing.title"))}</h2>'
        f'<div class="briefing-text">{texts.text("briefing.body_html", task_description=task_html)}</div>'
    )


# ---------------------------------------------------------------------------
# Main UI Builder
# ---------------------------------------------------------------------------

def build_ui(config: AppConfig) -> tuple[gr.Blocks, str]:
    texts = config.screens
    T = texts.text
    agent_colors = {a.name: a.chat_color for a in config.agents}
    _DEFAULT_PLACEHOLDER = config.ui.input_placeholder
    topbar_labels: dict[str, str] = texts.get("topbar.labels")
    CHECK1_QUESTIONS = _load_questions(texts, "check1")
    CHECK2_QUESTIONS = _load_questions(texts, "check2")
    CONDITIONS = _load_conditions(texts)
    LIKERT_ITEMS: list[dict] = _load_likert_items(texts)
    SAVED_BADGE = T("live.saved_badge")

    def _callout_html(section: str) -> str:
        """Callout box under a tutorial step (title + text from config.yaml)."""
        return (
            f'<div class="callout"><strong>{html_mod.escape(T(section + ".callout_title"))}</strong>'
            f'{T(section + ".callout_html")}</div>'
        )

    def _topbar(current_screen: str, study: StudySessionState) -> str:
        study.current_screen = current_screen
        if current_screen in SCREENS_ORDER:
            study.unlocked_screens.add(current_screen)
        return _render_topbar(
            config.ui.title,
            current_screen,
            study.completed_screens,
            topbar_labels,
            study.unlocked_screens,
        )

    def _log_study_event(
        study_state: StudySessionState,
        event_name: str,
        metadata: dict | None = None,
    ) -> str:
        """Record an event and return the updated event-log HTML."""
        _record_study_event(study_state, event_name, metadata)
        return _render_eventlog(study_state.events, texts)

    def _css_height(value: object, default: int) -> str:
        if isinstance(value, (int, float)):
            return f"{int(value)}px" if value > 0 else f"{default}px"
        s = str(value or "").strip()
        if not s:
            return f"{default}px"
        try:
            num = float(s)
            return f"{int(num)}px" if num > 0 else f"{default}px"
        except ValueError:
            return s

    def _css_width(value: object, default: int) -> str:
        if isinstance(value, (int, float)):
            if value <= 0:
                return f"{default}%"
            return f"{value:g}%" if value <= 100 else f"{int(value)}px"
        s = str(value or "").strip()
        if not s:
            return f"{default}%"
        try:
            num = float(s)
            if num <= 0:
                return f"{default}%"
            return f"{num:g}%" if num <= 100 else f"{int(num)}px"
        except ValueError:
            return s

    task_panel_h = _css_height(config.ui.task_panel_height, 560)
    agents_panel_h = _css_height(config.ui.agents_panel_height, 340)
    tips_panel_h = _css_height(config.ui.tips_panel_height, 200)
    task_panel_w = _css_width(config.ui.task_panel_width, 18)
    agents_panel_w = _css_width(config.ui.agents_panel_width, 18)
    tips_panel_w = _css_width(config.ui.tips_panel_width, 18)

    if agents_panel_w == tips_panel_w:
        right_col_w = agents_panel_w
        agents_box_w = "100%"
        tips_box_w = "100%"
    elif agents_panel_w.endswith("%") and tips_panel_w.endswith("%"):
        a_pct = float(agents_panel_w[:-1])
        t_pct = float(tips_panel_w[:-1])
        max_pct = max(a_pct, t_pct)
        right_col_w = f"{max_pct:g}%"
        agents_box_w = f"{(a_pct / max_pct * 100) if max_pct else 100:g}%"
        tips_box_w = f"{(t_pct / max_pct * 100) if max_pct else 100:g}%"
    elif agents_panel_w.endswith("px") and tips_panel_w.endswith("px"):
        a_px = float(agents_panel_w[:-2])
        t_px = float(tips_panel_w[:-2])
        right_col_w = f"{max(a_px, t_px):g}px"
        agents_box_w = agents_panel_w
        tips_box_w = tips_panel_w
    else:
        right_col_w = f"max({agents_panel_w}, {tips_panel_w})"
        agents_box_w = "100%"
        tips_box_w = "100%"

    css = f"""
    /* Light palette (default); Dark palette follows the computer's theme via Gradio's
       `.dark` class (Gradio switches it automatically from prefers-color-scheme). */
    :root, body, html, .gradio-container {{
        --bg: #faf9f7;
        --surface: #ffffff;
        --surface-2: #f3f2ef;
        --border: #e3e1db;
        --border-strong: #c9c6be;
        --text-primary: #211f1c;
        --text-secondary: #4a4742;
        --text-muted: #78746d;
        --accent: #2f5fd6;
        --accent-bg: #e9eefc;
        --accent-text: #1d3f9e;
        --warn-bg: #fbeaea;
        --warn-text: #9a2c2c;
        --ok-bg: #e8f3ea;
        --ok-text: #276a3c;
        --radius: 10px;
        --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        /* Dark ink for text on the pastel agent/user colour boxes (readable in both themes) */
        --ink: #211f1c;
    }}
    .dark, .dark body, .dark .gradio-container {{
        --bg: #14130f;
        --surface: #1f1e1a;
        --surface-2: #2a2924;
        --border: #3a3832;
        --border-strong: #55524a;
        --text-primary: #ecebe7;
        --text-secondary: #c6c3bb;
        --text-muted: #9a968d;
        --accent: #7a9cf0;
        --accent-bg: #1f2a4a;
        --accent-text: #b8ccff;
        --warn-bg: #3a1e1e;
        --warn-text: #f0a8a8;
        --ok-bg: #1b3022;
        --ok-text: #9fdcb2;
    }}

    /* Radio answer layout per question (config.yaml "layout") */
    .layout-vertical .wrap {{
        flex-direction: column !important;
        flex-wrap: nowrap !important;
        align-items: flex-start !important;
    }}
    .layout-horizontal .wrap {{
        flex-direction: row !important;
        flex-wrap: wrap !important;
    }}

    * {{
        box-sizing: border-box !important;
    }}

    body, html, .gradio-container {{
        margin: 0 !important;
        padding: 0 !important;
        font-family: var(--font) !important;
        background: var(--bg) !important;
        color: var(--text-primary) !important;
        -webkit-font-smoothing: antialiased;
        overflow-y: auto !important;
        overflow-x: hidden !important;
    }}

    /* Ensure all bold / strong text across all pages is dark and clearly readable */
    strong, b, .prose strong, .prose b, .markdown-text strong, .markdown-text b,
    .card strong, .card b, .briefing-text strong, .briefing-text b, .panel-body strong {{
        color: var(--text-primary) !important;
        font-weight: 700 !important;
    }}
    .callout strong, .callout b {{
        color: var(--accent-text) !important;
        font-weight: 700 !important;
    }}
    /* Speaker names in the chat bubbles use the same colour as the message text */
    strong.chat-name {{
        color: inherit !important;
    }}

    /* Ensure all headings, paragraphs, labels, and radio/checkbox options are dark and legible */
    h1, h2, h3, h4, p, label, span, li {{
        color: var(--text-primary);
    }}

    footer {{ display: none !important; }}

    .gradio-container, .main, .wrap, .contain {{
        width: 100% !important;
        max-width: 100% !important;
        min-width: 100% !important;
        margin: 0 !important;
        padding: 0 24px 90px !important;
        box-sizing: border-box !important;
    }}
    .main, .wrap, .contain {{
        padding: 0 !important;
    }}

    /* ===================================================================
       Original Login Page Styles (v0.5)
       =================================================================== */
    .text-center {{
        text-align: center !important;
    }}
    .large-markdown, .large-markdown p {{
        font-size: 1.15em !important;
        line-height: 1.65 !important;
        color: var(--text-primary) !important;
    }}
    #login-row {{
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        gap: 16px;
        min-height: calc(100vh - 150px);
        width: 100% !important;
        max-width: 100% !important;
    }}
    #login-left-panel-col {{
        width: 26% !important;
        flex: 0 0 26% !important;
        max-width: 26% !important;
    }}
    #login-task-desc-col {{
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 18px 20px;
    }}
    #login-main-chat-col {{
        width: 48% !important;
        flex: 0 0 48% !important;
        max-width: 48% !important;
        display: flex !important;
        flex-direction: column !important;
    }}
    #login-instructions-col {{
        width: 24% !important;
        flex: 0 0 24% !important;
        max-width: 24% !important;
    }}
    #login-col {{
        max-width: 450px !important;
        width: 100% !important;
        margin: 0 auto !important;
        margin-top: 8vh !important;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 28px 32px;
    }}
    #username-input label > span:first-child, #password-input label > span:first-child {{
        font-size: 1.2em !important;
        color: var(--text-primary) !important;
        font-weight: 600 !important;
    }}
    #username-input .info {{
        font-size: 1.05em !important;
        color: var(--text-secondary) !important;
    }}
    #username-input textarea, #username-input input, #password-input textarea, #password-input input {{
        font-size: 1.15em !important;
        color: var(--text-primary) !important;
        background: var(--surface) !important;
    }}

    /* ===================================================================
       v1 Study Flow Styles (Pages 1-10)
       =================================================================== */
    .topbar {{
        position: sticky;
        top: 0;
        background: var(--bg);
        padding: 18px 0 12px;
        z-index: 15;
        border-bottom: 1px solid var(--border);
        margin-bottom: 20px;
        width: 100% !important;
        max-width: 100% !important;
    }}
    h1.title {{
        font-size: 15px !important;
        font-weight: 500 !important;
        margin: 0 0 12px !important;
        color: var(--text-secondary) !important;
    }}
    .progress {{
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
    }}
    .progress button.step,
    .progress .step {{
        font-family: var(--font) !important;
        font-size: 12px !important;
        padding: 5px 12px !important;
        border-radius: 999px !important;
        border: 1px solid var(--border) !important;
        color: var(--text-muted) !important;
        white-space: nowrap !important;
        background: var(--surface) !important;
        cursor: not-allowed !important;
        opacity: 0.8;
        transition: all 0.15s ease;
    }}
    .progress button.step.active,
    .progress .step.active {{
        cursor: pointer !important;
        border-color: var(--accent) !important;
        color: var(--accent-text) !important;
        background: var(--accent-bg) !important;
        font-weight: 600 !important;
        opacity: 1 !important;
    }}
    .progress button.step.completed,
    .progress .step.completed {{
        background: #28a745 !important;
        border-color: #218838 !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        cursor: pointer !important;
        opacity: 1 !important;
    }}
    .progress button.step.completed:hover,
    .progress .step.completed:hover {{
        background: #218838 !important;
        box-shadow: 0 2px 6px rgba(40, 167, 69, 0.35) !important;
    }}
    .progress button.step.completed.active,
    .progress .step.completed.active {{
        background: #1e7e34 !important;
        border-color: #145523 !important;
        color: #ffffff !important;
        box-shadow: 0 0 0 2px rgba(40, 167, 69, 0.45) !important;
    }}

    /* Cards & Typography - Constant Full-Screen Width Across All Tabs & Subtabs */
    .card,
    #login-wrapper,
    #screen-consent,
    #screen-briefing,
    #screen-check1,
    #screen-team,
    #screen-check2,
    #screen-tutorial,
    #screen-manipulation,
    #screen-check3,
    #screen-live,
    #screen-debrief {{
        width: 100% !important;
        max-width: 100% !important;
        min-width: 100% !important;
        margin: 0 !important;
        box-sizing: border-box !important;
    }}
    .card {{
        background: var(--surface) !important;
        border: 1px solid var(--border) !important;
        border-radius: 14px !important;
        padding: 28px 32px !important;
    }}
    .card h2 {{
        font-size: 20px !important;
        font-weight: 600 !important;
        margin: 0 0 10px !important;
        color: var(--text-primary) !important;
    }}
    .card p {{
        line-height: 1.7 !important;
        color: var(--text-secondary) !important;
        font-size: 14.5px !important;
        margin: 0 0 12px !important;
        max-width: 100% !important;
    }}
    .briefing-text, .briefing-text p {{
        font-size: 15px !important;
        line-height: 1.8 !important;
        color: var(--text-primary) !important;
        width: 100% !important;
        max-width: 100% !important;
        margin: 0 0 14px !important;
    }}
    .muted-note {{
        font-size: 12.5px;
        color: var(--text-secondary);
        background: var(--surface-2);
        border-radius: 8px;
        padding: 8px 12px;
        margin-top: 14px;
        line-height: 1.5;
    }}
    .timer-note {{
        font-size: 14px;
        color: var(--accent-text);
        background: var(--accent-bg);
        border-radius: 8px;
        padding: 10px 14px;
        margin-top: 16px;
        font-weight: 600;
        display: inline-block;
    }}

    /* Team Grid & Agent Cards */
    .team-top {{
        margin-bottom: 16px;
        margin-top: 12px;
    }}
    .team-grid {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 14px;
        width: 100% !important;
    }}
    @media (max-width: 780px) {{
        .team-grid {{
            grid-template-columns: 1fr;
        }}
    }}
    .agent-card {{
        background: var(--surface-2);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 16px 18px;
        height: 100%;
    }}
    .agent-head {{
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 10px;
    }}
    .avatar {{
        width: 38px;
        height: 38px;
        border-radius: 50%;
        background: var(--surface);
        border: 1px solid var(--border);
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
        flex-shrink: 0;
    }}
    .agent-name {{
        font-size: 14px !important;
        font-weight: 600 !important;
        margin: 0 !important;
        color: var(--text-primary) !important;
    }}
    .agent-role {{
        font-size: 12px !important;
        color: var(--text-secondary) !important;
        margin: 0 !important;
    }}
    .agent-desc {{
        font-size: 13px !important;
        color: var(--text-secondary) !important;
        margin: 0 !important;
        padding-top: 10px;
        border-top: 1px solid var(--border);
        line-height: 1.55 !important;
    }}

    /* Tutorial styles - Constant full width across all 4 subtabs */
    #tut-step-1,
    #tut-step-2,
    #tut-step-3,
    #tut-step-4 {{
        width: 100% !important;
        max-width: 100% !important;
        min-width: 100% !important;
        box-sizing: border-box !important;
    }}
    .tut-progress {{
        display: flex;
        gap: 6px;
        margin-bottom: 16px;
    }}
    .tut-dot {{
        width: 26px;
        height: 4px;
        border-radius: 2px;
        background: var(--border);
    }}
    .tut-dot.done {{
        background: var(--border-strong);
    }}
    .tut-dot.current {{
        background: var(--accent);
    }}
    .tut-context {{
        background: var(--surface-2);
        border-radius: 10px;
        padding: 14px 16px;
        min-height: 48px;
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
    }}
    .tut-context.dimmed {{
        opacity: 0.7;
    }}
    .bubble {{
        display: flex;
        gap: 8px;
        margin-bottom: 8px;
        font-size: 13px;
        align-items: flex-start;
    }}
    .bubble .avatar {{
        width: 26px;
        height: 26px;
        font-size: 11px;
    }}
    .bubble .text {{
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 7px 11px;
        max-width: 75%;
        color: var(--text-primary);
    }}
    .bubble.self {{
        flex-direction: row-reverse;
    }}
    .callout {{
        background: var(--accent-bg);
        color: var(--accent-text);
        border-radius: 8px;
        padding: 11px 14px;
        margin-top: 10px;
        font-size: 13.5px;
        line-height: 1.6;
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
    }}
    .callout strong {{
        display: block;
        margin-bottom: 3px;
        font-weight: 700 !important;
        color: var(--accent-text) !important;
    }}
    .warn-line {{
        color: var(--warn-text);
        font-size: 13px;
        margin-top: 8px;
        font-weight: 600;
    }}

    /* Result / Alert Boxes */
    .result-box {{
        font-size: 13.5px;
        margin-top: 10px;
        padding: 10px 14px;
        border-radius: 8px;
        font-weight: 500;
    }}
    .result-box.ok {{
        background: var(--ok-bg);
        color: var(--ok-text);
    }}
    .result-box.warn {{
        background: var(--warn-bg);
        color: var(--warn-text);
    }}

    /* Condition / Manipulation Card */
    .cond-card {{
        background: var(--surface-2);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 18px 20px;
        margin-bottom: 14px;
        font-size: 14.5px;
        line-height: 1.7;
        color: var(--text-primary);
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
    }}
    .cond-tag {{
        display: inline-block;
        font-size: 11.5px !important;
        padding: 4px 10px;
        border-radius: 999px;
        background: var(--accent-bg);
        color: var(--accent-text) !important;
        margin-bottom: 10px !important;
        font-weight: 600;
    }}

    /* Live Task Layout & Panels: Large Chat Conversation + Readable Side Panels */
    #live-row {{
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        gap: 14px !important;
        width: 100% !important;
        max-width: 100% !important;
        align-items: flex-start !important;
    }}
    #left-panel-col {{
        width: {task_panel_w} !important;
        flex: 0 0 {task_panel_w} !important;
        max-width: {task_panel_w} !important;
        min-width: 180px !important;
    }}
    #main-chat-col {{
        width: calc(100% - {task_panel_w} - {right_col_w}) !important;
        flex: 1 1 calc(100% - {task_panel_w} - {right_col_w}) !important;
        max-width: calc(100% - {task_panel_w} - {right_col_w}) !important;
        min-width: 0 !important;
    }}
    #instructions-col {{
        width: {right_col_w} !important;
        flex: 0 0 {right_col_w} !important;
        max-width: {right_col_w} !important;
        min-width: 180px !important;
    }}
    #instructions-col .panel.agents-panel {{
        width: {agents_box_w};
        max-width: 100%;
    }}
    #instructions-col .panel.tips-panel {{
        width: {tips_box_w};
        max-width: 100%;
    }}
    .panel {{
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 12px 14px;
        font-size: 14.5px !important;
        margin-bottom: 10px;
    }}
    .panel summary, .panel summary span {{
        font-size: 16px !important;
        font-weight: 700 !important;
        cursor: pointer;
        color: var(--text-primary) !important;
        outline: none;
        list-style: none;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }}
    .panel summary::-webkit-details-marker {{
        display: none;
    }}
    .panel .panel-body, .panel .panel-body p, .panel .panel-body div {{
        margin-top: 8px;
        color: var(--text-secondary) !important;
        line-height: 1.6 !important;
        font-size: 14.5px !important;
    }}
    .mini-agent {{
        padding: 8px 10px !important;
        margin-bottom: 6px;
        border-radius: 8px;
    }}
    .mini-agent:last-child {{
        margin-bottom: 0;
    }}
    /* Fixed-height panel bodies: long content scrolls inside the panel */
    .panel .panel-body.scroll-body {{
        overflow-y: auto !important;
        overflow-x: hidden;
        padding-right: 4px;
    }}
    #left-panel-col .panel .panel-body.scroll-body {{
        height: {task_panel_h};
    }}
    #instructions-col .panel .panel-body.scroll-body.agents-body {{
        height: {agents_panel_h};
    }}
    #instructions-col .panel .panel-body.scroll-body.tips-body {{
        height: {tips_panel_h};
    }}
    .panel .mini-agent .agent-name,
    .mini-agent .agent-name {{
        font-size: 15.5px !important;
        font-weight: 700 !important;
        color: var(--ink) !important;
    }}
    .panel .mini-agent .agent-role,
    .mini-agent .agent-role {{
        font-size: 14px !important;
        font-weight: 600 !important;
        color: var(--ink) !important;
    }}
    .panel .mini-agent .agent-desc-mini,
    .mini-agent .agent-desc-mini {{
        font-size: 14px !important;
        line-height: 1.55 !important;
        color: var(--ink) !important;
        margin: 4px 0 0 !important;
    }}
    .chat-top {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 6px;
        padding: 4px 2px;
    }}
    .timer-badge {{
        font-size: 12.5px;
        color: var(--text-primary);
        background: var(--surface-2);
        padding: 4px 10px;
        border-radius: 999px;
        border: 1px solid var(--border);
        font-weight: 500;
    }}

    /* Chatbot border: transparent normally, glowing when paused */
    #chatbot {{
        border: 3px solid var(--border);
        border-radius: 12px;
        background: var(--surface);
        width: 100% !important;
    }}
    @keyframes border-glow {{
        0%, 100% {{ border-color: transparent; }}
        50%       {{ border-color: {config.ui.pause_border_color}; }}
    }}
    #chatbot.paused,
    #main-chat-col:has(#pause-btn.paused) #chatbot {{
        border: 3px solid {config.ui.pause_border_color} !important;
        animation: border-glow {config.ui.pause_animation_duration_seconds}s ease-in-out infinite;
    }}

    /* Debrief link */
    .debrief-link {{
        display: inline-block;
        margin-top: 16px;
        color: var(--accent-text) !important;
        text-decoration: none;
        font-size: 14.5px;
        font-weight: 600;
        border-bottom: 1px solid var(--accent-text);
    }}

    /* Sticky bottom Event Log */
    .eventlog-details {{
        position: fixed;
        left: 0;
        right: 0;
        bottom: 0;
        background: var(--surface);
        border-top: 1px solid var(--border);
        z-index: 30;
    }}
    .eventlog-toggle {{
        width: 100%;
        text-align: left;
        background: var(--surface-2);
        font-size: 12.5px;
        padding: 8px 24px;
        color: var(--text-secondary);
        cursor: pointer;
        user-select: none;
    }}
    .eventlog-body {{
        max-height: 150px;
        overflow-y: auto;
        padding: 8px 24px 14px;
        font-family: ui-monospace, Menlo, monospace;
        font-size: 11.5px;
        color: var(--text-muted);
        background: var(--surface);
    }}
    .eventlog-body div {{
        padding: 2px 0;
    }}

    /* Remove focus outlines on text inputs as in v0.5 */
    #txt-input,
    #txt-input *,
    #username-input,
    #username-input *,
    #password-input,
    #password-input * {{
        --ring-color: transparent !important;
        --shadow: none !important;
    }}
    #txt-input textarea:focus,
    #txt-input input:focus,
    #username-input textarea:focus,
    #username-input input:focus,
    #password-input textarea:focus,
    #password-input input:focus {{
        border-color: var(--border-strong, #c9c6be) !important;
        box-shadow: none !important;
        outline: none !important;
        --ring-color: transparent !important;
    }}

    /* @mention autocomplete dropdown */
    .mention-drop {{
        position: absolute !important;
        left: 0;
        bottom: 100%;
        min-width: 240px;
        max-width: 100%;
        background: var(--surface);
        border: 1px solid var(--border-strong);
        border-radius: 8px;
        overflow: hidden;
        z-index: 50;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
        margin-bottom: 4px;
    }}
    .mention-drop .opt {{
        padding: 8px 12px;
        font-size: 13px;
        cursor: pointer;
        display: flex;
        align-items: center;
        gap: 8px;
        color: var(--text-primary);
    }}
    .mention-drop .opt:hover {{
        background: var(--surface-2);
    }}
    .mention-drop .opt .avatar {{
        width: 22px;
        height: 22px;
        font-size: 10px;
    }}
    .screen-hidden {{
        display: none !important;
    }}
    """

    with gr.Blocks(title=config.ui.title) as demo:
        # Session & Simulation States
        study_state = gr.State(value=None)
        sim_state = gr.State(value=None)
        paused_state = gr.State(value=False)

        # ===================================================================
        # PAGE 0: ORIGINAL LOGIN PAGE (from v0.5)
        # ===================================================================
        with gr.Column(visible=True, elem_id="login-wrapper", elem_classes=[]) as login_view:
            gr.Markdown(f"# {config.ui.title}", elem_classes="text-center")
            with gr.Row(elem_id="login-row"):
                with gr.Column(scale=3, elem_id="login-left-panel-col"):
                    with gr.Column(visible=config.ui.show_task_description, elem_id="login-task-desc-col"):
                        gr.Markdown(f"## {config.ui.task_description_title}\n<br>")
                        gr.Markdown(config.ui.task_description, elem_classes="large-markdown")
                with gr.Column(scale=6, elem_id="login-main-chat-col"):
                    with gr.Column(elem_id="login-col"):
                        username_input = gr.Textbox(
                            label=T("login.name_label"),
                            info=T("login.name_info", max_length=config.ui.max_username_length),
                            placeholder=T("login.name_placeholder"),
                            value="",
                            elem_id="username-input",
                        )
                        password_input = gr.Textbox(
                            label=T("login.password_label"),
                            placeholder=T("login.password_placeholder"),
                            type="password",
                            elem_id="password-input",
                            visible=bool(config.ui.password),
                        )
                        login_btn = gr.Button(T("login.button"), variant="primary", elem_id="login-btn")
                with gr.Column(scale=3, elem_id="login-instructions-col"):
                    pass

        # Global style tag (unscoped by Gradio's .gradio-container prefixing) to keep
        # full-screen width 100% constant across all tabs and Tutorial subtabs
        gr.HTML(
            value=(
                "<style>"
                "html { overflow-y: scroll !important; overflow-x: hidden !important; width: 100% !important; }"
                "body, gradio-app { overflow-x: hidden !important; overflow-y: visible !important; width: 100% !important; max-width: 100% !important; margin: 0 !important; padding: 0 !important; }"
                "</style>"
            ),
            visible=True,
        )

        # Sticky Topbar with 10-screen progress indicator (shown after Login)
        topbar_html = gr.HTML(
            value="",
            visible=True,
            elem_classes=["screen-hidden"],
        )
        topbar_nav_input = gr.Textbox(
            value="",
            show_label=False,
            elem_id="topbar-nav-input",
            elem_classes=["screen-hidden"],
        )
        topbar_nav_btn = gr.Button(
            "Jump",
            elem_id="topbar-nav-btn",
            elem_classes=["screen-hidden"],
        )

        # ===================================================================
        # PAGE 1: CONSENT
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-consent", elem_classes=["card", "screen-hidden"]) as screen_consent:
            gr.HTML(
                f'<h2>{html_mod.escape(T("consent.title"))}</h2>'
                + "".join(f"<p>{p}</p>" for p in texts.get("consent.paragraphs"))
            )
            consent_checkbox = gr.Checkbox(
                label=T("consent.checkbox_label"),
                value=False,
                elem_id="consent-box",
            )
            with gr.Row():
                consent_prev_btn = gr.Button(T("common.previous"), elem_id="consent-prev")
                consent_continue_btn = gr.Button(
                    T("common.continue"),
                    variant="primary",
                    interactive=False,
                    elem_id="consent-continue",
                )

        # ===================================================================
        # PAGE 2: TASK BRIEFING
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-briefing", elem_classes=["card", "screen-hidden"]) as screen_briefing:
            gr.HTML(_render_briefing_html(texts, config.ui.task_description))
            with gr.Row():
                briefing_prev_btn = gr.Button(T("common.previous"), elem_id="briefing-prev")
                briefing_continue_btn = gr.Button(
                    T("common.continue"),
                    variant="primary",
                    visible=True,
                    interactive=True,
                    elem_id="briefing-continue",
                )

        # ===================================================================
        # PAGE 3: CHECK 1 — THE TASK
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-check1", elem_classes=["card", "screen-hidden"]) as screen_check1:
            gr.HTML(
                f'<h2>{html_mod.escape(T("check1.title"))}</h2>'
                f'<p>{html_mod.escape(T("check1.intro"))}</p>'
            )
            c1_radios: list[gr.Radio] = []
            for q in CHECK1_QUESTIONS:
                c1_opts = list(q["options"])
                if q["shuffle"]:
                    random.shuffle(c1_opts)
                r = gr.Radio(
                    choices=c1_opts,
                    label=q["title"],
                    value=None,
                    elem_id=q["id"],
                    elem_classes=[f"layout-{q['layout']}"],
                )
                c1_radios.append(r)

            check1_result_html = gr.HTML(value="")
            with gr.Row():
                check1_prev_btn = gr.Button(T("common.previous"), elem_id="check1-prev")
                check1_review_btn = gr.Button(
                    T("check1.review_button"),
                    visible=True,
                    elem_classes=["screen-hidden"],
                    elem_id="check1-review",
                )
                check1_submit_btn = gr.Button(
                    T("check1.submit_button"),
                    variant="primary",
                    elem_id="check1-submit",
                )

        # ===================================================================
        # PAGE 4: MEET YOUR TEAM
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-team", elem_classes=["card", "screen-hidden"]) as screen_team:
            gr.HTML(
                f'<h2>{html_mod.escape(T("team.title"))}</h2>'
                f'<p>{html_mod.escape(T("team.intro"))}</p>'
            )
            team_cards_html = gr.HTML(
                value=_render_team_cards_html(config, [])
            )
            gr.HTML(
                f'<div class="muted-note">{html_mod.escape(T("team.note"))}</div>'
            )
            with gr.Row():
                team_prev_btn = gr.Button(T("common.previous"), elem_id="team-prev")
                team_shuffle_btn = gr.Button(T("team.shuffle_button"))
                team_continue_btn = gr.Button(T("common.continue"), variant="primary", elem_id="team-continue")

        # ===================================================================
        # PAGE 5: CHECK 2 — YOUR TEAM
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-check2", elem_classes=["card", "screen-hidden"]) as screen_check2:
            gr.HTML(
                f'<h2>{html_mod.escape(T("check2.title"))}</h2>'
                f'<p>{html_mod.escape(T("check2.intro"))}</p>'
            )
            c2_radios: list[gr.Radio] = []
            for q in CHECK2_QUESTIONS:
                c2_opts = list(q["options"])
                if q["shuffle"]:
                    random.shuffle(c2_opts)
                r = gr.Radio(
                    choices=c2_opts,
                    label=q["title"],
                    value=None,
                    elem_id=q["id"],
                    elem_classes=[f"layout-{q['layout']}"],
                )
                c2_radios.append(r)

            check2_result_html = gr.HTML(value="")
            with gr.Row():
                check2_prev_btn = gr.Button(T("common.previous"), elem_id="check2-prev")
                check2_review_btn = gr.Button(
                    T("check2.review_button"),
                    visible=True,
                    elem_classes=["screen-hidden"],
                    elem_id="check2-review",
                )
                check2_submit_btn = gr.Button(
                    T("check2.submit_button"),
                    variant="primary",
                    elem_id="check2-submit",
                )

        # ===================================================================
        # PAGE 6: INTERACTIVE TUTORIAL (4 Guided Sub-steps)
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-tutorial", elem_classes=["card", "screen-hidden"]) as screen_tutorial:
            gr.HTML(f'<h2>{html_mod.escape(T("tutorial.title"))}</h2>')
            tut_progress_html = gr.HTML(value=_render_tut_dots(1))

            # --- Tutorial Step 1: Broadcast message ---
            with gr.Column(visible=True, elem_id="tut-step-1", elem_classes=[]) as tut_step_1_col:
                tut1_context_html = gr.HTML(
                    value=(
                        '<div class="tut-context dimmed"><p style="margin:0; font-size:12.5px;">'
                        f'{T("tutorial.step1.empty_context_html")}</p></div>'
                    ),
                    elem_id="tut1-context",
                )
                tut1_input = gr.Textbox(
                    placeholder=T("tutorial.step1.input_placeholder"),
                    show_label=False,
                    elem_id="tut1-input",
                )
                gr.HTML(_callout_html("tutorial.step1"))
                with gr.Row():
                    tut1_prev_btn = gr.Button(T("common.previous"), elem_id="tut1-prev")
                    tut1_send_btn = gr.Button(T("tutorial.send_button"), variant="primary", elem_id="tut1-send")

            # --- Tutorial Step 2: @Alice mention ---
            with gr.Column(visible=True, elem_id="tut-step-2", elem_classes=["screen-hidden"]) as tut_step_2_col:
                tut2_context_html = gr.HTML(value='<div class="tut-context"></div>', elem_id="tut2-context")
                with gr.Row():
                    tut2_mention_btns = []
                    for ag_name in texts.get("tutorial.step2.mention_buttons"):
                        b = gr.Button(f"@{ag_name}", size="sm")
                        tut2_mention_btns.append((b, ag_name))
                tut2_input = gr.Textbox(
                    placeholder=T("tutorial.step2.input_placeholder"),
                    show_label=False,
                    elem_id="tut2-input",
                )
                gr.HTML(_callout_html("tutorial.step2"))
                tut2_warn_html = gr.HTML(value="")
                with gr.Row():
                    tut2_back_btn = gr.Button(T("common.previous"), elem_id="tut2-prev")
                    tut2_send_btn = gr.Button(T("tutorial.send_button"), variant="primary", elem_id="tut2-send")

            # --- Tutorial Step 3: @Picasso image generation ---
            with gr.Column(visible=True, elem_id="tut-step-3", elem_classes=["screen-hidden"]) as tut_step_3_col:
                tut3_context_html = gr.HTML(value='<div class="tut-context"></div>', elem_id="tut3-context")
                with gr.Row():
                    tut3_mention_btns = []
                    for ag_name in texts.get("tutorial.step3.mention_buttons"):
                        b = gr.Button(f"@{ag_name}", size="sm")
                        tut3_mention_btns.append((b, ag_name))
                tut3_input = gr.Textbox(
                    placeholder=T("tutorial.step3.input_placeholder"),
                    show_label=False,
                    elem_id="tut3-input",
                )
                gr.HTML(_callout_html("tutorial.step3"))
                tut3_warn_html = gr.HTML(value="")
                with gr.Row():
                    tut3_back_btn = gr.Button(T("common.previous"), elem_id="tut3-prev")
                    tut3_send_btn = gr.Button(T("tutorial.send_button"), variant="primary", elem_id="tut3-send")

            # --- Tutorial Step 4: Pause anytime ---
            with gr.Column(visible=True, elem_id="tut-step-4", elem_classes=["screen-hidden"]) as tut_step_4_col:
                gr.HTML(
                    '<div class="tut-context">'
                    + _tut_bubble_html(
                        T("tutorial.step4.context_initial"),
                        T("tutorial.step4.context_text"),
                    )
                    + "</div>"
                )
                with gr.Row():
                    tut4_input = gr.Textbox(
                        placeholder=T("tutorial.step4.input_placeholder"),
                        show_label=False,
                        scale=5,
                        elem_id="tut4-input",
                    )
                    tut4_pause_btn = gr.Button(T("tutorial.step4.pause_button"), scale=1, elem_id="tut4-pause")
                gr.HTML(_callout_html("tutorial.step4"))
                tut4_paused_note_html = gr.HTML(value="")
                with gr.Row():
                    tut4_back_btn = gr.Button(T("common.previous"), elem_id="tut4-prev")
                    tut4_continue_btn = gr.Button(T("common.continue"), variant="primary", interactive=False, elem_id="tut4-continue")

        # ===================================================================
        # PAGE 7: MANIPULATION — LEADERSHIP ROLE
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-manipulation", elem_classes=["card", "screen-hidden"]) as screen_manipulation:
            gr.HTML(f'<h2>{html_mod.escape(T("manipulation.title"))}</h2>')
            cond_display_html = gr.HTML(value=_render_condition_html(CONDITIONS[0]))
            with gr.Column(visible=True, elem_id="cond-reflection-wrap", elem_classes=["screen-hidden"]) as cond_reflection_col:
                gr.HTML(
                    '<h3 class="sub" style="font-size:14px; font-weight:600; margin:12px 0 4px;">'
                    f'{html_mod.escape(T("manipulation.reflection_title"))}</h3>'
                )
                cond_reflection_input = gr.Textbox(
                    placeholder=T("manipulation.reflection_placeholder"),
                    show_label=False,
                    lines=4,
                    elem_id="cond-reflection",
                )
            if config.ui.show_prototype_note:
                gr.HTML(
                    '<div class="muted-note">'
                    + T("manipulation.prototype_note", min_length=config.ui.min_reflection_length)
                    + "</div>"
                )
            with gr.Row():
                cond_prev_btn = gr.Button(T("common.previous"), elem_id="cond-prev")
                cond_reassign_btn = gr.Button(T("manipulation.reassign_button"), elem_id="cond-reassign")
                cond_continue_btn = gr.Button(T("common.continue"), variant="primary", elem_id="cond-continue")

        # ===================================================================
        # PAGE 8: CHECK 3 — REFLECTION SCALE (14 Likert items)
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-check3", elem_classes=["card", "screen-hidden"]) as screen_check3:
            gr.HTML(
                f'<h2>{html_mod.escape(T("check3.title"))}</h2>'
                f'<p>{html_mod.escape(T("check3.intro", count=len(LIKERT_ITEMS)))}</p>'
            )
            likert_choices = [(opt["text"], str(opt["value"])) for opt in texts.get("check3.scale")]
            likert_radios: list[gr.Radio] = []
            for idx, item in enumerate(LIKERT_ITEMS):
                lr = gr.Radio(
                    choices=likert_choices,
                    label=f"{idx + 1}. {item['text']}",
                    value=None,
                    elem_id=item["id"],
                    elem_classes=[f"layout-{item['layout']}"],
                )
                likert_radios.append(lr)

            check3_result_html = gr.HTML(value="")
            with gr.Row():
                check3_prev_btn = gr.Button(T("common.previous"), elem_id="check3-prev")
                check3_continue_btn = gr.Button(T("common.continue"), variant="primary", elem_id="check3-continue")

        # ===================================================================
        # PAGE 9: LIVE TASK
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-live", elem_classes=["screen-hidden"]) as screen_live:
            welcome_md = gr.Markdown(value="", visible=True, elem_classes=["text-center", "screen-hidden"])
            with gr.Row(elem_id="live-row"):
                # Left Column: Collapsible Task Panel
                with gr.Column(scale=2, min_width=180, elem_id="left-panel-col"):
                    task_panel_html = (
                        '<details open class="panel">'
                        f'<summary><span>{html_mod.escape(T("live.task_panel_title"))}</span><span>▾</span></summary>'
                        '<div class="panel-body scroll-body">'
                        '<p style="margin:0 0 8px; font-size:14.5px; line-height:1.6;">'
                        f'{T("live.goal_html")}</p>'
                        f'<div style="font-size:14px; line-height:1.6; color:var(--text-secondary); margin-top:8px;">{html_mod.escape(config.ui.task_description)}</div>'
                        "</div>"
                        "</details>"
                    )
                    gr.HTML(value=task_panel_html)

                # Center Column: Large Chatbot, @Mention buttons, Input + Send/Pause
                with gr.Column(scale=8, min_width=500, elem_id="main-chat-col"):
                    gr.HTML(
                        value=(
                            '<div class="chat-top">'
                            f'<strong style="font-size:14px;">{html_mod.escape(T("live.chat_title"))}</strong>'
                            f'<span class="timer-badge">{html_mod.escape(T("live.timer_label"))} <span id="live-timer">00:00</span></span>'
                            "</div>"
                        )
                    )
                    chatbot = gr.Chatbot(
                        height=max(config.ui.chatbot_height, 640),
                        group_consecutive_messages=False,
                        sanitize_html=False,
                        elem_id="chatbot",
                    )

                    with gr.Row(elem_id="agent-mention-buttons"):
                        mention_btns = []
                        for a in config.agents:
                            btn = gr.Button(f"@{a.name}", size="sm")
                            mention_btns.append((btn, a.name))

                    with gr.Row(equal_height=True):
                        txt = gr.Textbox(
                            placeholder=_DEFAULT_PLACEHOLDER,
                            show_label=False,
                            container=False,
                            scale=7,
                            lines=1,
                            max_lines=1,
                            elem_id="txt-input",
                        )
                        send_btn = gr.Button(T("live.send_button"), variant="primary", scale=1, elem_id="send-btn")
                        pause_btn = gr.Button(T("live.pause_button"), scale=1, elem_id="pause-btn")

                    gr.HTML(
                        f'<div class="muted-note">{html_mod.escape(T("live.poster_note"))}</div>'
                    )

                # Right Column: Collapsible Agents & Tips Panels
                with gr.Column(scale=2, min_width=180, elem_id="instructions-col"):
                    agents_items_html = ""
                    for a in config.agents:
                        role_str = a.title or a.role
                        color = a.chat_color or "#f3f2ef"
                        _, desc_str = _agent_card_text(a)
                        agents_items_html += (
                            f'<div class="mini-agent" style="background:{color}; border:2px solid {color};">'
                            f'<p class="agent-name">{html_mod.escape(a.name)}</p>'
                            f'<p class="agent-role">{html_mod.escape(role_str)}</p>'
                            f'<p class="agent-desc-mini">{html_mod.escape(desc_str)}</p>'
                            f"</div>"
                        )
                    right_panels_html = (
                        '<details open class="panel agents-panel">'
                        f'<summary><span>{html_mod.escape(T("live.agents_panel_title"))}</span><span>▾</span></summary>'
                        f'<div class="panel-body scroll-body agents-body">{agents_items_html}</div>'
                        "</details>"
                        '<details open class="panel tips-panel">'
                        f'<summary><span>{html_mod.escape(T("live.tips_panel_title"))}</span><span>▾</span></summary>'
                        '<div class="panel-body scroll-body tips-body" style="font-size:14.5px; line-height:1.6;">'
                        f'{T("live.tips_html")}'
                        "</div>"
                        "</details>"
                    )
                    gr.HTML(value=right_panels_html)

            with gr.Row():
                live_prev_btn = gr.Button(T("common.previous"), elem_id="live-prev")
                finish_task_btn = gr.Button(T("live.finish_button"), variant="primary", elem_id="finish-task-btn")

        # ===================================================================
        # PAGE 10: DEBRIEF
        # ===================================================================
        with gr.Column(visible=True, elem_id="screen-debrief", elem_classes=["card", "screen-hidden"]) as screen_debrief:
            debrief_html = gr.HTML(
                value=_render_debrief_card(
                    texts,
                    config.ui.exit_survey_url.format(session_id="DEMO123"),
                    show_prototype_note=config.ui.show_prototype_note,
                )
            )
            with gr.Row():
                debrief_prev_btn = gr.Button(T("common.previous"), elem_id="debrief-prev")

        # Sticky bottom Event Log drawer
        eventlog_html = gr.HTML(
            value=_render_eventlog([f"enter_screen: login  —  {time.strftime('%H:%M:%S')}"], texts)
        )

        # Map of the 10 study screen containers in SCREENS_ORDER
        screen_containers = [
            screen_consent,
            screen_briefing,
            screen_check1,
            screen_team,
            screen_check2,
            screen_tutorial,
            screen_manipulation,
            screen_check3,
            screen_live,
            screen_debrief,
        ]

        def _screen_visibility_updates(target_screen: str) -> list:
            updates = []
            for scr_id in SCREENS_ORDER:
                base_cls = [] if scr_id == "live" else ["card"]
                if scr_id == target_screen:
                    updates.append(gr.update(elem_classes=base_cls))
                else:
                    updates.append(gr.update(elem_classes=[*base_cls, "screen-hidden"]))
            return updates

        def _tut_step_updates(active_step: int) -> list:
            return [
                gr.update(elem_classes=[] if i == active_step else ["screen-hidden"])
                for i in range(1, 5)
            ]

        def _ensure_study(
            study: StudySessionState | None,
            formatted_name: str = "Participant",
        ) -> StudySessionState:
            if study is not None:
                return study
            agent_names = [a.name for a in config.agents]
            logger = ConversationLogger(
                output_dir=config.logging.output_dir,
                scenario=config.ui.task_description,
                save_json=config.logging.save_json,
                agent_names=agent_names,
            )
            specialists = _specialist_names(config)
            random.shuffle(specialists)
            assigned_cond = random.choice(CONDITIONS)
            return StudySessionState(
                user_name=formatted_name,
                logger=logger,
                specialist_order=specialists,
                condition=assigned_cond,
            )

        # -------------------------------------------------------------------
        # Page 0 Handlers: Original Login -> Page 1 (Consent)
        # -------------------------------------------------------------------
        def on_login(username: str, password: str, study: StudySessionState | None):
            is_valid, formatted_name, error_msg = _validate_and_format_name(
                username, config.ui.max_username_length, texts
            )
            if not is_valid:
                gr.Warning(error_msg)
                return [
                    gr.update(),
                    gr.update(),
                    *[gr.update() for _ in screen_containers],
                    gr.update(),
                    study,
                ]
            if config.ui.password and password != config.ui.password:
                gr.Warning(T("login.errors.wrong_password"))
                return [
                    gr.update(),
                    gr.update(),
                    *[gr.update() for _ in screen_containers],
                    gr.update(),
                    study,
                ]

            if study is None:
                study = _ensure_study(None, formatted_name=formatted_name)
            else:
                study.user_name = formatted_name

            ev_html = _log_study_event(
                study, "enter_screen: consent", {"user_name": formatted_name}
            )
            return [
                gr.update(elem_classes=["screen-hidden"]),
                gr.update(
                    value=_topbar("consent", study),
                    elem_classes=[],
                ),
                *_screen_visibility_updates("consent"),
                ev_html,
                study,
            ]

        login_btn.click(
            fn=on_login,
            inputs=[username_input, password_input, study_state],
            outputs=[login_view, topbar_html, *screen_containers, eventlog_html, study_state],
            show_progress="hidden",
        )
        username_input.submit(
            fn=on_login,
            inputs=[username_input, password_input, study_state],
            outputs=[login_view, topbar_html, *screen_containers, eventlog_html, study_state],
            show_progress="hidden",
        )
        password_input.submit(
            fn=on_login,
            inputs=[username_input, password_input, study_state],
            outputs=[login_view, topbar_html, *screen_containers, eventlog_html, study_state],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 1 Handlers: Consent (Previous -> Login, Continue -> Briefing)
        # -------------------------------------------------------------------
        def on_consent_toggle(checked: bool):
            return gr.update(interactive=bool(checked))

        consent_checkbox.change(
            fn=on_consent_toggle,
            inputs=[consent_checkbox],
            outputs=[consent_continue_btn],
            queue=False,
        )

        def on_consent_prev(study: StudySessionState | None):
            ev_html = _log_study_event(study, "enter_screen: login") if study else gr.update()
            return [
                gr.update(elem_classes=[]),
                gr.update(value="", elem_classes=["screen-hidden"]),
                *[
                    gr.update(elem_classes=(["screen-hidden"] if scr_id == "live" else ["card", "screen-hidden"]))
                    for scr_id in SCREENS_ORDER
                ],
                ev_html,
                study,
            ]

        consent_prev_btn.click(
            fn=on_consent_prev,
            inputs=[study_state],
            outputs=[login_view, topbar_html, *screen_containers, eventlog_html, study_state],
            show_progress="hidden",
        )

        def on_enter_briefing(study: StudySessionState | None):
            """Switch to Page 2 (Briefing) and show the text, Previous, and Continue buttons."""
            study = _ensure_study(study)
            ev_html = _log_study_event(study, "enter_screen: briefing")
            return [
                *_screen_visibility_updates("briefing"),
                gr.update(
                    value=_topbar("briefing", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
            ]

        def on_consent_continue(consented: bool, study: StudySessionState | None):
            if not consented:
                gr.Warning(T("consent.warning_not_checked"))
                return [
                    *[gr.update() for _ in screen_containers],
                    gr.update(),
                    gr.update(),
                    study,
                ]
            study = _ensure_study(study)
            study.completed_screens.add("consent")
            return on_enter_briefing(study)

        consent_continue_btn.click(
            fn=on_consent_continue,
            inputs=[consent_checkbox, study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
            ],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 2 Handlers: Task Briefing (Previous -> Consent, Continue -> Check 1)
        # -------------------------------------------------------------------
        def on_briefing_prev(study: StudySessionState | None):
            study = _ensure_study(study)
            ev_html = _log_study_event(study, "enter_screen: consent")
            return [
                *_screen_visibility_updates("consent"),
                gr.update(
                    value=_topbar("consent", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
            ]

        briefing_prev_btn.click(
            fn=on_briefing_prev,
            inputs=[study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state],
            show_progress="hidden",
        )

        def on_briefing_continue(study: StudySessionState | None):
            study = _ensure_study(study)
            study.completed_screens.add("briefing")
            ev_html = _log_study_event(study, "enter_screen: check1")
            c1_updates = [
                gr.update(value=study.check1_answers[q["id"]])
                if study.check1_answers.get(q["id"]) is not None
                else gr.update()
                for q in CHECK1_QUESTIONS
            ]
            return [
                *_screen_visibility_updates("check1"),
                gr.update(
                    value=_topbar("check1", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                *c1_updates,
                "",
                gr.update(elem_classes=["screen-hidden"]),
            ]

        briefing_continue_btn.click(
            fn=on_briefing_continue,
            inputs=[study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                *c1_radios,
                check1_result_html,
                check1_review_btn,
            ],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 3 Handlers: Check 1 (Previous/Review -> Briefing, Submit -> Team)
        # -------------------------------------------------------------------
        def on_evaluate_check1(*args):
            *values, study = args
            study = _ensure_study(study)
            answers = {q["id"]: v for q, v in zip(CHECK1_QUESTIONS, values)}
            study.check1_answers.update(answers)
            any_missing = any(v is None for v in answers.values())
            all_correct = all(
                answers[q["id"]] == q["correct"] for q in CHECK1_QUESTIONS
            )
            study.attempts["check1"] += 1
            attempt_num = study.attempts["check1"]
            ev_html = _log_study_event(
                study,
                f"check1_attempt: {attempt_num} result:{'pass' if all_correct else 'fail'}",
                {"answers": answers},
            )

            if any_missing:
                return [
                    *[gr.update() for _ in screen_containers],
                    gr.update(),
                    ev_html,
                    study,
                    _result_box("warn", T("check1.messages.missing_html")),
                    gr.update(elem_classes=["screen-hidden"]),
                    gr.update(),
                ]

            if all_correct:
                study.completed_screens.add("check1")
                ev_html = _log_study_event(
                    study,
                    "enter_screen: team",
                    {"specialist_order": study.specialist_order},
                )
                return [
                    *_screen_visibility_updates("team"),
                    gr.update(
                        value=_topbar("team", study),
                        elem_classes=[],
                    ),
                    ev_html,
                    study,
                    _result_box("ok", T("check1.messages.correct_html")),
                    gr.update(elem_classes=["screen-hidden"]),
                    _render_team_cards_html(config, study.specialist_order),
                ]

            return [
                *[gr.update() for _ in screen_containers],
                gr.update(),
                ev_html,
                study,
                _result_box("warn", T("check1.messages.incorrect_html")),
                gr.update(elem_classes=[]),
                gr.update(),
            ]

        check1_submit_btn.click(
            fn=on_evaluate_check1,
            inputs=[*c1_radios, study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                check1_result_html,
                check1_review_btn,
                team_cards_html,
            ],
            show_progress="hidden",
        )

        def on_check1_back(*args):
            *values, study = args
            study = _ensure_study(study)
            study.check1_answers.update({q["id"]: v for q, v in zip(CHECK1_QUESTIONS, values)})
            return on_enter_briefing(study)

        check1_review_btn.click(
            fn=on_check1_back,
            inputs=[*c1_radios, study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
            ],
            show_progress="hidden",
        )

        check1_prev_btn.click(
            fn=on_check1_back,
            inputs=[*c1_radios, study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
            ],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 4 Handlers: Meet Your Team (Previous -> Check 1, Shuffle, Continue -> Check 2)
        # -------------------------------------------------------------------
        def on_team_prev(study: StudySessionState | None):
            study = _ensure_study(study)
            ev_html = _log_study_event(study, "enter_screen: check1")
            c1_updates = [
                gr.update(value=study.check1_answers[q["id"]])
                if study.check1_answers.get(q["id"]) is not None
                else gr.update()
                for q in CHECK1_QUESTIONS
            ]
            return [
                *_screen_visibility_updates("check1"),
                gr.update(
                    value=_topbar("check1", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                *c1_updates,
            ]

        team_prev_btn.click(
            fn=on_team_prev,
            inputs=[study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state, *c1_radios],
            show_progress="hidden",
        )

        def on_shuffle_team(study: StudySessionState | None):
            study = _ensure_study(study)
            random.shuffle(study.specialist_order)
            ev_html = _log_study_event(
                study, "team_order_shuffled", {"specialist_order": study.specialist_order}
            )
            return (
                _render_team_cards_html(config, study.specialist_order),
                ev_html,
                study,
            )

        team_shuffle_btn.click(
            fn=on_shuffle_team,
            inputs=[study_state],
            outputs=[team_cards_html, eventlog_html, study_state],
            show_progress="hidden",
        )

        def on_team_continue(study: StudySessionState | None):
            study = _ensure_study(study)
            study.completed_screens.add("team")
            ev_html = _log_study_event(study, "enter_screen: check2")
            c2_updates = [
                gr.update(value=study.check2_answers[q["id"]])
                if study.check2_answers.get(q["id"]) is not None
                else gr.update()
                for q in CHECK2_QUESTIONS
            ]
            return [
                *_screen_visibility_updates("check2"),
                gr.update(
                    value=_topbar("check2", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                *c2_updates,
                "",
                gr.update(elem_classes=["screen-hidden"]),
            ]

        team_continue_btn.click(
            fn=on_team_continue,
            inputs=[study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                *c2_radios,
                check2_result_html,
                check2_review_btn,
            ],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 5 Handlers: Check 2 (Previous/Review -> Team, Submit -> Tutorial)
        # -------------------------------------------------------------------
        def on_evaluate_check2(*args):
            *values, study = args
            study = _ensure_study(study)
            answers = {q["id"]: v for q, v in zip(CHECK2_QUESTIONS, values)}
            study.check2_answers.update(answers)
            any_missing = any(v is None for v in answers.values())
            all_correct = all(
                answers[q["id"]] == q["correct"] for q in CHECK2_QUESTIONS
            )
            study.attempts["check2"] += 1
            attempt_num = study.attempts["check2"]
            ev_html = _log_study_event(
                study,
                f"check2_attempt: {attempt_num} result:{'pass' if all_correct else 'fail'}",
                {"answers": answers},
            )

            if any_missing:
                return [
                    *[gr.update() for _ in screen_containers],
                    gr.update(),
                    ev_html,
                    study,
                    _result_box("warn", T("check2.messages.missing_html")),
                    gr.update(elem_classes=["screen-hidden"]),
                ]

            if all_correct:
                study.completed_screens.add("check2")
                _log_study_event(study, "enter_screen: tutorial")
                ev_html = _log_study_event(study, "tutorial_step_shown: 1")
                return [
                    *_screen_visibility_updates("tutorial"),
                    gr.update(
                        value=_topbar("tutorial", study),
                        elem_classes=[],
                    ),
                    ev_html,
                    study,
                    _result_box("ok", T("check2.messages.correct_html")),
                    gr.update(elem_classes=["screen-hidden"]),
                ]

            return [
                *[gr.update() for _ in screen_containers],
                gr.update(),
                ev_html,
                study,
                _result_box("warn", T("check2.messages.incorrect_html")),
                gr.update(elem_classes=[]),
            ]

        check2_submit_btn.click(
            fn=on_evaluate_check2,
            inputs=[*c2_radios, study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                check2_result_html,
                check2_review_btn,
            ],
            show_progress="hidden",
        )

        def on_check2_review(*args):
            *values, study = args
            study = _ensure_study(study)
            study.check2_answers.update({q["id"]: v for q, v in zip(CHECK2_QUESTIONS, values)})
            ev_html = _log_study_event(study, "enter_screen: team")
            return [
                *_screen_visibility_updates("team"),
                gr.update(
                    value=_topbar("team", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
            ]

        check2_review_btn.click(
            fn=on_check2_review,
            inputs=[*c2_radios, study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state],
            show_progress="hidden",
        )
        check2_prev_btn.click(
            fn=on_check2_review,
            inputs=[*c2_radios, study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 6 Handlers: Interactive 4-Step Tutorial
        # -------------------------------------------------------------------
        def on_tut1_prev(study: StudySessionState | None):
            study = _ensure_study(study)
            ev_html = _log_study_event(study, "enter_screen: check2")
            c2_updates = [
                gr.update(value=study.check2_answers[q["id"]])
                if study.check2_answers.get(q["id"]) is not None
                else gr.update()
                for q in CHECK2_QUESTIONS
            ]
            return [
                *_screen_visibility_updates("check2"),
                gr.update(
                    value=_topbar("check2", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                *c2_updates,
            ]

        tut1_prev_btn.click(
            fn=on_tut1_prev,
            inputs=[study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state, *c2_radios],
            show_progress="hidden",
        )

        for btn, ag_name in tut2_mention_btns:
            btn.click(
                fn=lambda cur, n=ag_name: (cur or "") + f"@{n} ",
                inputs=[tut2_input],
                outputs=[tut2_input],
                queue=False,
            )

        for btn, ag_name in tut3_mention_btns:
            btn.click(
                fn=lambda cur, n=ag_name: (cur or "") + f"@{n} ",
                inputs=[tut3_input],
                outputs=[tut3_input],
                queue=False,
            )

        user_initial = T("tutorial.user_initial")
        step2_agent = T("tutorial.step2.mention_agent")
        step3_agent = T("tutorial.step3.mention_agent")
        step3_word = T("tutorial.step3.required_word")

        def _switch_tut_step(step_num: int, study: StudySessionState | None):
            study = _ensure_study(study)
            ev_html = _log_study_event(study, f"tutorial_step_shown: {step_num}")
            return [
                _render_tut_dots(step_num),
                *_tut_step_updates(step_num),
                ev_html,
                study,
            ]

        def on_tut1_send(msg: str, study: StudySessionState | None):
            study = _ensure_study(study)
            if study.tut1_done:
                ev_html = _log_study_event(study, "tutorial_step_shown: 2")
                return [
                    gr.update(),
                    gr.update(),
                    _render_tut_dots(2),
                    *_tut_step_updates(2),
                    ev_html,
                    study,
                    gr.update(),
                ]
            v = (msg or "").strip()
            if not v:
                return [
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    study,
                    gr.update(),
                ]
            bubbles = (
                '<div class="tut-context">'
                + _tut_bubble_html(user_initial, v, is_self=True)
                + "".join(
                    _tut_bubble_html(r["initial"], r["text"])
                    for r in texts.get("tutorial.step1.replies")
                )
                + "</div>"
            )
            study.tut1_done = True
            ev_html = _log_study_event(study, "tutorial_step1_message_sent", {"text": v})
            return [
                bubbles,
                bubbles,
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                ev_html,
                study,
                gr.update(value=T("common.continue")),
            ]

        tut1_send_btn.click(
            fn=on_tut1_send,
            inputs=[tut1_input, study_state],
            outputs=[
                tut1_context_html,
                tut2_context_html,
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
                tut1_send_btn,
            ],
            show_progress="hidden",
        )
        tut1_input.submit(
            fn=on_tut1_send,
            inputs=[tut1_input, study_state],
            outputs=[
                tut1_context_html,
                tut2_context_html,
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
                tut1_send_btn,
            ],
            show_progress="hidden",
        )

        def on_tut2_send(msg: str, study: StudySessionState | None):
            study = _ensure_study(study)
            if study.tut2_done:
                ev_html = _log_study_event(study, "tutorial_step_shown: 3")
                return [
                    gr.update(),
                    gr.update(),
                    "",
                    _render_tut_dots(3),
                    *_tut_step_updates(3),
                    ev_html,
                    study,
                    gr.update(),
                ]
            v = (msg or "").strip()
            if not re.match(rf"^@{re.escape(step2_agent)}\b", v, flags=re.IGNORECASE):
                return [
                    gr.update(),
                    gr.update(),
                    f'<div class="warn-line">{T("tutorial.step2.warning_html", agent=step2_agent)}</div>',
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    study,
                    gr.update(),
                ]
            bubbles = (
                '<div class="tut-context">'
                + _tut_bubble_html(user_initial, v, is_self=True)
                + "".join(
                    _tut_bubble_html(r["initial"], r["text"])
                    for r in texts.get("tutorial.step2.replies")
                )
                + "</div>"
            )
            study.tut2_done = True
            ev_html = _log_study_event(study, "tutorial_step2_mention_success", {"text": v})
            return [
                bubbles,
                bubbles,
                "",
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                ev_html,
                study,
                gr.update(value=T("common.continue")),
            ]

        tut2_send_btn.click(
            fn=on_tut2_send,
            inputs=[tut2_input, study_state],
            outputs=[
                tut2_context_html,
                tut3_context_html,
                tut2_warn_html,
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
                tut2_send_btn,
            ],
            show_progress="hidden",
        )
        tut2_input.submit(
            fn=on_tut2_send,
            inputs=[tut2_input, study_state],
            outputs=[
                tut2_context_html,
                tut3_context_html,
                tut2_warn_html,
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
                tut2_send_btn,
            ],
            show_progress="hidden",
        )

        def on_tut3_send(msg: str, study: StudySessionState | None):
            study = _ensure_study(study)
            if study.tut3_done:
                ev_html = _log_study_event(study, "tutorial_step_shown: 4")
                return [
                    gr.update(),
                    "",
                    _render_tut_dots(4),
                    *_tut_step_updates(4),
                    ev_html,
                    study,
                    gr.update(),
                ]
            raw = (msg or "").strip()
            v = raw.lower()
            if f"@{step3_agent.lower()}" not in v or step3_word.lower() not in v:
                return [
                    gr.update(),
                    f'<div class="warn-line">{T("tutorial.step3.warning_html", agent=step3_agent, word=step3_word)}</div>',
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    study,
                    gr.update(),
                ]
            picasso_sketch_html = (
                T("tutorial.step3.reply_html")
                + '<div style="margin-top:6px; width:140px; height:90px; background:var(--surface-2); '
                "border:1px dashed var(--border-strong); border-radius:6px; display:flex; "
                'align-items:center; justify-content:center; font-size:11px; color:var(--text-secondary);">'
                + html_mod.escape(T("tutorial.step3.image_placeholder_text"))
                + "</div>"
            )
            bubbles = (
                '<div class="tut-context">'
                + _tut_bubble_html(user_initial, raw, is_self=True)
                + _tut_bubble_html(T("tutorial.step3.reply_initial"), "", raw_html=picasso_sketch_html)
                + "</div>"
            )
            study.tut3_done = True
            ev_html = _log_study_event(study, "tutorial_step3_image_generated", {"text": raw})
            return [
                bubbles,
                "",
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                ev_html,
                study,
                gr.update(value=T("common.continue")),
            ]

        tut3_send_btn.click(
            fn=on_tut3_send,
            inputs=[tut3_input, study_state],
            outputs=[
                tut3_context_html,
                tut3_warn_html,
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
                tut3_send_btn,
            ],
            show_progress="hidden",
        )
        tut3_input.submit(
            fn=on_tut3_send,
            inputs=[tut3_input, study_state],
            outputs=[
                tut3_context_html,
                tut3_warn_html,
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
                tut3_send_btn,
            ],
            show_progress="hidden",
        )

        tut2_back_btn.click(
            fn=lambda s: _switch_tut_step(1, s),
            inputs=[study_state],
            outputs=[
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
            ],
            show_progress="hidden",
        )
        tut3_back_btn.click(
            fn=lambda s: _switch_tut_step(2, s),
            inputs=[study_state],
            outputs=[
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
            ],
            show_progress="hidden",
        )
        tut4_back_btn.click(
            fn=lambda s: _switch_tut_step(3, s),
            inputs=[study_state],
            outputs=[
                tut_progress_html,
                tut_step_1_col,
                tut_step_2_col,
                tut_step_3_col,
                tut_step_4_col,
                eventlog_html,
                study_state,
            ],
            show_progress="hidden",
        )

        def on_tut4_pause(study: StudySessionState | None):
            study = _ensure_study(study)
            if study.tut4_paused:
                study.tut4_paused = False
                ev_html = _log_study_event(study, "tutorial_step4_resumed")
                return (
                    gr.update(value=T("tutorial.step4.pause_button")),
                    _result_box("ok", T("tutorial.step4.resumed_note_html")),
                    gr.update(interactive=True),
                    ev_html,
                    study,
                )
            study.tut4_paused = True
            ev_html = _log_study_event(study, "tutorial_step4_pause_used")
            return (
                gr.update(value=T("tutorial.step4.resume_button")),
                _result_box("ok", T("tutorial.step4.paused_note_html")),
                gr.update(interactive=True),
                ev_html,
                study,
            )

        def on_tut4_typing(text: str, study: StudySessionState | None):
            study = _ensure_study(study)
            if (text or "").strip() and not study.tut4_paused:
                study.tut4_paused = True
                ev_html = _log_study_event(study, "tutorial_step4_pause_used")
                return (
                    gr.update(value=T("tutorial.step4.resume_button")),
                    _result_box("ok", T("tutorial.step4.paused_note_html")),
                    gr.update(interactive=True),
                    ev_html,
                    study,
                )
            return gr.update(), gr.update(), gr.update(), gr.update(), study

        tut4_pause_btn.click(
            fn=on_tut4_pause,
            inputs=[study_state],
            outputs=[tut4_pause_btn, tut4_paused_note_html, tut4_continue_btn, eventlog_html, study_state],
            show_progress="hidden",
        )
        tut4_input.change(
            fn=on_tut4_typing,
            inputs=[tut4_input, study_state],
            outputs=[tut4_pause_btn, tut4_paused_note_html, tut4_continue_btn, eventlog_html, study_state],
            show_progress="hidden",
        )

        def on_tut4_continue(study: StudySessionState | None):
            study = _ensure_study(study)
            study.completed_screens.add("tutorial")
            cond = study.condition
            _log_study_event(study, "enter_screen: manipulation")
            ev_html = _log_study_event(
                study, f"condition_assigned: {cond['name']}", {"condition_id": cond["id"]}
            )
            needs_reflection = bool(cond["reflection"])
            is_ready = (not needs_reflection) or (
                len(study.reflection_text.strip()) >= config.ui.min_reflection_length
            )
            return [
                *_screen_visibility_updates("manipulation"),
                gr.update(
                    value=_topbar("manipulation", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                _render_condition_html(cond),
                gr.update(elem_classes=[] if needs_reflection else ["screen-hidden"]),
                gr.update(value=study.reflection_text),
                gr.update(interactive=is_ready),
            ]

        tut4_continue_btn.click(
            fn=on_tut4_continue,
            inputs=[study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                cond_display_html,
                cond_reflection_col,
                cond_reflection_input,
                cond_continue_btn,
            ],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 7 Handlers: Leadership Role Manipulation (Previous -> Tutorial)
        # -------------------------------------------------------------------
        def on_cond_prev(study: StudySessionState | None):
            study = _ensure_study(study)
            ev_html = _log_study_event(study, "enter_screen: tutorial")
            return [
                *_screen_visibility_updates("tutorial"),
                gr.update(
                    value=_topbar("tutorial", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
            ]

        cond_prev_btn.click(
            fn=on_cond_prev,
            inputs=[study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state],
            show_progress="hidden",
        )

        def on_reassign_condition(study: StudySessionState | None):
            study = _ensure_study(study)
            cond = random.choice(CONDITIONS)
            study.condition = cond
            study.reflection_text = ""
            ev_html = _log_study_event(
                study, f"condition_assigned: {cond['name']}", {"condition_id": cond["id"]}
            )
            needs_reflection = bool(cond["reflection"])
            return (
                _render_condition_html(cond),
                gr.update(elem_classes=[] if needs_reflection else ["screen-hidden"]),
                gr.update(value=""),
                gr.update(interactive=(not needs_reflection)),
                ev_html,
                study,
            )

        cond_reassign_btn.click(
            fn=on_reassign_condition,
            inputs=[study_state],
            outputs=[
                cond_display_html,
                cond_reflection_col,
                cond_reflection_input,
                cond_continue_btn,
                eventlog_html,
                study_state,
            ],
            show_progress="hidden",
        )

        def on_reflection_change(text: str, study: StudySessionState | None):
            if study is None or not study.condition.get("reflection"):
                return gr.update(interactive=True)
            is_ready = len((text or "").strip()) >= config.ui.min_reflection_length
            return gr.update(interactive=is_ready)

        cond_reflection_input.change(
            fn=on_reflection_change,
            inputs=[cond_reflection_input, study_state],
            outputs=[cond_continue_btn],
            queue=False,
        )

        def on_cond_continue(reflection_text: str, study: StudySessionState | None):
            study = _ensure_study(study)
            study.reflection_text = (reflection_text or "").strip()
            if study.condition.get("reflection") and len(study.reflection_text) < config.ui.min_reflection_length:
                gr.Warning(T("manipulation.warning_reflection_too_short", min_length=config.ui.min_reflection_length))
                return [
                    *[gr.update() for _ in screen_containers],
                    gr.update(),
                    gr.update(),
                    study,
                    *[gr.update() for _ in likert_radios],
                    gr.update(),
                ]
            study.completed_screens.add("manipulation")
            if study.reflection_text:
                _log_study_event(
                    study,
                    "condition_reflection_submitted",
                    {"condition": study.condition["name"], "reflection": study.reflection_text},
                )
            ev_html = _log_study_event(study, "enter_screen: check3")
            lk_updates = [
                gr.update(value=str(study.likert_responses[LIKERT_ITEMS[i]["id"]]))
                if study.likert_responses.get(LIKERT_ITEMS[i]["id"]) is not None
                else gr.update()
                for i in range(len(LIKERT_ITEMS))
            ]
            return [
                *_screen_visibility_updates("check3"),
                gr.update(
                    value=_topbar("check3", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                *lk_updates,
                "",
            ]

        cond_continue_btn.click(
            fn=on_cond_continue,
            inputs=[cond_reflection_input, study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                *likert_radios,
                check3_result_html,
            ],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 8 Handlers: Check 3 (Previous -> Manipulation, Continue -> Live)
        # -------------------------------------------------------------------
        def on_check3_prev(study: StudySessionState | None):
            study = _ensure_study(study)
            ev_html = _log_study_event(study, "enter_screen: manipulation")
            return [
                *_screen_visibility_updates("manipulation"),
                gr.update(
                    value=_topbar("manipulation", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
            ]

        check3_prev_btn.click(
            fn=on_check3_prev,
            inputs=[study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state],
            show_progress="hidden",
        )

        def on_evaluate_check3(*args):
            *lk_vals, study, sim = args
            study = _ensure_study(study)
            missing = sum(1 for v in lk_vals if v is None)
            if missing > 0:
                return [
                    *[gr.update() for _ in screen_containers],
                    gr.update(),
                    gr.update(),
                    study,
                    sim,
                    _result_box("warn", T("check3.missing_html", missing=missing)),
                    gr.update(),
                ]

            study.completed_screens.add("check3")
            responses = {LIKERT_ITEMS[i]["id"]: int(v) for i, v in enumerate(lk_vals)}
            study.likert_responses = responses
            if study.live_start_time is None:
                study.live_start_time = time.time()
            _log_study_event(study, "reflection_scale_completed", {"responses": responses})
            ev_html = _log_study_event(study, "enter_screen: live")

            if sim is None:
                sim = Simulation(
                    config,
                    user_name=study.user_name,
                    logger=study.logger,
                )

            greeting = random.choice(config.ui.greetings) if config.ui.greetings else "Hello"
            welcome_text = T("live.welcome", greeting=greeting, user_name=study.user_name)

            return [
                *_screen_visibility_updates("live"),
                gr.update(
                    value=_topbar("live", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                sim,
                _result_box("ok", T("check3.complete_html")),
                gr.update(value=welcome_text, elem_classes=["text-center"]),
            ]

        check3_continue_btn.click(
            fn=on_evaluate_check3,
            inputs=[*likert_radios, study_state, sim_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                sim_state,
                check3_result_html,
                welcome_md,
            ],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 9 Handlers: Live Task, Multi-Agent Chat, Poster Save, Previous/Finish
        # -------------------------------------------------------------------
        def on_live_prev(state: Simulation | None, study: StudySessionState | None):
            study = _ensure_study(study)
            if state is not None:
                state.cancel()
            ev_html = _log_study_event(study, "enter_screen: check3")
            return [
                *_screen_visibility_updates("check3"),
                gr.update(
                    value=_topbar("check3", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
            ]

        live_prev_btn.click(
            fn=on_live_prev,
            inputs=[sim_state, study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state],
            show_progress="hidden",
        )

        def _agent_html(color: str, name: str, text: str) -> str:
            return (
                f'<div style="background:{color};border:2px solid {color};padding:6px 10px;border-radius:6px;color:var(--ink,#211f1c);">'
                f'<strong class="chat-name">{html_mod.escape(name)}</strong>: {html_mod.escape(text)}'
                f"</div>"
            )

        def _user_html(name: str, text: str) -> str:
            color = config.ui.user_chat_color
            return (
                f'<div style="background:{color};border:2px solid {color};padding:6px 10px;border-radius:6px;color:var(--ink,#211f1c);">'
                f'<strong class="chat-name">{html_mod.escape(name)}</strong>: {html_mod.escape(text)}'
                f"</div>"
            )

        def _on_chatbot_select(evt: gr.SelectData, state: Simulation | None, history: list):
            if not state:
                return history
            msg_text = evt.value
            if isinstance(msg_text, str) and "![Generated Image]" in msg_text:
                match = re.search(r"!\[.*?\]\((.*?)\)", msg_text)
                if match:
                    url = match.group(1)
                    import urllib.request
                    out_path = state._logger._output_dir / f"{state._logger._session_id}.jpg"
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req) as response, open(out_path, "wb") as out_file:
                            out_file.write(response.read())
                        gr.Info(T("live.poster_saved", file=out_path.name))

                        if isinstance(evt.index, (list, tuple)):
                            index_key = tuple(evt.index)
                            row, col = index_key
                            current_content = history[row][col]
                        else:
                            index_key = evt.index
                            current_content = history[index_key]["content"]

                        if isinstance(current_content, list):
                            is_saved = any(
                                isinstance(item, dict) and item.get("type") == "text" and SAVED_BADGE in item.get("text", "")
                                for item in current_content
                            )
                        else:
                            is_saved = isinstance(current_content, str) and SAVED_BADGE in current_content

                        if not is_saved:
                            if hasattr(state, "_last_saved_index") and getattr(state, "_last_saved_index") is not None:
                                old_idx = state._last_saved_index
                                old_content = getattr(state, "_last_saved_content", None)
                                if old_content is not None and old_idx != index_key:
                                    if isinstance(old_idx, tuple) and isinstance(history[old_idx[0]], tuple):
                                        old_row, old_col = old_idx
                                        new_row_list = list(history[old_row])
                                        new_row_list[old_col] = old_content
                                        history[old_row] = tuple(new_row_list)
                                    elif isinstance(old_idx, int) and isinstance(history[old_idx], dict):
                                        history[old_idx]["content"] = old_content

                            state._last_saved_index = index_key
                            state._last_saved_content = current_content

                            badge_html = (
                                "<div style='margin-bottom: 8px;'>"
                                "<span style='background-color: #28a745; color: #ffffff !important; "
                                "padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 0.9em;'>"
                                f"{html_mod.escape(SAVED_BADGE)}</span></div>\n"
                            )

                            import copy
                            if isinstance(current_content, list):
                                new_content = copy.deepcopy(current_content)
                                for item in new_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        item["text"] = badge_html + item.get("text", "")
                                        break
                                else:
                                    new_content.insert(0, {"type": "text", "text": badge_html})
                            elif isinstance(current_content, str):
                                new_content = badge_html + current_content
                            else:
                                new_content = current_content

                            if isinstance(index_key, tuple):
                                if isinstance(history[row], tuple):
                                    new_row_list = list(history[row])
                                    new_row_list[col] = new_content
                                    history[row] = tuple(new_row_list)
                                else:
                                    history[row][col] = new_content
                            else:
                                history[index_key]["content"] = new_content

                    except Exception as e:
                        gr.Warning(T("live.poster_save_failed", error=e))
            return history

        def _apply_stream(events, history):
            """Process stream events and yield (txt_placeholder, history) tuples."""
            current_agent: str | None = None
            current_text: str = ""
            current_color: str = "#e8f4f8"

            for event in events:
                if event.event_type == "start":
                    current_agent = getattr(event, "agent_display_name", "") or event.agent_name
                    current_text = ""
                    current_color = agent_colors.get(event.agent_name, "#e8f4f8")
                    history.append({
                        "role": "assistant",
                        "content": _agent_html(current_color, current_agent, current_text),
                    })
                    yield None, history

                elif event.event_type == "chunk":
                    current_text += event.text
                    history[-1]["content"] = _agent_html(
                        current_color, current_agent or "", current_text
                    )
                    yield None, history

                elif event.event_type == "replace":
                    current_agent = getattr(event, "agent_display_name", "") or event.agent_name
                    current_text = event.text
                    current_color = agent_colors.get(event.agent_name, "#e8f4f8")
                    history[-1]["content"] = _agent_html(
                        current_color, current_agent, current_text
                    )
                    yield None, history

                elif event.event_type == "skip":
                    if history and history[-1]["role"] == "assistant":
                        history.pop()
                    current_agent = None
                    current_text = ""
                    yield None, history

        def on_typing_change(text: str, state: Simulation | None, paused: bool):
            """Called on every keystroke — switches the session to Pause mode while the user types.

            Agents may finish the sentence they are currently typing; no new turn starts
            until the user sends the message or clicks Resume.
            """
            if state is None:
                return gr.update(), paused
            state.set_user_typing(bool(text))
            if text and not paused:
                state.pause()
                return (
                    gr.update(value=T("live.resume_button"), elem_classes=["paused"]),
                    True,
                )
            return gr.update(), paused

        def on_pause_toggle(state: Simulation | None, paused: bool):
            """Toggle pause/resume when the Pause/Resume button is clicked."""
            if state is None:
                return gr.update(), paused
            if paused:
                state.reset_typing_state()
                state.resume()
                return (
                    gr.update(value=T("live.pause_button"), elem_classes=[]),
                    False,
                )
            else:
                state.pause()
                return (
                    gr.update(value=T("live.resume_button"), elem_classes=["paused"]),
                    True,
                )

        def on_interrupt(state: Simulation | None):
            """Fires immediately (queue=False) when the user sends a message."""
            if state is not None:
                state.cancel()
            return (
                gr.update(value=T("live.pause_button"), elem_classes=[]),
                False,
            )

        def on_chat(
            user_message: str,
            history: list[dict],
            state: Simulation | None,
            study: StudySessionState | None,
        ):
            study = _ensure_study(study)
            user_name = study.user_name
            if state is None:
                state = Simulation(
                    config,
                    user_name=user_name,
                    logger=study.logger,
                )
                history = []

            if not (user_message or "").strip():
                ev_str = _render_eventlog(study.events, texts)
                yield gr.update(value="", placeholder=_DEFAULT_PLACEHOLDER), history, state, ev_str, study
                return

            ev_html = _log_study_event(study, "live_message_sent")

            history.append({"role": "user", "content": _user_html(user_name, user_message)})
            yield (
                gr.update(value="", placeholder=_DEFAULT_PLACEHOLDER),
                history,
                state,
                ev_html,
                study,
            )

            for ph, h in _apply_stream(
                state.step_user_stream(user_message), history,
            ):
                yield (
                    gr.update(placeholder=ph) if ph else gr.update(),
                    h,
                    state,
                    gr.update(),
                    study,
                )

            yield (
                gr.update(),
                history,
                state,
                gr.update(),
                study,
            )

        for btn, agent_name in mention_btns:
            def make_append_fn(name):
                def append_mention(current_text):
                    current_text = current_text or ""
                    return current_text + f"@{name} "
                return append_mention

            btn.click(
                fn=make_append_fn(agent_name),
                inputs=[txt],
                outputs=[txt],
                queue=False,
                show_progress="hidden",
            )

        txt.change(
            fn=on_typing_change,
            inputs=[txt, sim_state, paused_state],
            outputs=[pause_btn, paused_state],
            queue=False,
            show_progress="hidden",
        )
        pause_btn.click(
            fn=on_pause_toggle,
            inputs=[sim_state, paused_state],
            outputs=[pause_btn, paused_state],
            queue=False,
            show_progress="hidden",
        )
        send_btn.click(
            fn=on_interrupt,
            inputs=[sim_state],
            outputs=[pause_btn, paused_state],
            queue=False,
            show_progress="hidden",
        )
        send_btn.click(
            fn=on_chat,
            inputs=[txt, chatbot, sim_state, study_state],
            outputs=[txt, chatbot, sim_state, eventlog_html, study_state],
            show_progress="hidden",
            trigger_mode="always_last",
        )
        txt.submit(
            fn=on_interrupt,
            inputs=[sim_state],
            outputs=[pause_btn, paused_state],
            queue=False,
            show_progress="hidden",
        )
        txt.submit(
            fn=on_chat,
            inputs=[txt, chatbot, sim_state, study_state],
            outputs=[txt, chatbot, sim_state, eventlog_html, study_state],
            show_progress="hidden",
            trigger_mode="always_last",
        )

        chatbot.select(
            fn=_on_chatbot_select,
            inputs=[sim_state, chatbot],
            outputs=[chatbot],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Page 9 -> Page 10: Finish Task & Debrief (plus Previous -> Live)
        # -------------------------------------------------------------------
        def on_finish_task(state: Simulation | None, study: StudySessionState | None):
            study = _ensure_study(study)
            study.completed_screens.add("live")
            if state is not None:
                state.cancel()
            session_id = "DEMO123"
            if study.logger is not None:
                session_id = study.logger.session_id
            elapsed = (
                int(time.time() - study.live_start_time)
                if study.live_start_time
                else 0
            )
            _log_study_event(
                study,
                "live_task_finished",
                {"elapsed_seconds": elapsed},
            )
            ev_html = _log_study_event(study, "enter_screen: debrief")

            survey_url = config.ui.exit_survey_url.format(session_id=session_id)
            return [
                *_screen_visibility_updates("debrief"),
                gr.update(
                    value=_topbar("debrief", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                _render_debrief_card(texts, survey_url, show_prototype_note=config.ui.show_prototype_note),
            ]

        finish_task_btn.click(
            fn=on_finish_task,
            inputs=[sim_state, study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                debrief_html,
            ],
            show_progress="hidden",
        )

        def on_debrief_prev(study: StudySessionState | None):
            study = _ensure_study(study)
            ev_html = _log_study_event(study, "enter_screen: live")
            return [
                *_screen_visibility_updates("live"),
                gr.update(
                    value=_topbar("live", study),
                    elem_classes=[],
                ),
                ev_html,
                study,
            ]

        debrief_prev_btn.click(
            fn=on_debrief_prev,
            inputs=[study_state],
            outputs=[*screen_containers, topbar_html, eventlog_html, study_state],
            show_progress="hidden",
        )

        # -------------------------------------------------------------------
        # Topbar Completed-Step Direct Navigation Handler
        # -------------------------------------------------------------------
        def on_topbar_nav(
            target_raw: str,
            state: Simulation | None,
            study: StudySessionState | None,
        ):
            study = _ensure_study(study)
            target = (target_raw or "").split(":")[0].strip()
            if target not in SCREENS_ORDER or (
                target not in study.completed_screens
                and target not in study.unlocked_screens
                and target != study.current_screen
            ):
                return [
                    *[gr.update() for _ in screen_containers],
                    gr.update(),
                    gr.update(),
                    study,
                    *[gr.update() for _ in c1_radios],
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    *[gr.update() for _ in c2_radios],
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    gr.update(),
                    *[gr.update() for _ in likert_radios],
                    gr.update(),
                ]

            if state is not None and target != "live":
                state.cancel()

            ev_html = _log_study_event(study, f"enter_screen: {target} (via_topbar)")
            c1_updates = [
                gr.update(value=study.check1_answers[q["id"]])
                if study.check1_answers.get(q["id"]) is not None
                else gr.update()
                for q in CHECK1_QUESTIONS
            ]
            c2_updates = [
                gr.update(value=study.check2_answers[q["id"]])
                if study.check2_answers.get(q["id"]) is not None
                else gr.update()
                for q in CHECK2_QUESTIONS
            ]
            cond = study.condition
            needs_reflection = bool(cond["reflection"])
            is_ready = (not needs_reflection) or (
                len(study.reflection_text.strip()) >= config.ui.min_reflection_length
            )
            lk_updates = [
                gr.update(value=str(study.likert_responses[LIKERT_ITEMS[i]["id"]]))
                if study.likert_responses.get(LIKERT_ITEMS[i]["id"]) is not None
                else gr.update()
                for i in range(len(LIKERT_ITEMS))
            ]

            return [
                *_screen_visibility_updates(target),
                gr.update(
                    value=_topbar(target, study),
                    elem_classes=[],
                ),
                ev_html,
                study,
                *c1_updates,
                "",
                gr.update(elem_classes=["screen-hidden"]),
                _render_team_cards_html(config, study.specialist_order),
                *c2_updates,
                "",
                gr.update(elem_classes=["screen-hidden"]),
                _render_condition_html(cond),
                gr.update(elem_classes=[] if needs_reflection else ["screen-hidden"]),
                gr.update(value=study.reflection_text) if study.reflection_text else gr.update(),
                gr.update(interactive=is_ready) if study.reflection_text or not needs_reflection else gr.update(),
                *lk_updates,
                "",
            ]

        topbar_nav_btn.click(
            fn=on_topbar_nav,
            inputs=[topbar_nav_input, sim_state, study_state],
            outputs=[
                *screen_containers,
                topbar_html,
                eventlog_html,
                study_state,
                *c1_radios,
                check1_result_html,
                check1_review_btn,
                team_cards_html,
                *c2_radios,
                check2_result_html,
                check2_review_btn,
                cond_display_html,
                cond_reflection_col,
                cond_reflection_input,
                cond_continue_btn,
                *likert_radios,
                check3_result_html,
            ],
            show_progress="hidden",
        )

        # Client-side JS for topbar button clicks, live elapsed timer, and @mention autocomplete
        client_js = """
        () => {
            // Delegated click handler for completed/current topbar step buttons
            if (!window.__topbarNavAttached) {
                window.__topbarNavAttached = true;
                document.addEventListener('click', (e) => {
                    const stepBtn = e.target.closest('.progress button.step.completed, .progress button.step.active');
                    if (!stepBtn || stepBtn.disabled) return;
                    const targetScreen = stepBtn.getAttribute('data-screen');
                    if (!targetScreen) return;
                    const navInput = document.querySelector('#topbar-nav-input textarea, #topbar-nav-input input');
                    const navBtn = document.getElementById('topbar-nav-btn');
                    if (!navInput || !navBtn) return;
                    const proto = navInput.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(navInput, targetScreen + ':' + Date.now());
                    navInput.dispatchEvent(new Event('input', { bubbles: true }));
                    navInput.dispatchEvent(new Event('change', { bubbles: true }));
                    setTimeout(() => { navBtn.click(); }, 30);
                });
            }

            // Live task elapsed timer (MM:SS)
            let liveSeconds = 0;
            setInterval(() => {
                const liveScreen = document.getElementById('screen-live');
                const timerEl = document.getElementById('live-timer');
                if (liveScreen && timerEl && liveScreen.offsetParent !== null) {
                    liveSeconds += 1;
                    const m = Math.floor(liveSeconds / 60);
                    const s = liveSeconds % 60;
                    timerEl.innerText = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
                }
            }, 1000);

            // @mention autocomplete dropdown
            const agents = __MENTION_AGENTS__;
            const targetIds = ['tut2-input', 'tut3-input', 'txt-input'];
            const attachMention = (wrapId) => {
                const wrap = document.getElementById(wrapId);
                if (!wrap || wrap.dataset.mentionAttached) return;
                const input = wrap.querySelector('textarea, input');
                if (!input) return;
                wrap.dataset.mentionAttached = '1';
                wrap.style.position = 'relative';
                wrap.style.overflow = 'visible';
                const drop = document.createElement('div');
                drop.className = 'mention-drop';
                drop.style.display = 'none';
                wrap.appendChild(drop);

                input.addEventListener('input', () => {
                    const v = input.value;
                    const at = v.lastIndexOf('@');
                    if (at === -1) { drop.style.display = 'none'; return; }
                    const frag = v.slice(at + 1).toLowerCase();
                    if (frag.indexOf(' ') !== -1) { drop.style.display = 'none'; return; }
                    const matches = agents.filter(a => a.name.toLowerCase().indexOf(frag) === 0);
                    if (matches.length === 0) { drop.style.display = 'none'; return; }
                    drop.innerHTML = matches.map(a =>
                        `<div class="opt" data-name="${a.name}"><div class="avatar">${a.initial}</div>${a.name} <span style="color:var(--text-muted); font-size:11px;">— ${a.role}</span></div>`
                    ).join('');
                    drop.querySelectorAll('.opt').forEach(opt => {
                        opt.addEventListener('mousedown', (e) => {
                            e.preventDefault();
                            const name = opt.getAttribute('data-name');
                            input.value = v.slice(0, at + 1) + name + ' ';
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            drop.style.display = 'none';
                            input.focus();
                        });
                    });
                    drop.style.display = 'block';
                });
                input.addEventListener('blur', () => {
                    setTimeout(() => { drop.style.display = 'none'; }, 150);
                });
            };
            setInterval(() => { targetIds.forEach(attachMention); }, 500);
        }
        """
        mention_agents = [
            {"name": a.name, "role": a.title or a.role, "initial": a.name[:1].upper()}
            for a in config.agents
        ]
        client_js = client_js.replace("__MENTION_AGENTS__", json.dumps(mention_agents))
        demo.load(fn=None, inputs=None, outputs=None, js=client_js)

    return demo, css


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-Agent Behavioral Simulation",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to the YAML configuration file.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    share = config.ui.share

    # Pre-initialize OpenAI clients in the background so the first user
    # interaction doesn't pay the HTTP/TLS setup cost.
    threading.Thread(target=prewarm_clients, args=(config,), daemon=True).start()

    demo, css = build_ui(config)

    if share:
        def _post_launch_update() -> None:
            for _ in range(60):
                time.sleep(1)
                url = getattr(demo, "share_url", None)
                if url:
                    print(f"[share] Public URL: {url}")
                    _update_google_sheets(url, config)
                    break

        threading.Thread(target=_post_launch_update, daemon=True).start()

    import os
    working_dir = os.path.abspath(".")

    demo.launch(
        server_port=config.ui.server_port,
        share=share,
        css=css,
        allowed_paths=[working_dir],
    )


if __name__ == "__main__":
    main()