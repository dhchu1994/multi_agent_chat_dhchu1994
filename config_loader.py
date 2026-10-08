"""
Loads and validates the YAML configuration file into typed dataclasses.

Besides the API, orchestrator, scenario, agent, logging and UI settings, the
`screens` section of config.yaml (all participant-facing text of the study
flow) is exposed through the ScreenTexts helper, so main.py never contains
user-visible wording itself.

Usage:
    from config_loader import load_config
    cfg = load_config("config.yaml")
    cfg.screens.text("consent.title")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Dataclasses mirroring the YAML structure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApiConfig:
    base_url: str = ""
    api_key: str = ""
    organization: str | None = None
    timeout: int = 60
    agent_model: str = ""
    orchestrator_model: str = ""


@dataclass(frozen=True)
class ImageApiConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-image-1"
    dummy_image_url: str = ""





@dataclass(frozen=True)
class OrchestratorConfig:
    max_agent_turns: int = 0
    temperature: float = 0.0
    max_tokens: int = 0
    context_window: int = 0
    agent_context_window: int = 40
    response_threshold: float = 0.0
    min_responders: int = 0
    max_orchestrator_rounds: int = 0
    # Agents whose LLM-assigned probability meets or exceeds this value are
    # always included regardless of talkativeness (directly addressed or
    # role-critical).
    high_relevance_threshold: float = 0.0
    # Global dampener applied to every non-high-relevance agent's talkativeness
    # to make agents slightly less vocal overall.
    base_vocal_factor: float = 0.0
    # Full system prompt template sent to the orchestrator LLM. Supports
    # {user_name}, {last_speaker}, {response_threshold}, {max_agent_turns},
    # and {min_responders} placeholders.
    system_prompt: str = ""


@dataclass(frozen=True)
class ScenarioConfig:
    # Suffix appended to every agent's system prompt at runtime.
    # Supports the {user_name} placeholder.
    agent_extra_system_prompt: str = ""


@dataclass(frozen=True)
class AgentEntry:
    name: str
    system_prompt: str
    role: str = ""
    title: str = ""
    description: str = ""
    talkativeness: float = 0.5
    temperature: float = 0.7
    max_tokens: int = 256
    chat_color: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LoggingConfig:
    output_dir: str = ""
    save_json: bool = False


@dataclass(frozen=True)
class UiConfig:
    title: str = ""
    chatbot_height: int = 0
    task_panel_height: int | float | str = 560
    task_panel_width: int | float | str = 18
    agents_panel_height: int | float | str = 340
    agents_panel_width: int | float | str = 18
    tips_panel_height: int | float | str = 200
    tips_panel_width: int | float | str = 18
    server_port: int = 0
    share: bool = False
    # Seconds to wait after the user clears the typing box (textbox becomes
    # empty without submitting) before the LLM agents resume their turns.
    user_typing_pause_seconds: float = 0.0
    # True delay (seconds) agents wait before showing the typing indicator and
    # making the LLM API call, to simulate a natural response time.
    true_delay_min: float = 0.0
    true_delay_max: float = 0.0
    # Live-typing speed: seconds to pause between each streamed chunk appearing
    # in the chat bubble. Higher = slower, more readable typing effect.
    live_typing_delay: float = 0.0
    # Password for login screen.
    password: str = ""
    # Colour of the chatbot border animation shown while the simulation is paused.
    pause_border_color: str = ""
    pause_animation_duration_seconds: float = 0.0
    # Default text in the message input box.
    input_placeholder: str = "Type your message..."
    max_username_length: int = 16
    user_chat_color: str = "#e9eefc"
    show_task_description: bool = True
    task_description_title: str = "📋 Task Description"
    task_description: str = ""
    greetings: list[str] = field(default_factory=list)
    backup_names: list[str] = field(default_factory=list)
    show_prototype_note: bool = True
    min_reflection_length: int = 20
    exit_survey_url: str = "https://example.qualtrics.com/jfe/form/SV_demo?participant_id={session_id}"


class ScreenTexts:
    """Read-only access to the `screens` section of config.yaml by dotted path.

    Example: ``texts.get("check1.questions")`` or ``texts.text("common.previous")``.
    Missing keys raise a KeyError that names the full path, so a typo or an
    accidentally deleted entry in config.yaml is easy to find.
    """

    def __init__(self, raw: dict | None = None) -> None:
        self._raw: dict = raw or {}

    def get(self, path: str):
        node = self._raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"Missing entry 'screens.{path}' in the configuration file.")
            node = node[part]
        return node

    def text(self, path: str, **placeholders: object) -> str:
        """Return the string at *path* with every ``{name}`` placeholder replaced."""
        value = self.get(path)
        if not isinstance(value, str):
            raise TypeError(f"Entry 'screens.{path}' must be text, got {type(value).__name__}.")
        for key, replacement in placeholders.items():
            value = value.replace("{" + key + "}", str(replacement))
        return value


@dataclass(frozen=True)
class GoogleSheetsConfig:
    spreadsheet_id: str = ""
    sheet_name: str = ""
    cell: str = ""
    credentials_file: str = ""


@dataclass(frozen=True)
class AppConfig:
    api: ApiConfig
    image_api: ImageApiConfig
    orchestrator: OrchestratorConfig
    scenario: ScenarioConfig
    agents: list[AgentEntry]
    logging: LoggingConfig
    ui: UiConfig
    screens: ScreenTexts = field(default_factory=ScreenTexts)
    google_sheets: GoogleSheetsConfig | None = None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _build_api(raw: dict) -> ApiConfig:
    return ApiConfig(
        base_url=raw.get("base_url", ""),
        api_key=raw.get("api_key", ""),
        organization=raw.get("organization"),
        timeout=raw.get("timeout", 60),
        agent_model=raw.get("agent_model", ""),
        orchestrator_model=raw.get("orchestrator_model", ""),
    )


def _build_image_api(raw: dict) -> ImageApiConfig:
    return ImageApiConfig(
        base_url=raw.get("base_url", "https://api.openai.com/v1"),
        api_key=raw.get("api_key", "sk-dummy-key"),
        model=raw.get("model", "gpt-image-1"),
        dummy_image_url=raw.get("dummy_image_url", ""),
    )


def _build_agents(raw_list: list[dict]) -> list[AgentEntry]:
    agents: list[AgentEntry] = []
    seen_names: set[str] = set()

    for entry in raw_list:
        name = entry.get("name", "")
        if not name:
            raise ValueError("Every agent must have a non-empty 'name'.")
        if name in seen_names:
            raise ValueError(f"Duplicate agent name: '{name}'.")
        seen_names.add(name)

        if not entry.get("system_prompt", "").strip():
            raise ValueError(f"Agent '{name}' must have a non-empty 'system_prompt'.")

        kwargs = {
            "name": name,
            "system_prompt": entry["system_prompt"].strip(),
            "role": entry.get("role", ""),
            "title": entry.get("title", ""),
            "description": entry.get("description", ""),
            "chat_color": entry.get("chat_color", ""),
        }
        if "talkativeness" in entry and entry["talkativeness"] is not None:
            kwargs["talkativeness"] = float(entry["talkativeness"])
        if "temperature" in entry and entry["temperature"] is not None:
            kwargs["temperature"] = float(entry["temperature"])
        if "max_tokens" in entry and entry["max_tokens"] is not None:
            kwargs["max_tokens"] = int(entry["max_tokens"])

        known_keys = {"name", "system_prompt", "role", "title", "description", "chat_color", "talkativeness", "temperature", "max_tokens"}
        kwargs["extra"] = {k: v for k, v in entry.items() if k not in known_keys}

        agents.append(AgentEntry(**kwargs))

    return agents


def load_config(path: str | Path) -> AppConfig:
    """Parse and validate the YAML configuration file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh)

    if raw is None:
        raise ValueError("Configuration file is empty.")

    raw_gs = raw.get("google_sheets")
    google_sheets = GoogleSheetsConfig(**raw_gs) if raw_gs else None

    raw_ui = dict(raw.get("ui", {}) or {})
    for legacy in ("shuffle_check_1", "shuffle_check_2", "shuffle check 1", "shuffle check 2"):
        raw_ui.pop(legacy, None)  # replaced by per-question "shuffle" in screens.checkN.questions
    for alias in ("show prototype note", "prototype_note", "prototype note"):
        if alias in raw_ui:
            raw_ui.setdefault("show_prototype_note", bool(raw_ui.pop(alias)))

    return AppConfig(
        api=_build_api(raw.get("api", {})),
        image_api=_build_image_api(raw.get("image_api", {})),
        orchestrator=OrchestratorConfig(**raw.get("orchestrator", {})),
        scenario=ScenarioConfig(**raw.get("scenario", {})),
        agents=_build_agents(raw.get("agents", [])),
        logging=LoggingConfig(**raw.get("logging", {})),
        ui=UiConfig(**raw_ui),
        screens=ScreenTexts(raw.get("screens")),
        google_sheets=google_sheets,
    )