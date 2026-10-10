"""
Interaction Portal v2 Configuration Loader

Loads and validates YAML configuration for the v2 application.
All configuration is typed and validated using dataclasses.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Union
from pathlib import Path
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    """Model configuration"""
    version: str = "gpt-4-2024-05-13"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    timeout: int = 60
    max_retries: int = 1


@dataclass
class QualtricsConfig:
    """Qualtrics integration configuration"""
    enabled: bool = False
    survey_url: str = ""
    return_url: str = ""


@dataclass
class SessionConfig:
    """Session-level configuration"""
    task_timer_seconds: int = 600
    break_timer_seconds: int = 120
    minimum_reading_time_orientation: int = 180
    minimum_reading_time_role: int = 180
    minimum_reading_time_brief: int = 60
    num_random_clients: int = 6
    fixed_last_client: str = "C7"
    allow_resume: bool = True
    resume_timeout_hours: int = 24
    desktop_only: bool = True
    mobile_error_message: str = "This study is designed for desktop browsers only."
    used_link_error_message: str = "This link has already been used."
    invalid_link_error_message: str = "Invalid entry link."


@dataclass
class EditorConfig:
    """Card editor configuration per condition"""
    type: str = "read_only"  # "read_only", "evaluative", "generative"
    submit_only: Optional[bool] = None
    send_back_needs_reason: Optional[bool] = None
    min_typed_share: Optional[int] = None


@dataclass
class PanelConfig:
    """Orchestrator panel configuration per condition"""
    word_budget: List[int] = field(default_factory=lambda: [60, 80])
    refresh_min_seconds: int = 60
    name_unasked: bool = False
    show_diagnosis: bool = False
    show_principle: bool = False


@dataclass
class NOAIConfig:
    """NOAI-specific configuration"""
    show_reference_pack: bool = True
    reference_pack_sections: List[str] = field(default_factory=lambda: ["nia", "theo", "rhys", "mira"])


@dataclass
class ConditionConfig:
    """Single condition configuration"""
    code: str = ""
    role: str = "passive"  # "passive", "evaluative", "generative", "none"
    panel_level: str = "coordination"  # "coordination", "task_focused", "developmental", "none"
    editor: EditorConfig = field(default_factory=EditorConfig)
    panel: PanelConfig = field(default_factory=PanelConfig)
    noai: Optional[NOAIConfig] = None


@dataclass
class AgentConfig:
    """Single agent configuration"""
    name: str = ""
    label: str = ""
    description: str = ""
    system_prompt: str = ""


@dataclass
class AgentsConfig:
    """All agents configuration"""
    nia: AgentConfig = field(default_factory=AgentConfig)
    theo: AgentConfig = field(default_factory=AgentConfig)
    rhys: AgentConfig = field(default_factory=AgentConfig)
    mira: AgentConfig = field(default_factory=AgentConfig)
    orchestrator: AgentConfig = field(default_factory=AgentConfig)


@dataclass
class ClientConfig:
    """Single client configuration"""
    name: str = ""
    brief_file: str = ""
    private_material: Dict[str, str] = field(default_factory=dict)  # agent_name -> file path
    hidden_items: List[str] = field(default_factory=list)
    fixed_last: bool = False


@dataclass
class PracticeExerciseConfig:
    """Single practice exercise configuration"""
    id: str = ""
    title: str = ""
    description: str = ""
    expected_action: str = ""
    expected_addressees: str = ""
    scripted_reply: str = ""
    next_unlocked: str = ""


@dataclass
class PracticeConfig:
    """Practice exercises configuration"""
    exercises: List[PracticeExerciseConfig] = field(default_factory=list)
    role_practice: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class CheckinItemConfig:
    """Single check-in question configuration"""
    id: str = ""
    question: str = ""
    type: str = "likert"  # "likert", "multiple_choice"
    options: List[str] = field(default_factory=list)
    skip_for_noai: bool = False


@dataclass
class CheckinConfig:
    """Check-in questions configuration"""
    items: List[CheckinItemConfig] = field(default_factory=list)


@dataclass
class CardFieldConfig:
    """Single card field configuration"""
    id: str = ""
    label: str = ""
    placeholder: str = ""
    required: bool = True


@dataclass
class CardEditorConfig:
    """Card editor configuration"""
    fields: List[CardFieldConfig] = field(default_factory=list)
    provenance: Dict[str, bool] = field(default_factory=lambda: {
        "track_author": True,
        "track_history": True,
        "track_span_id": True
    })


@dataclass
class LoggingConfig:
    """Logging configuration"""
    log_file: str = "logs/events.log"
    json_log_file: str = "logs/events.jsonl"
    session_store: str = "data/sessions.json"
    backup_frequency: str = "daily"
    export_formats: List[str] = field(default_factory=lambda: ["csv", "json"])
    log_events: Dict[str, bool] = field(default_factory=lambda: {
        "session_start": True, "session_end": True,
        "page_enter": True, "page_leave": True,
        "window_blur": True, "window_focus": True,
        "practice_step": True, "role_check": True,
        "message_sent": True, "agent_reply": True, "orch_message": True,
        "fold_open": True, "fold_close": True, "panel_shown": True,
        "card_action": True, "card_submit": True, "checkin_answer": True,
        "pack_open": True, "pack_close": True,
        "timer_expired": True, "break_start": True, "break_end": True,
        "agent_error": True, "scroll_sample": True
    })


@dataclass
class FoldedBlocksConfig:
    """Folded blocks configuration"""
    enabled: bool = True
    collapsed_by_default: bool = True
    label_format: str = "{agents} - {count} messages - {time}"


@dataclass
class ChatConfig:
    """Chat configuration"""
    autocomplete_enabled: bool = True
    max_message_length: int = 1000
    show_timestamps: bool = True


@dataclass
class PanelUIConfig:
    """Panel UI configuration"""
    refresh_interval: int = 60
    max_word_count: int = 100


@dataclass
class CardUIConfig:
    """Card UI configuration"""
    min_height: str = "400px"
    field_min_height: str = "80px"
    show_provenance: bool = True


@dataclass
class UIConfig:
    """UI configuration"""
    theme: str = "default"
    layout: str = "three_column"
    responsive: bool = False
    folded_blocks: FoldedBlocksConfig = field(default_factory=FoldedBlocksConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    panel: PanelUIConfig = field(default_factory=PanelUIConfig)
    card: CardUIConfig = field(default_factory=CardUIConfig)


@dataclass
class AdminConfig:
    """Admin configuration"""
    export_page_enabled: bool = True
    backup_directory: str = "backups"
    max_backups: int = 30


@dataclass
class TextContentConfig:
    """All text content configuration"""
    entry: Dict[str, str] = field(default_factory=dict)
    orientation: Dict[str, str] = field(default_factory=dict)
    meet_team: Dict[str, str] = field(default_factory=dict)
    role: Dict[str, str] = field(default_factory=dict)
    practice: Dict[str, str] = field(default_factory=dict)
    client_brief: Dict[str, str] = field(default_factory=dict)
    task: Dict[str, Any] = field(default_factory=dict)
    submission: Dict[str, str] = field(default_factory=dict)
    checkin: Dict[str, str] = field(default_factory=dict)
    break_page: Dict[str, str] = field(default_factory=dict)
    seventh_client: Dict[str, str] = field(default_factory=dict)
    exit: Dict[str, str] = field(default_factory=dict)


@dataclass
class ApplicationConfig:
    """Application metadata"""
    name: str = "Interaction Portal"
    version: str = "2.0.0"
    description: str = "Multi-agent chat simulation"


@dataclass
class FullConfig:
    """Complete configuration for Interaction Portal v2"""
    model: ModelConfig = field(default_factory=ModelConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    application: ApplicationConfig = field(default_factory=ApplicationConfig)
    qualtrics: QualtricsConfig = field(default_factory=QualtricsConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    conditions: Dict[str, ConditionConfig] = field(default_factory=dict)
    clients: Dict[str, ClientConfig] = field(default_factory=dict)
    seventh_client: Dict[str, Any] = field(default_factory=dict)
    practice: PracticeConfig = field(default_factory=PracticeConfig)
    checkin: CheckinConfig = field(default_factory=CheckinConfig)
    card_editor: CardEditorConfig = field(default_factory=CardEditorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    panel: Dict[str, Any] = field(default_factory=dict)
    ui: UIConfig = field(default_factory=UIConfig)
    text: TextContentConfig = field(default_factory=TextContentConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    
    # Runtime info
    config_file_path: str = ""
    config_version: str = ""

    def get_condition(self, condition_code: str) -> ConditionConfig:
        """Get condition config by code"""
        if condition_code not in self.conditions:
            raise ValueError(f"Unknown condition code: {condition_code}")
        return self.conditions[condition_code]

    def get_client(self, client_id: str) -> ClientConfig:
        """Get client config by ID"""
        if client_id not in self.clients:
            raise ValueError(f"Unknown client ID: {client_id}")
        return self.clients[client_id]

    def get_agent(self, agent_name: str) -> AgentConfig:
        """Get agent config by name"""
        agent_map = {
            'nia': self.agents.nia,
            'theo': self.agents.theo,
            'rhys': self.agents.rhys,
            'mira': self.agents.mira,
            'orchestrator': self.agents.orchestrator
        }
        if agent_name not in agent_map:
            raise ValueError(f"Unknown agent: {agent_name}")
        return agent_map[agent_name]

    def get_text(self, section: str, key: str, default: str = "") -> str:
        """Get text content by section and key"""
        section_obj = getattr(self.text, section, None)
        if section_obj and isinstance(section_obj, dict):
            return section_obj.get(key, default)
        return default


class ConfigLoaderV2:
    """Loader for Interaction Portal v2 configuration"""
    
    def __init__(self, config_path: str = "config_v2.yaml"):
        self.config_path = config_path
        self.config: Optional[FullConfig] = None
        self._validate_paths = True
    
    def load(self, validate: bool = True) -> FullConfig:
        """Load configuration from YAML file"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw_config = yaml.safe_load(f)
            
            # Convert to typed config
            self.config = self._parse_config(raw_config)
            self.config.config_file_path = self.config_path
            self.config.config_version = self._get_config_version()
            
            if validate:
                self.validate()
            
            logger.info(f"Configuration loaded from {self.config_path}")
            return self.config
            
        except FileNotFoundError:
            logger.error(f"Config file not found: {self.config_path}")
            raise
        except yaml.YAMLError as e:
            logger.error(f"YAML parsing error: {e}")
            raise
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise
    
    def _parse_config(self, raw: Dict) -> FullConfig:
        """Parse raw YAML into typed config"""
        config = FullConfig()
        
        # Application
        if 'application' in raw:
            config.application = ApplicationConfig(**raw['application'])
        
        # Model
        if 'model' in raw:
            model_raw = raw['model']
            # Handle environment variable for API key
            if 'api_key' in model_raw and model_raw['api_key'].startswith('${') and model_raw['api_key'].endswith('}'):
                env_var = model_raw['api_key'][2:-1]
                model_raw['api_key'] = os.environ.get(env_var, "")
            config.model = ModelConfig(**model_raw)
        
        # Qualtrics
        if 'qualtrics' in raw:
            config.qualtrics = QualtricsConfig(**raw['qualtrics'])
        
        # Session
        if 'session' in raw:
            config.session = SessionConfig(**raw['session'])
        
        # Conditions
        if 'conditions' in raw:
            config.conditions = {}
            for code, cond_raw in raw['conditions'].items():
                cond_dict = dict(cond_raw)
                editor_raw = cond_dict.pop('editor', None)
                panel_raw = cond_dict.pop('panel', None)
                noai_raw = cond_dict.pop('noai', None)
                
                editor_config = EditorConfig(**editor_raw) if isinstance(editor_raw, dict) else (editor_raw or EditorConfig())
                panel_config = PanelConfig(**panel_raw) if isinstance(panel_raw, dict) else (panel_raw or PanelConfig())
                noai_config = NOAIConfig(**noai_raw) if isinstance(noai_raw, dict) else noai_raw
                
                cond_config = ConditionConfig(
                    editor=editor_config,
                    panel=panel_config,
                    noai=noai_config,
                    **cond_dict
                )
                config.conditions[code] = cond_config
        
        # Agents
        if 'agents' in raw:
            agents_raw = raw['agents']
            config.agents = AgentsConfig(
                nia=AgentConfig(**agents_raw['nia']),
                theo=AgentConfig(**agents_raw['theo']),
                rhys=AgentConfig(**agents_raw['rhys']),
                mira=AgentConfig(**agents_raw['mira']),
                orchestrator=AgentConfig(**agents_raw['orchestrator'])
            )
        
        # Clients
        if 'clients' in raw:
            config.clients = {}
            for client_id, client_raw in raw['clients'].items():
                config.clients[client_id] = ClientConfig(**client_raw)
        
        # Seventh client
        if 'seventh_client' in raw:
            config.seventh_client = raw['seventh_client']
        
        # Practice
        if 'practice' in raw:
            practice_raw = raw['practice']
            exercises = []
            for ex_raw in practice_raw.get('exercises', []):
                exercises.append(PracticeExerciseConfig(**ex_raw))
            config.practice = PracticeConfig(
                exercises=exercises,
                role_practice=practice_raw.get('role_practice', {})
            )
        
        # Check-in
        if 'checkin' in raw:
            checkin_raw = raw['checkin']
            items = []
            for item_raw in checkin_raw.get('items', []):
                items.append(CheckinItemConfig(**item_raw))
            config.checkin = CheckinConfig(items=items)
        
        # Card editor
        if 'card_editor' in raw:
            card_raw = raw['card_editor']
            fields = []
            for field_raw in card_raw.get('fields', []):
                fields.append(CardFieldConfig(**field_raw))
            config.card_editor = CardEditorConfig(
                fields=fields,
                provenance=card_raw.get('provenance', {})
            )
        
        # Logging
        if 'logging' in raw:
            config.logging = LoggingConfig(**raw['logging'])
        
        # Panel
        if 'panel' in raw:
            config.panel = raw['panel']
        
        # UI
        if 'ui' in raw:
            ui_raw = raw['ui']
            config.ui = UIConfig(
                theme=ui_raw.get('theme', 'default'),
                layout=ui_raw.get('layout', 'three_column'),
                responsive=ui_raw.get('responsive', False),
                folded_blocks=FoldedBlocksConfig(**ui_raw.get('folded_blocks', {})),
                chat=ChatConfig(**ui_raw.get('chat', {})),
                panel=PanelUIConfig(**ui_raw.get('panel', {})),
                card=CardUIConfig(**ui_raw.get('card', {}))
            )
        
        # Text content
        if 'text' in raw:
            config.text = TextContentConfig(**raw['text'])
        
        # Admin
        if 'admin' in raw:
            config.admin = AdminConfig(**raw['admin'])
        
        return config
    
    def _get_config_version(self) -> str:
        """Get config version from git or timestamp"""
        import subprocess
        try:
            # Try to get git commit hash
            result = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                capture_output=True, text=True, cwd=os.path.dirname(self.config_path)
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
        
        # Fallback to timestamp
        from datetime import datetime
        return datetime.now().strftime("%Y%m%d_%H%M%S")
    
    def validate(self):
        """Validate configuration"""
        if not self.config:
            raise ValueError("No config loaded")
        
        errors = []
        
        # Check model config
        if not self.config.model.api_key and not self.config.qualtrics.enabled:
            # API key not required for testing without Qualtrics
            pass
        
        # Check conditions
        expected_conditions = [
            "PAS-COORD", "PAS-TASK", "PAS-DEV",
            "EVA-COORD", "EVA-TASK", "EVA-DEV",
            "GEN-COORD", "GEN-TASK", "GEN-DEV", "NOAI"
        ]
        for code in expected_conditions:
            if code not in self.config.conditions:
                errors.append(f"Missing condition: {code}")
        
        # Check clients
        if len(self.config.clients) < 7:
            errors.append(f"Expected 7 clients, found {len(self.config.clients)}")
        
        # Check fixed last client
        if self.config.session.fixed_last_client not in self.config.clients:
            errors.append(f"Fixed last client {self.config.session.fixed_last_client} not in clients")
        
        # Check agents
        required_agents = ['nia', 'theo', 'rhys', 'mira', 'orchestrator']
        for agent in required_agents:
            if not hasattr(self.config.agents, agent):
                errors.append(f"Missing agent: {agent}")
        
        # Check text content
        required_text = ['entry', 'orientation', 'meet_team', 'role', 'practice', 
                        'client_brief', 'task', 'submission', 'checkin', 'break_page', 
                        'seventh_client', 'exit']
        for section in required_text:
            if not hasattr(self.config.text, section):
                errors.append(f"Missing text section: {section}")
        
        if errors:
            raise ValueError("Configuration validation errors:\n" + "\n".join(errors))
        
        logger.info("Configuration validation passed")
    
    def get_condition(self, condition_code: str) -> ConditionConfig:
        """Get condition config by code"""
        if not self.config:
            self.load()
        
        if condition_code not in self.config.conditions:
            raise ValueError(f"Unknown condition code: {condition_code}")
        
        return self.config.conditions[condition_code]
    
    def get_client(self, client_id: str) -> ClientConfig:
        """Get client config by ID"""
        if not self.config:
            self.load()
        
        if client_id not in self.config.clients:
            raise ValueError(f"Unknown client ID: {client_id}")
        
        return self.config.clients[client_id]
    
    def get_agent(self, agent_name: str) -> AgentConfig:
        """Get agent config by name"""
        if not self.config:
            self.load()
        
        agent_map = {
            'nia': self.config.agents.nia,
            'theo': self.config.agents.theo,
            'rhys': self.config.agents.rhys,
            'mira': self.config.agents.mira,
            'orchestrator': self.config.agents.orchestrator
        }
        
        if agent_name not in agent_map:
            raise ValueError(f"Unknown agent: {agent_name}")
        
        return agent_map[agent_name]
    
    def get_text(self, section: str, key: str, default: str = "") -> str:
        """Get text content by section and key"""
        if not self.config:
            self.load()
        
        section_obj = getattr(self.config.text, section, None)
        if section_obj and isinstance(section_obj, dict):
            return section_obj.get(key, default)
        return default
    
    def reload(self):
        """Reload configuration from file"""
        self.config = None
        return self.load()


# Global config loader instance
_config_loader: Optional[ConfigLoaderV2] = None


def get_config_loader(config_path: str = "config_v2.yaml") -> ConfigLoaderV2:
    """Get or create global config loader"""
    global _config_loader
    if _config_loader is None:
        _config_loader = ConfigLoaderV2(config_path)
    return _config_loader


def get_config(config_path: str = "config_v2.yaml") -> FullConfig:
    """Get loaded configuration"""
    loader = get_config_loader(config_path)
    if loader.config is None:
        loader.load()
    return loader.config


def reset_config_loader():
    """Reset the global config loader instance"""
    global _config_loader
    _config_loader = None


if __name__ == "__main__":
    # Test loading
    loader = ConfigLoaderV2()
    config = loader.load()
    print(f"Loaded config for {config.application.name} v{config.application.version}")
    print(f"Conditions: {list(config.conditions.keys())}")
    print(f"Clients: {list(config.clients.keys())}")
    print(f"Agents: {['nia', 'theo', 'rhys', 'mira', 'orchestrator']}")
