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

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_dotenv_if_present(env_path: Path | str | None = None) -> list[Path]:
    """Load key-value pairs from a .env file into os.environ without overwriting existing variables.

    Checks the specified path or scans the current working directory, repository root,
    and config directory in sequence. Preserves any variables already set in os.environ
    (such as variables defined on the command line or inherited from the process environment).
    """
    candidate_paths: list[Path] = []
    if env_path is not None:
        candidate_paths.append(Path(env_path).resolve())
    else:
        repo_root = DEFAULT_CONFIG_DIR.parent
        for p in [Path.cwd() / ".env", repo_root / ".env", DEFAULT_CONFIG_DIR / ".env"]:
            rp = p.resolve()
            if rp not in candidate_paths:
                candidate_paths.append(rp)

    loaded_files: list[Path] = []
    for path in candidate_paths:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, raw_val = line.split("=", 1)
            key = key.strip()
            raw_val = raw_val.strip()
            if not key:
                continue

            if (raw_val.startswith('"') and raw_val.endswith('"') and len(raw_val) >= 2) or (
                raw_val.startswith("'") and raw_val.endswith("'") and len(raw_val) >= 2
            ):
                val = raw_val[1:-1]
            else:
                if " #" in raw_val:
                    raw_val = raw_val.split(" #", 1)[0].strip()
                val = raw_val.strip("\"'")

            if key not in os.environ:
                os.environ[key] = val
        loaded_files.append(path)

    return loaded_files


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
    allow_paste: bool = True


PROVIDER_SPECS: dict[str, dict[str, Any]] = {
    "mock": {
        "canonical": "mock",
        "aliases": ["offline", "test", "none"],
        "base_url": "",
        "api_key_env": "",
        "fallback_api_key_envs": [],
        "default_model": "mock",
        "display_name": "Mock (Offline Deterministic)",
    },
    "openai": {
        "canonical": "openai",
        "aliases": ["chatgpt"],
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "fallback_api_key_envs": ["CHATGPT_API_KEY", "PORTAL_LLM_API_KEY"],
        "default_model": "gpt-5.4-mini",
        "display_name": "OpenAI / ChatGPT",
    },
    "claude": {
        "canonical": "claude",
        "aliases": ["anthropic"],
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
        "fallback_api_key_envs": ["CLAUDE_API_KEY", "PORTAL_LLM_API_KEY"],
        "default_model": "claude-haiku-5-5",
        "display_name": "Anthropic Claude",
    },
    "deepseek": {
        "canonical": "deepseek",
        "aliases": [],
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "fallback_api_key_envs": ["PORTAL_LLM_API_KEY"],
        "default_model": "deepseek-flash",
        "display_name": "DeepSeek",
    },
    "glm": {
        "canonical": "glm",
        "aliases": ["zhipu", "zhipuai", "bigmodel", "zai"],
        "base_url": "https://api.z.ai/api/paas/v4",
        "api_key_env": "ZAI_API_KEY",
        "fallback_api_key_envs": ["GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY", "PORTAL_LLM_API_KEY"],
        "default_model": "glm-5.3-flash",
        "display_name": "GLM (Z.ai, formerly Zhipu AI)",
    },
    "qwen": {
        "canonical": "qwen",
        "aliases": ["dashscope", "tongyi", "aliyun", "modelstudio"],
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "fallback_api_key_envs": ["QWEN_API_KEY", "PORTAL_LLM_API_KEY"],
        "default_model": "qwen3.8-flash",
        "display_name": "Qwen (Alibaba Cloud Model Studio / DashScope)",
    },
}


def canonical_provider(name: str) -> str:
    """Normalize provider name string to canonical form."""
    n = (name or "").strip().lower()
    for canon, spec in PROVIDER_SPECS.items():
        if n == canon or n in spec["aliases"]:
            return canon
    return n


def get_provider_spec(name: str) -> dict[str, Any] | None:
    """Get provider specification by canonical name or alias."""
    canon = canonical_provider(name)
    return PROVIDER_SPECS.get(canon)


def get_provider_env_vars(provider_str: str, configured_var: str = "") -> list[str]:
    """Return all environment variable names that can supply the API key for this provider."""
    spec = get_provider_spec(provider_str)
    vars_list: list[str] = []
    if configured_var:
        vars_list.append(configured_var)
    if spec:
        for v in [spec["api_key_env"]] + spec["fallback_api_key_envs"]:
            if v and v not in vars_list:
                vars_list.append(v)
    if "PORTAL_LLM_API_KEY" not in vars_list:
        vars_list.append("PORTAL_LLM_API_KEY")
    return vars_list


