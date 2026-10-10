"""
Typed configuration loader for the Interaction Portal.

Reads ``config/config.yaml``, ``config/conditions.yaml``, the Markdown prompt
templates in ``config/prompts/`` and the per-client content in
``config/content/clients/``. All participant-facing wording and every study
parameter is configuration; the application only contains mechanics.

Environment overrides (handy for testing without Qualtrics or a model key):
    PORTAL_STANDALONE=1|0   PORTAL_LLM_PROVIDER=mock|openai   PORTAL_TIMER_SCALE=0.05
    PORTAL_SECRET_KEY       PORTAL_ADMIN_TOKEN                PORTAL_CONFIG_DIR
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_HIDDEN = re.compile(r"<!--\s*hidden:([\w-]+)\s*-->\n?(.*?)\n?<!--\s*/hidden:\1\s*-->", re.DOTALL)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConditionConfig:
    code: str
    role: str            # passive | evaluative | generative | none
    panel_level: str     # coordination | task_focused | developmental | none
    ai: bool


@dataclass(frozen=True)
class TimerConfig:
    task_seconds: float = 600
    break_seconds: float = 120
    orientation_min_seconds: float = 15
    team_min_seconds: float = 0
    role_min_seconds: float = 10
    brief_min_seconds: float = 10
    resume_gap_seconds: float = 900


@dataclass(frozen=True)
class PanelConfig:
    word_budget: tuple[int, int] = (60, 80)
    word_target: int = 70
    refresh_min_seconds: float = 60
    name_unasked: bool = True
    forbidden_language: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class EditorConfig:
    send_back_needs_reason: bool = True
    min_typed_share: float = 0.25
    max_span_chars: int = 600


@dataclass(frozen=True)
class LlmConfig:
    provider: str = "mock"
    base_url: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    model: str = ""
    timeout_seconds: float = 60
    retries: int = 1
    specialist_temperature: float = 0.3
    orchestrator_temperature: float = 0.3
    panel_temperature: float = 0.4
    specialist_max_tokens: int = 400
    orchestrator_max_tokens: int = 300
    panel_max_tokens: int = 220
    live_typing_delay: float = 0.012


@dataclass(frozen=True)
class SpecialistConfig:
    name: str
    initial: str
    role_label: str
    team_description: str
    domain: str
    private_file: str
    card_fields: list[str]
    domain_keywords: list[str] = field(default_factory=list)
    hidden_keywords: list[str] = field(default_factory=list)
    color: str = "#e3ebf8"


@dataclass(frozen=True)
class CardField:
    id: str
    label: str
    hint: str = ""


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    name: str
    summary: str
    requirements: list[str]
    body: str
    private: dict[str, "PrivateMaterial"]    # keyed by specialist name

    @property
    def hidden_item_ids(self) -> list[str]:
        ids: list[str] = []
        for pm in self.private.values():
            ids.extend(pm.hidden_items)
        return ids


@dataclass(frozen=True)
class PrivateMaterial:
    agent: str
    client_id: str
    hidden_items: list[str]
    text_plain: str      # exactly what the reference pack shows (markers stripped)
    text_marked: str     # what the specialist sees (hidden items flagged by ID)
    hidden_passages: dict[str, str]


@dataclass
class PortalConfig:
    root: Path
    raw: dict[str, Any]
    config_version: str
    standalone: bool
    timer_scale: float
    allow_mobile: bool
    admin_token: str
    secret_key: str
    host: str
    port: int
    min_viewport_width: int
    llm: LlmConfig
    timers: TimerConfig
    panel: PanelConfig
    editor: EditorConfig
    conditions: dict[str, ConditionConfig]
    specialists: list[SpecialistConfig]
    card_fields: list[CardField]
    clients: dict[str, ClientConfig]
    randomised_clients: list[str]
    fixed_last_client: str
    break_after_position: int
    seventh_client_pack: bool
    prompts: dict[str, str]
    pages: dict[str, Any]
    practice: dict[str, Any]
    autopilot: dict[str, Any]
    qualtrics: dict[str, Any]
    orchestrator: dict[str, Any]
    reference_pack: list[dict[str, str]]
    study_title: str
    db_path: Path

    # ---- helpers --------------------------------------------------------
    def text(self, path: str, **fmt: Any) -> Any:
        """Look up ``pages`` text by dotted path; optionally ``str.format`` it."""
        node: Any = self.pages
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"Missing config text: pages.{path}")
            node = node[part]
        if fmt and isinstance(node, str):
            return node.format(**fmt)
        return node

    def scaled(self, seconds: float) -> float:
        return seconds / self.timer_scale if self.timer_scale > 0 else seconds

    def specialist(self, name: str) -> SpecialistConfig:
        for s in self.specialists:
            if s.name.lower() == name.lower():
                return s
        raise KeyError(name)

    @property
    def specialist_names(self) -> list[str]:
        return [s.name for s in self.specialists]

    @property
    def orchestrator_name(self) -> str:
        return self.orchestrator["name"]

    @property
    def all_agent_names(self) -> list[str]:
        return self.specialist_names + [self.orchestrator_name]

    @property
    def card_field_ids(self) -> list[str]:
        return [f.id for f in self.card_fields]

    def team_roster(self, exclude: str | None = None) -> str:
        lines = []
        for s in self.specialists:
            if exclude and s.name == exclude:
                continue
            lines.append(f"- {s.name} ({s.role_label}): {s.domain}")
        if exclude != self.orchestrator_name:
            lines.append(f"- {self.orchestrator_name} ({self.orchestrator['role_label']}): assigns work among the specialists; holds no client material.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    return (yaml.safe_load(m.group(1)) or {}), m.group(2)


def parse_private_material(path: Path) -> PrivateMaterial:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    body = re.sub(r"^# .*\n+", "", body.lstrip())   # drop the file's own title line
    hidden: dict[str, str] = {}

    def marked(m: re.Match) -> str:
        hidden[m.group(1)] = m.group(2).strip()
        return (f"[HIDDEN ITEM {m.group(1)}: disclose only if the question concerns this topic] "
                f"{m.group(2).strip()}")

    def plain(m: re.Match) -> str:
        return m.group(2).strip()

    text_marked = _HIDDEN.sub(marked, body).strip()
    text_plain = _HIDDEN.sub(plain, body).strip()
    declared = list(meta.get("hidden_items") or [])
    if sorted(declared) != sorted(hidden):
        raise ValueError(f"{path}: hidden_items {declared} do not match markers {sorted(hidden)}")
    return PrivateMaterial(
        agent=str(meta.get("agent", "")), client_id=str(meta.get("client_id", "")),
        hidden_items=declared, text_plain=text_plain, text_marked=text_marked,
        hidden_passages=hidden,
    )


def load_client(client_dir: Path, specialists: list[SpecialistConfig]) -> ClientConfig:
    meta, body = parse_frontmatter((client_dir / "brief.md").read_text(encoding="utf-8"))
    private: dict[str, PrivateMaterial] = {}
    for s in specialists:
        private[s.name] = parse_private_material(client_dir / "private" / s.private_file)
    reqs = list(meta.get("requirements") or [])
    if len(reqs) != 3:
        raise ValueError(f"{client_dir}: a brief needs exactly three stated requirements")
    return ClientConfig(
        client_id=str(meta.get("client_id", client_dir.name)), name=str(meta["name"]),
        summary=str(meta.get("summary", "")), requirements=reqs, body=body.strip(), private=private,
    )


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _build(cls, raw: dict | None):
    raw = dict(raw or {})
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = set(raw) - allowed
    if unknown:
        raise TypeError(f"Unknown {cls.__name__} config keys: {sorted(unknown)}")
    return cls(**raw)


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load_config(config_dir: str | Path | None = None) -> PortalConfig:
    if config_dir is None:
        root = Path(os.environ.get("PORTAL_CONFIG_DIR") or DEFAULT_CONFIG_DIR).resolve()
    else:
        candidate = Path(config_dir)
        if not candidate.is_absolute():
            if candidate.exists():
                root = candidate.resolve()
            elif (DEFAULT_CONFIG_DIR / candidate).exists():
                root = (DEFAULT_CONFIG_DIR / candidate).resolve()
            elif (DEFAULT_CONFIG_DIR.parent / candidate).exists():
                root = (DEFAULT_CONFIG_DIR.parent / candidate).resolve()
            else:
                root = candidate.resolve()
        else:
            root = candidate.resolve()
    if root.is_file():
        root = root.parent
    raw = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    cond_raw = yaml.safe_load((root / "conditions.yaml").read_text(encoding="utf-8"))["conditions"]

    testing = raw.get("testing", {})
    standalone = _env_bool("PORTAL_STANDALONE", bool(testing.get("standalone_mode", False)))
    scale = float(os.environ.get("PORTAL_TIMER_SCALE") or testing.get("timer_scale", 1.0))

    llm = _build(LlmConfig, raw.get("llm"))
    if os.environ.get("PORTAL_LLM_PROVIDER"):
        llm = LlmConfig(**{**llm.__dict__, "provider": os.environ["PORTAL_LLM_PROVIDER"]})

    timers = _build(TimerConfig, raw.get("timers"))
    p = dict(raw.get("panel", {}))
    panel = PanelConfig(
        word_budget=tuple(p.get("word_budget", [60, 80])), word_target=int(p.get("word_target", 70)),  # type: ignore[arg-type]
        refresh_min_seconds=float(p.get("refresh_min_seconds", 60)), name_unasked=bool(p.get("name_unasked", True)),
        forbidden_language=str(p.get("forbidden_language", "")), sections=dict(p.get("sections", {})),
    )
    editor = _build(EditorConfig, raw.get("editor"))
    conditions = {
        code: ConditionConfig(code=code, role=str(c["role"]), panel_level=str(c["panel_level"]), ai=bool(c["ai"]))
        for code, c in cond_raw.items()
    }
    specialists = [SpecialistConfig(**s) for s in raw["specialists"]]
    card_fields = [CardField(**f) for f in raw["card"]["fields"]]

    # prompts
    prompts: dict[str, str] = {}
    for f in (root / "prompts").glob("*.md"):
        prompts[f.stem] = f.read_text(encoding="utf-8")
    for f in (root / "prompts" / "panel_levels").glob("*.md"):
        prompts[f"panel_{f.stem}"] = f.read_text(encoding="utf-8")

    crt = raw["clients"]
    content_dir = root / crt.get("content_dir", "content/clients")
    wanted = list(crt["randomised"]) + [crt["fixed_last"]]
    clients = {cid: load_client(content_dir / cid, specialists) for cid in wanted}

    pages = raw["pages"]
    return PortalConfig(
        root=root, raw=raw, config_version=str(raw["config_version"]), standalone=standalone,
        timer_scale=scale, allow_mobile=bool(testing.get("allow_mobile", False)),
        admin_token=os.environ.get("PORTAL_ADMIN_TOKEN") or str(testing.get("admin_token", "")),
        secret_key=os.environ.get("PORTAL_SECRET_KEY") or str(raw["server"]["secret_key"]),
        host=str(raw["server"]["host"]), port=int(raw["server"]["port"]),
        min_viewport_width=int(raw["server"].get("min_viewport_width", 900)),
        llm=llm, timers=timers, panel=panel, editor=editor, conditions=conditions,
        specialists=specialists, card_fields=card_fields, clients=clients,
        randomised_clients=list(crt["randomised"]), fixed_last_client=str(crt["fixed_last"]),
        break_after_position=int(crt.get("break_after_position", 3)),
        seventh_client_pack=bool(crt.get("seventh_client_pack", False)),
        prompts=prompts, pages=pages, practice=raw["practice"], autopilot=raw.get("autopilot", {}),
        qualtrics=raw["qualtrics"], orchestrator=raw["orchestrator"],
        reference_pack=raw["reference_pack"]["sections"], study_title=str(raw.get("study_title", "Interaction Portal")),
        db_path=Path(os.environ.get("PORTAL_DB_PATH") or (root.parent / "logs" / "portal.sqlite3")),
    )


def build_reference_pack(cfg: PortalConfig, client_id: str) -> list[dict[str, str]]:
    """Assemble the four-section reference pack from the specialists' private files.

    The text is *identical* to what each specialist holds (hidden-item markers
    stripped), so pack and specialist material can never drift apart.
    """
    client = cfg.clients[client_id]
    return [
        {"agent": s["agent"], "title": s["title"], "text": client.private[s["agent"]].text_plain}
        for s in cfg.reference_pack
    ]