def resolve_api_key(cfg: Any) -> tuple[str, str]:
    """Find the API key and the environment variable name it came from."""
    llm = cfg.llm if hasattr(cfg, "llm") else cfg
    spec = get_provider_spec(getattr(llm, "provider", ""))
    candidate_vars: list[str] = []

    # 1. Primary provider api_key_env
    if spec and spec.get("api_key_env"):
        candidate_vars.append(spec["api_key_env"])

    # 2. Configured api_key_env on llm if explicitly specified and distinct
    configured = getattr(llm, "api_key_env", "")
    if configured and configured not in candidate_vars:
        candidate_vars.append(configured)

    # 3. Provider fallbacks (e.g. CLAUDE_API_KEY, ZHIPUAI_API_KEY, QWEN_API_KEY)
    if spec:
        for v in spec.get("fallback_api_key_envs", []):
            if v and v not in candidate_vars:
                candidate_vars.append(v)

    # 4. Universal override
    if "PORTAL_LLM_API_KEY" not in candidate_vars:
        candidate_vars.append("PORTAL_LLM_API_KEY")

    for var in candidate_vars:
        val = os.environ.get(var, "").strip()
        if val:
            return val, var
    return "", ""


@dataclass
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
    supports_stream_usage: bool = True
    token_param: str = "max_tokens"
    send_temperature: bool = True
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
    config_sha256: str = ""
    entry_link_secret: str = ""
    require_entry_sig: bool = False
    max_concurrent_participants: int = 100

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

    def set_provider(self, provider: str, base_url: str | None = None,
                     model: str | None = None, api_key_env: str | None = None) -> None:
        """Switch the active LLM provider and automatically apply canonical defaults."""
        canon = canonical_provider(provider)
        spec = get_provider_spec(canon)
        cur = self.llm.__dict__.copy()
        cur["provider"] = canon
        if spec:
            cur["base_url"] = base_url if base_url is not None else spec["base_url"]
            cur["model"] = model if model is not None else spec["default_model"]
            cur["api_key_env"] = api_key_env if api_key_env is not None else spec["api_key_env"]
        self.llm = LlmConfig(**cur)


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

def compute_config_sha256(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in (".yaml", ".yml", ".md"):
            h.update(p.relative_to(root).as_posix().encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


def load_config(config_dir: str | Path | None = None) -> PortalConfig:
    load_dotenv_if_present()
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

    llm_dict = dict(raw.get("llm") or {})
    configured_provider = os.environ.get("PORTAL_LLM_PROVIDER") or llm_dict.get("provider", "mock")
    canon_provider = canonical_provider(configured_provider)
    spec = get_provider_spec(canon_provider)

    default_base_url = spec["base_url"] if spec else ""
    default_api_key_env = spec["api_key_env"] if spec else "OPENAI_API_KEY"
    default_model = spec["default_model"] if spec else ""

    raw_base_url = llm_dict.get("base_url", "")
    if canon_provider not in ("mock", "openai") and (not raw_base_url or raw_base_url == "https://api.openai.com/v1"):
        llm_dict["base_url"] = default_base_url
    elif not raw_base_url:
        llm_dict["base_url"] = default_base_url

    base_url_override = (
        os.environ.get("PORTAL_LLM_BASE_URL")
        or (os.environ.get(f"{canon_provider.upper()}_BASE_URL") if canon_provider else None)
    )
    if base_url_override:
        llm_dict["base_url"] = base_url_override

    raw_model = llm_dict.get("model", "")
    if canon_provider not in ("mock", "openai") and (not raw_model or raw_model in ("gpt-5.4-mini", "gpt-4o-mini-2024-07-18")):
        llm_dict["model"] = default_model
    elif not raw_model:
        llm_dict["model"] = default_model
    if os.environ.get("PORTAL_LLM_MODEL"):
        llm_dict["model"] = os.environ["PORTAL_LLM_MODEL"]

    raw_api_key_env = llm_dict.get("api_key_env", "")
    if canon_provider not in ("mock", "openai") and (not raw_api_key_env or raw_api_key_env == "OPENAI_API_KEY"):
        llm_dict["api_key_env"] = default_api_key_env
    elif not raw_api_key_env:
        llm_dict["api_key_env"] = default_api_key_env
    if os.environ.get("PORTAL_LLM_API_KEY_ENV"):
        llm_dict["api_key_env"] = os.environ["PORTAL_LLM_API_KEY_ENV"]

    llm_dict["provider"] = canon_provider
    llm = _build(LlmConfig, llm_dict)

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

    config_sha = compute_config_sha256(root)
    secret_key = os.environ.get("PORTAL_SECRET_KEY") or str(raw["server"].get("secret_key", ""))
    admin_token = os.environ.get("PORTAL_ADMIN_TOKEN") or str(testing.get("admin_token", ""))
    completion_code_secret = (
        os.environ.get("PORTAL_COMPLETION_SECRET")
        or os.environ.get("PORTAL_COMPLETION_CODE_SECRET")
        or str(raw.get("qualtrics", {}).get("completion_code_secret", ""))
    )
    entry_link_secret = os.environ.get("PORTAL_ENTRY_SECRET") or secret_key
    portal_env = os.environ.get("PORTAL_ENV", "").lower()
    is_production = (portal_env in ("production", "prod")) or (not standalone)
    require_entry_sig = _env_bool("PORTAL_REQUIRE_ENTRY_SIG", is_production)

    host = os.environ.get("PORTAL_HOST") or str(raw["server"].get("host", "127.0.0.1"))
    port = int(os.environ.get("PORTAL_PORT") or raw["server"].get("port", 8000))

    known_defaults = {
        "change-me-in-production",
        "admin-test-token",
        "change-me-too",
        "replace-with-a-secure-random-secret",
        "secure-token-for-exporting-data",
    }
    if is_production:
        if secret_key in known_defaults or len(secret_key) < 24:
            raise RuntimeError(
                f"Insecure server.secret_key in production: must be >= 24 chars and not a default value. Set PORTAL_SECRET_KEY."
            )
        if admin_token in known_defaults or len(admin_token) < 24:
            raise RuntimeError(
                f"Insecure admin_token in production: must be >= 24 chars and not a default value. Set PORTAL_ADMIN_TOKEN."
            )
        if completion_code_secret in known_defaults or len(completion_code_secret) < 24:
            raise RuntimeError(
                f"Insecure qualtrics.completion_code_secret in production: must be >= 24 chars and not a default value. Set PORTAL_COMPLETION_SECRET."
            )
    else:
        if host not in ("127.0.0.1", "localhost", "::1"):
            import logging
            logging.getLogger("portal").warning(
                "Standalone mode is running on non-loopback host '%s'. The test launcher is accessible to the network.", host
            )

    # propagate possibly overridden completion code secret
    if "qualtrics" in raw and isinstance(raw["qualtrics"], dict):
        raw["qualtrics"]["completion_code_secret"] = completion_code_secret

    return PortalConfig(
        root=root, raw=raw, config_version=str(raw["config_version"]), standalone=standalone,
        timer_scale=scale, allow_mobile=bool(testing.get("allow_mobile", False)),
        admin_token=admin_token, secret_key=secret_key, host=host, port=port,
        min_viewport_width=int(raw["server"].get("min_viewport_width", 900)),
        llm=llm, timers=timers, panel=panel, editor=editor, conditions=conditions,
        specialists=specialists, card_fields=card_fields, clients=clients,
        randomised_clients=list(crt["randomised"]), fixed_last_client=str(crt["fixed_last"]),
        break_after_position=int(crt.get("break_after_position", 3)),
        seventh_client_pack=bool(crt.get("seventh_client_pack", False)),
        prompts=prompts, pages=pages, practice=raw["practice"], autopilot=raw.get("autopilot", {}),
        qualtrics=raw["qualtrics"], orchestrator=raw["orchestrator"],
        reference_pack=raw["reference_pack"]["sections"], study_title=str(raw.get("study_title", "Interaction Portal")),
        db_path=Path(os.environ.get("PORTAL_DB_PATH") or (root.parent / "logs" / ("portal_test.sqlite3" if standalone else "portal.sqlite3"))),
        config_sha256=config_sha, entry_link_secret=entry_link_secret, require_entry_sig=require_entry_sig,
        max_concurrent_participants=int(os.environ.get("PORTAL_MAX_PARTICIPANTS") or raw["server"].get("max_concurrent_participants", 100)),
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
