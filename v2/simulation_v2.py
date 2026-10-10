"""
Interaction Portal v2 - Core Simulation Loop

Implements the 13-page session flow as specified in the v2 spec:

Page Order:
1. Entry
2. Orientation
3. Meet Your Team
4. Your Role
5. Practice (4 exercises)
6. Client Brief (x6 randomised + 1 fixed last)
7. Task Page
8. Submission
9. Check-in
10. Break (after client 3)
11. Seventh Client (alone)
12. Exit

Server-side timers are maintained here. All state is server-side.
"""

import time
import uuid
import random
import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple, Callable
from datetime import datetime, timezone, timedelta
from enum import Enum
import logging

from config_loader_v2 import get_config, ConditionConfig, ClientConfig
from agents_v2 import (
    AgentFactory, SpecialistAgent, OrchestratorAgent, 
    Message, FoldedBlock, PanelGenerator, get_panel_generator
)
from card_editor import Card, CardEditor, CardActionType
from conversation_logger_v2 import (
    get_logger, reset_logger, ConversationLoggerV2,
    Event, SessionStartEvent, SessionEndEvent
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PageType(Enum):
    """Page types in the session flow"""
    ENTRY = "entry"
    ORIENTATION = "orientation"
    MEET_TEAM = "meet_team"
    YOUR_ROLE = "your_role"
    PRACTICE = "practice"
    CLIENT_BRIEF = "client_brief"
    TASK = "task"
    SUBMISSION = "submission"
    CHECKIN = "checkin"
    BREAK = "break"
    SEVENTH_CLIENT = "seventh_client"
    EXIT = "exit"


class SessionState(Enum):
    """Session states"""
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    TIMEOUT = "timeout"


@dataclass
class Timer:
    """Server-side timer"""
    duration_seconds: int
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    is_running: bool = False
    
    def start(self):
        """Start timer"""
        self.start_time = time.time()
        self.end_time = self.start_time + self.duration_seconds
        self.is_running = True
    
    def stop(self):
        """Stop timer"""
        self.is_running = False
    
    def pause(self):
        """Pause timer"""
        if self.is_running:
            elapsed = time.time() - self.start_time
            self.duration_seconds = max(0, int(self.duration_seconds - elapsed))
            self.is_running = False
    
    def resume(self):
        """Resume timer"""
        if not self.is_running:
            self.start()
    
    def get_remaining_seconds(self) -> float:
        """Get remaining time in seconds"""
        if not self.is_running or self.start_time is None:
            return self.duration_seconds
        return max(0, self.end_time - time.time())
    
    def get_remaining_formatted(self) -> str:
        """Get remaining time as formatted string (MM:SS)"""
        seconds = int(self.get_remaining_seconds())
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:02d}"
    
    def is_expired(self) -> bool:
        """Check if timer has expired"""
        if not self.is_running or self.start_time is None:
            return False
        return time.time() >= self.end_time


@dataclass
class ClientState:
    """State for a single client task"""
    client_id: str
    brief_read: bool = False
    task_started: bool = False
    task_timer: Optional[Timer] = None
    card: Optional[Card] = None
    card_editor: Optional[CardEditor] = None
    transcript: List[Message] = field(default_factory=list)
    folded_blocks: List[FoldedBlock] = field(default_factory=list)
    panel_text: str = ""
    panel_sections: List[str] = field(default_factory=list)
    panel_word_count: int = 0
    submission_complete: bool = False
    checkin_complete: bool = False
    
    def start_task_timer(self, duration_seconds: int):
        """Start task timer"""
        self.task_timer = Timer(duration_seconds)
        self.task_timer.start()
    
    def get_task_timer(self) -> Optional[Timer]:
        """Get task timer"""
        return self.task_timer


@dataclass
class Session:
    """Complete session state"""
    pid: str
    condition_code: str
    condition: ConditionConfig
    state: SessionState = SessionState.NOT_STARTED
    current_page: PageType = PageType.ENTRY
    
    # Timers
    session_start_time: float = field(default_factory=time.time)
    orientation_timer: Optional[Timer] = None
    role_timer: Optional[Timer] = None
    break_timer: Optional[Timer] = None
    
    # Page state
    orientation_read: bool = False
    team_order: List[str] = field(default_factory=list)  # Random order of specialists
    role_shown: bool = False
    practice_complete: bool = False
    current_practice_step: int = 0
    
    # Client state
    clients: Dict[str, ClientState] = field(default_factory=dict)
    client_order: List[str] = field(default_factory=list)  # Order of clients for this session
    current_client_index: int = 0
    break_after_client_3: bool = False
    seventh_client_complete: bool = False
    
    # Cards
    specialist_card_order: List[str] = field(default_factory=list)
    
    # Tracking
    resume_count: int = 0
    last_activity_time: float = field(default_factory=time.time)
    
    # UI state
    window_focused: bool = True
    scroll_positions: Dict[str, float] = field(default_factory=dict)
    
    def __post_init__(self):
        # Initialize client order
        config = get_config()
        
        # Get all client IDs except the fixed last one
        all_clients = [cid for cid, client in config.clients.items() if not client.fixed_last]
        fixed_last = config.session.fixed_last_client
        
        # Randomise 6 clients
        random.shuffle(all_clients)
        self.client_order = all_clients[:config.session.num_random_clients] + [fixed_last]
        
        # Initialize specialist card order (random)
        self.specialist_card_order = ["nia", "theo", "rhys", "mira"]
        random.shuffle(self.specialist_card_order)
        
        # Initialize team order (random)
        self.team_order = ["nia", "theo", "rhys", "mira"]
        random.shuffle(self.team_order)
        
        # Initialize clients
        for client_id in self.client_order:
            self.clients[client_id] = ClientState(client_id=client_id)
    
    def get_current_client(self) -> Optional[ClientState]:
        """Get current client state"""
        if self.current_client_index < len(self.client_order):
            return self.clients.get(self.client_order[self.current_client_index])
        return None
    
    def get_current_client_id(self) -> Optional[str]:
        """Get current client ID"""
        if self.current_client_index < len(self.client_order):
            return self.client_order[self.current_client_index]
        return None
    
    def advance_to_next_client(self):
        """Advance to next client"""
        self.current_client_index += 1
        
        # Check if we should start break after client 3
        if self.current_client_index == 3 and not self.break_after_client_3:
            self.break_after_client_3 = True
    
    def is_break_time(self) -> bool:
        """Check if it's time for break (after client 3)"""
        return self.break_after_client_3 and self.current_client_index == 3
    
    def start_orientation_timer(self):
        """Start orientation timer"""
        config = get_config()
        self.orientation_timer = Timer(config.session.minimum_reading_time_orientation)
        self.orientation_timer.start()
    
    def start_role_timer(self):
        """Start role page timer"""
        config = get_config()
        self.role_timer = Timer(config.session.minimum_reading_time_role)
        self.role_timer.start()
    
    def start_break_timer(self):
        """Start break timer"""
        config = get_config()
        self.break_timer = Timer(config.session.break_timer_seconds)
        self.break_timer.start()
    
    def can_continue_from_orientation(self) -> bool:
        """Check if can continue from orientation page"""
        if not self.orientation_timer:
            return True
        return self.orientation_timer.is_expired() or self.orientation_read
    
    def can_continue_from_role(self) -> bool:
        """Check if can continue from role page"""
        if not self.role_timer:
            return True
        return self.role_timer.is_expired() or self.role_shown
    
    def mark_orientation_read(self):
        """Mark orientation as read (allows continuing early)"""
        self.orientation_read = True
    
    def mark_role_shown(self):
        """Mark role as shown"""
        self.role_shown = True
    
    def mark_practice_complete(self):
        """Mark practice as complete"""
        self.practice_complete = True
    
    def update_last_activity(self):
        """Update last activity time"""
        self.last_activity_time = time.time()
    
    def is_session_timeout(self) -> bool:
        """Check if session has timed out due to inactivity"""
        config = get_config()
        timeout_hours = config.session.resume_timeout_hours
        return (time.time() - self.last_activity_time) > (timeout_hours * 3600)


class SimulationV2:
    """Core simulation for Interaction Portal v2"""
    
    def __init__(self, pid: str, condition_code: str, user_agent: str, viewport: Dict):
        self.pid = pid
        self.condition_code = condition_code
        self.user_agent = user_agent
        self.viewport = viewport
        
        # Load configuration
        self.config = get_config()
        
        # Get condition
        self.condition: ConditionConfig = self.config.conditions.get(condition_code)
        if not self.condition:
            raise ValueError(f"Unknown condition code: {condition_code}")
        
        # Initialize session
        self.session = Session(
            pid=pid,
            condition_code=condition_code,
            condition=self.condition
        )
        
        # Initialize logger
        self.logger = get_logger()
        
        # Start session and log
        self._start_session()
        
        # Initialize agents
        self._initialize_agents()
        
        # Initialize card editor
        self.card_editor = CardEditor(self.condition)
        
        # Panel generator
        self.panel_generator = get_panel_generator()
        
        logger.info(f"Simulation initialized for {pid} with condition {condition_code}")
    
    def _start_session(self):
        """Start session and log session_start event"""
        # Generate client order
        all_clients = [cid for cid, client in self.config.clients.items() if not client.fixed_last]
        fixed_last = self.config.session.fixed_last_client
        
        random.shuffle(all_clients)
        client_order = all_clients[:self.config.session.num_random_clients] + [fixed_last]
        
        # Generate card order
        card_order = ["nia", "theo", "rhys", "mira"]
        random.shuffle(card_order)
        
        # Start session in logger
        self.logger.start_session(
            pid=self.pid,
            condition_code=self.condition_code,
            user_agent=self.user_agent,
            viewport=self.viewport,
            client_order=client_order,
            card_order=card_order
        )
        
        # Update session
        self.session.client_order = client_order
        self.session.specialist_card_order = card_order
    
    def _initialize_agents(self):
        """Initialize all agents with current client"""
        # Get first client
        first_client_id = self.session.get_current_client_id()
        if first_client_id:
            config = get_config()
            client_config = config.get_client(first_client_id)
            
            # Set client for all specialists
            for agent_name in ["nia", "theo", "rhys", "mira"]:
                agent = AgentFactory.get_agent(agent_name)
                agent.set_client(first_client_id)
                agent.load_hidden_items(first_client_id)
    
    def get_page_content(self, page_type: PageType, client_id: Optional[str] = None) -> Dict:
        """Get content for a specific page"""
        config = self.config
        text_config = config.text
        
        if page_type == PageType.ENTRY:
            return {
                "title": "Entry",
                "content": "",
                "show_continue": True
            }
        
        elif page_type == PageType.ORIENTATION:
            if not self.session.orientation_timer:
                self.session.start_orientation_timer()
            return {
                "title": text_config.orientation.get("title", "Orientation"),
                "content": text_config.orientation.get("content", ""),
                "continue_button": text_config.orientation.get("continue_button", "Continue"),
                "can_continue": self.session.can_continue_from_orientation(),
                "minimum_reading_message": text_config.orientation.get("minimum_reading_message", ""),
                "timer_remaining": self.session.orientation_timer.get_remaining_formatted() if self.session.orientation_timer else ""
            }
        
        elif page_type == PageType.MEET_TEAM:
            # Get team order
            team_order = self.session.team_order
            agents_config = config.agents
            
            team_members = []
            for agent_name in team_order:
                agent_config = getattr(agents_config, agent_name)
                team_members.append({
                    "name": agent_config.name,
                    "label": agent_config.label,
                    "description": agent_config.description
                })
            
            # Add orchestrator
            orch_config = agents_config.orchestrator
            # Orchestrator description varies by panel level
            orch_description = self._get_orchestrator_description()
            
            team_members.append({
                "name": orch_config.name,
                "label": orch_config.label,
                "description": orch_description
            })
            
            return {
                "title": text_config.meet_team.get("title", "Meet Your Team"),
                "introduction": text_config.meet_team.get("introduction", ""),
                "team_members": team_members,
                "show_continue": True
            }
        
        elif page_type == PageType.YOUR_ROLE:
            if not self.session.role_timer:
                self.session.start_role_timer()
            role_text = self._get_role_text()
            return {
                "title": text_config.role.get("title", "Your Role"),
                "content": role_text,
                "continue_button": text_config.role.get("continue_button", "Continue"),
                "can_continue": self.session.can_continue_from_role(),
                "timer_remaining": self.session.role_timer.get_remaining_formatted() if self.session.role_timer else ""
            }
        
        elif page_type == PageType.PRACTICE:
            return self._get_practice_content()
        
        elif page_type == PageType.CLIENT_BRIEF:
            if not client_id:
                client_id = self.session.get_current_client_id()
            
            client_config = config.get_client(client_id)
            brief_text = self._load_brief_text(client_id)
            
            # Get card fields
            card_fields = [
                {"id": f.id, "label": f.label, "placeholder": f.placeholder}
                for f in config.card_editor.fields
            ]
            
            return {
                "title": f"{text_config.client_brief.get('title', 'Client Brief')} - {client_config.name}",
                "brief": brief_text,
                "requirements_label": text_config.client_brief.get("requirements_label", "Requirements:"),
                "card_fields_label": text_config.client_brief.get("card_fields_label", "Card Fields:"),
                "card_fields": card_fields,
                "continue_button": text_config.client_brief.get("continue_button", "Start Task"),
                "client_name": client_config.name
            }
        
        elif page_type == PageType.TASK:
            client_id = client_id or self.session.get_current_client_id()
            client_state = self.session.get_current_client()
            
            if not client_state:
                return {"error": "No current client"}
            
            # Get role reminder
            role_reminder = self._get_role_reminder()
            
            # Get chat transcript
            transcript = self._format_transcript(client_state.transcript)
            
            # Get folded blocks
            folded_blocks = [
                {
                    "block_id": block.block_id,
                    "label": block.get_label(),
                    "is_open": block.is_open,
                    "messages": [
                        {
                            "text": msg.text,
                            "sender": msg.sender,
                            "timestamp": msg.timestamp
                        } for msg in block.messages
                    ]
                } for block in client_state.folded_blocks
            ]
            
            # Get panel content
            panel_content = self._get_panel_content(client_id, client_state)
            
            # Get card editor state
            card_state = self.card_editor.get_card_state()
            
            # Get timer
            timer_remaining = ""
            if client_state.task_timer:
                timer_remaining = client_state.task_timer.get_remaining_formatted()
            
            return {
                "title": f"{text_config.task.get('title', 'Task Page')} - {config.clients[client_id].name}",
                "brief": self._load_brief_text(client_id),
                "role_reminder": role_reminder,
                "team_chat_label": text_config.task.get("team_chat_label", "Team Chat"),
                "panel_label": text_config.task.get("panel_label", "Orchestrator Panel"),
                "card_editor_label": text_config.task.get("card_editor_label", "Card Editor"),
                "transcript": transcript,
                "folded_blocks": folded_blocks,
                "panel": panel_content,
                "card_editor": card_state,
                "timer_label": text_config.task.get("timer_label", "Time remaining"),
                "timer_remaining": timer_remaining,
                "submit_button": text_config.task.get("submit_button", "Submit Card"),
                "is_noai": self.condition.code == "NOAI"
            }
        
        elif page_type == PageType.SUBMISSION:
            card_state = self.card_editor.get_card_state()
            return {
                "title": text_config.submission.get("title", "Review Submission"),
                "instruction": text_config.submission.get("instruction", ""),
                "card": card_state,
                "submit_button": text_config.submission.get("submit_button", "Confirm Submission"),
                "back_button": text_config.submission.get("back_button", "Back to Edit")
            }
        
        elif page_type == PageType.CHECKIN:
            checkin_items = []
            for item in config.checkin.items:
                if self.condition.code == "NOAI" and item.skip_for_noai:
                    continue
                checkin_items.append({
                    "id": item.id,
                    "question": item.question,
                    "type": item.type,
                    "options": item.options
                })
            
            return {
                "title": text_config.checkin.get("title", "Check-in Questions"),
                "instruction": text_config.checkin.get("instruction", ""),
                "items": checkin_items,
                "submit_button": text_config.checkin.get("submit_button", "Submit Answers")
            }
        
        elif page_type == PageType.BREAK:
            timer_remaining = ""
            if self.session.break_timer:
                timer_remaining = self.session.break_timer.get_remaining_formatted()
            
            return {
                "title": text_config.break_page.get("title", "Break Time"),
                "message": text_config.break_page.get("message", ""),
                "end_break_button": text_config.break_page.get("end_break_button", "End Break Early"),
                "timer_remaining": timer_remaining
            }
        
        elif page_type == PageType.SEVENTH_CLIENT:
            # Seventh client is alone - no team, no panel
            client_id = self.session.get_current_client_id()
            if client_id:
                client_config = config.get_client(client_id)
                brief_text = self._load_brief_text(client_id)
                
                return {
                    "title": f"{text_config.seventh_client.get('title', 'Final Client Task')} - {client_config.name}",
                    "instruction": text_config.seventh_client.get("instruction", ""),
                    "brief": brief_text,
                    "card_editor": self.card_editor.get_card_state(),
                    "timer_remaining": self.session.get_current_client().task_timer.get_remaining_formatted() if self.session.get_current_client() and self.session.get_current_client().task_timer else ""
                }
        
        elif page_type == PageType.EXIT:
            # Generate completion code
            completion_code = self._generate_completion_code()
            
            return {
                "title": text_config.exit.get("title", "Thank You!"),
                "message": text_config.exit.get("message", ""),
                "completion_code_label": text_config.exit.get("completion_code_label", "Your completion code:"),
                "completion_code": completion_code,
                "redirect_message": text_config.exit.get("redirect_message", "")
            }
        
        return {"error": f"Unknown page type: {page_type}"}
    
    def _get_orchestrator_description(self) -> str:
        """Get orchestrator description based on panel level"""
        panel_level = self.condition.panel_level
        descriptions = {
            "coordination": "The orchestrator assigns work among the specialists and provides team status updates.",
            "task_focused": "The orchestrator assigns work and provides feedback on what is wrong with the card.",
            "developmental": "The orchestrator assigns work and explains why problems recur.",
            "none": "The orchestrator assigns work among the specialists."
        }
        return descriptions.get(panel_level, descriptions["coordination"])
    
    def _get_role_text(self) -> str:
        """Get role description based on condition"""
        role = self.condition.role
        text_config = self.config.text
        
        if role == "passive":
            return text_config.role.get("passive", "")
        elif role == "evaluative":
            return text_config.role.get("evaluative", "")
        elif role == "generative":
            return text_config.role.get("generative", "")
        else:
            return ""
    
    def _get_role_reminder(self) -> str:
        """Get role reminder for task page"""
        text_config = self.config.text
        role = self.condition.role
        
        task_dict = text_config.task if isinstance(text_config.task, dict) else {}
        role_reminder = task_dict.get("role_reminder", {})
        if isinstance(role_reminder, dict):
            return role_reminder.get(role, "")
        return ""
    
    def _get_practice_content(self) -> Dict:
        """Get practice exercises content"""
        config = self.config
        text_config = config.text
        practice_config = config.practice
        
        # Get role-specific practice
        role = self.condition.role
        role_practice = practice_config.role_practice.get(role, practice_config.role_practice.get("none", {}))
        
        exercises = []
        for i, ex_config in enumerate(practice_config.exercises):
            # Customize exercise 4 based on role
            if ex_config.id == "exercise_4":
                ex_description = role_practice.get("exercise_4_description", ex_config.description)
                check_question = role_practice.get("check_question", "")
                correct_answer = role_practice.get("correct_answer", "")
                
                exercises.append({
                    "id": ex_config.id,
                    "title": ex_config.title,
                    "description": ex_description,
                    "check_question": check_question,
                    "correct_answer": correct_answer
                })
            else:
                exercises.append({
                    "id": ex_config.id,
                    "title": ex_config.title,
                    "description": ex_config.description
                })
        
        return {
            "title": text_config.practice.get("title", "Practice Exercises"),
            "introduction": text_config.practice.get("introduction", ""),
            "exercises": exercises,
            "complete_message": text_config.practice.get("complete_message", ""),
            "current_exercise": getattr(self.session, "current_practice_step", 0),
            "practice_complete": self.session.practice_complete
        }
    
    def _load_brief_text(self, client_id: str) -> str:
        """Load brief text for a client"""
        client_config = self.config.get_client(client_id)
        brief_file = client_config.brief_file
        
        try:
            with open(brief_file, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            logger.warning(f"Brief file not found: {brief_file}")
            return f"Brief for {client_config.name} is not available."
    
    def _format_transcript(self, transcript: List[Message]) -> List[Dict]:
        """Format transcript for display"""
        formatted = []
        for msg in transcript:
            formatted.append({
                "sender": msg.sender,
                "text": msg.text,
                "timestamp": msg.timestamp,
                "is_orchestrator": msg.is_orchestrator_message
            })
        return formatted
    
    def _get_panel_content(self, client_id: str, client_state: ClientState) -> Dict:
        """Get panel content for current state"""
        if self.condition.code == "NOAI":
            return {"text": "", "word_count": 0, "sections": []}
        
        # Check if we need to regenerate panel
        if not client_state.panel_text or self._should_refresh_panel(client_state):
            panel = self.panel_generator.generate_panel(
                level=self.condition.panel_level,
                brief=self._load_brief_text(client_id),
                transcript=client_state.transcript,
                card_status=self.card_editor.card.get_all_content(),
                word_budget=tuple(self.condition.panel.word_budget),
                forbidden_language=self._get_forbidden_language(),
                show_diagnosis=self.condition.panel.show_diagnosis,
                show_principle=self.condition.panel.show_principle,
                name_unasked=self.condition.panel.name_unasked
            )
            
            client_state.panel_text = panel["text"]
            client_state.panel_sections = panel["sections"]
            client_state.panel_word_count = panel["word_count"]
            
            # Log panel shown
            self.logger.log_panel_shown(
                pid=self.pid,
                level=self.condition.panel_level,
                text=panel["text"],
                word_count=panel["word_count"],
                sections_present=panel["sections"],
                client=client_id,
                page="task"
            )
        
        return {
            "text": client_state.panel_text,
            "word_count": client_state.panel_word_count,
            "sections": client_state.panel_sections
        }
    
    def _should_refresh_panel(self, client_state: ClientState) -> bool:
        """Check if panel should be refreshed"""
        # Refresh after each orchestrator burst, at most once a minute
        if not client_state.transcript:
            return False
        
        # Check if last message was from orchestrator
        last_msg = client_state.transcript[-1]
        if last_msg.sender == "orchestrator":
            # Check time since last panel update
            # In production, track last panel update time
            return True
        
        return False
    
    def _get_forbidden_language(self) -> List[str]:
        """Get forbidden language for panel"""
        panel_config = self.config.panel
        level = self.condition.panel_level
        
        if level in panel_config:
            return panel_config[level].get("forbidden_language", [])
        return []
    
    def _generate_completion_code(self) -> str:
        """Generate a completion code for the participant"""
        import hashlib
        
        # Create a hash based on pid and session
        code_base = f"{self.pid}{self.session.session_start_time}{self.condition_code}"
        hash_obj = hashlib.md5(code_base.encode())
        hash_hex = hash_obj.hexdigest()
        
        # Take first 6 characters, uppercase
        return hash_hex[:6].upper()
    
    def handle_action(self, page_type: PageType, action: str, data: Dict) -> Dict:
        """Handle user action on a page"""
        logger.info(f"Handling action: {action} on {page_type} with data: {data}")
        
        if page_type == PageType.ENTRY:
            result = self._handle_entry_action(action, data)
        elif page_type == PageType.ORIENTATION:
            result = self._handle_orientation_action(action, data)
        elif page_type == PageType.MEET_TEAM:
            result = self._handle_meet_team_action(action, data)
        elif page_type == PageType.YOUR_ROLE:
            result = self._handle_role_action(action, data)
        elif page_type == PageType.PRACTICE:
            result = self._handle_practice_action(action, data)
        elif page_type == PageType.CLIENT_BRIEF:
            result = self._handle_client_brief_action(action, data)
        elif page_type == PageType.TASK:
            result = self._handle_task_action(action, data)
        elif page_type == PageType.SUBMISSION:
            result = self._handle_submission_action(action, data)
        elif page_type == PageType.CHECKIN:
            result = self._handle_checkin_action(action, data)
        elif page_type == PageType.BREAK:
            result = self._handle_break_action(action, data)
        elif page_type == PageType.SEVENTH_CLIENT:
            result = self._handle_seventh_client_action(action, data)
        elif page_type == PageType.EXIT:
            result = self._handle_exit_action(action, data)
        else:
            return {"success": False, "error": f"Unknown page type: {page_type}"}
        
        if result.get("next_page"):
            try:
                self.session.current_page = PageType(result["next_page"])
            except ValueError:
                pass
        
        return result
    
    def _handle_entry_action(self, action: str, data: Dict) -> Dict:
        """Handle entry page actions"""
        if action in ("validate_link", "continue"):
            # Link validation happens during entry
            return {"success": True, "valid": True, "condition": self.condition_code, "next_page": PageType.ORIENTATION.value}
        return {"success": False, "error": "Invalid action"}
    
    def _handle_orientation_action(self, action: str, data: Dict) -> Dict:
        """Handle orientation page actions"""
        if action == "continue":
            # Mark orientation as read
            self.session.mark_orientation_read()
            self.session.update_last_activity()
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "orientation")
            
            return {
                "success": True,
                "next_page": PageType.MEET_TEAM.value,
                "can_continue": True
            }
        elif action == "reading_complete":
            self.session.mark_orientation_read()
            return {"success": True, "can_continue": True}
        elif action == "timer_check":
            can_continue = self.session.can_continue_from_orientation()
            timer_remaining = self.session.orientation_timer.get_remaining_formatted() if self.session.orientation_timer else "00:00"
            return {"success": True, "expired": can_continue, "can_continue": can_continue, "timer": timer_remaining}
        
        return {"success": False, "error": "Invalid action"}
    
    def _handle_meet_team_action(self, action: str, data: Dict) -> Dict:
        """Handle meet team page actions"""
        if action == "continue":
            self.session.update_last_activity()
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "meet_team")
            
            # Check if NOAI condition - skip role page
            if self.condition.code == "NOAI":
                return {
                    "success": True,
                    "next_page": PageType.PRACTICE.value
                }
            
            return {
                "success": True,
                "next_page": PageType.YOUR_ROLE.value
            }
        
        return {"success": False, "error": "Invalid action"}
    
    def _handle_role_action(self, action: str, data: Dict) -> Dict:
        """Handle your role page actions"""
        if action == "continue":
            self.session.mark_role_shown()
            self.session.update_last_activity()
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "your_role")
            
            return {
                "success": True,
                "next_page": PageType.PRACTICE.value
            }
        elif action == "reading_complete":
            self.session.mark_role_shown()
            return {"success": True, "can_continue": True}
        elif action == "timer_check":
            can_continue = self.session.can_continue_from_role()
            timer_remaining = self.session.role_timer.get_remaining_formatted() if self.session.role_timer else "00:00"
            return {"success": True, "expired": can_continue, "can_continue": can_continue, "timer": timer_remaining}
        
        return {"success": False, "error": "Invalid action"}
    
    def _handle_practice_action(self, action: str, data: Dict) -> Dict:
        """Handle practice page actions"""
        if action == "complete_exercise":
            exercise_id = data.get("exercise_id")
            self.session.current_practice_step += 1
            
            # Log practice step
            self.logger.log_practice_step(
                pid=self.pid,
                step_id=exercise_id,
                attempts=data.get("attempts", 1),
                seconds=data.get("seconds", 0)
            )
            
            # Check if this is the last exercise
            if exercise_id == "exercise_4" or self.session.current_practice_step >= 4:
                # Handle role check
                answer = data.get("answer", "")
                correct_answer = self._get_correct_answer()
                correct = answer.lower() == correct_answer.lower()
                
                self.logger.log_role_check(
                    pid=self.pid,
                    answer=answer,
                    correct=correct,
                    sent_back=False
                )
                
                self.session.mark_practice_complete()
            
            return {"success": True, "practice_complete": self.session.practice_complete}
        
        elif action in ("practice_complete", "continue"):
            self.session.mark_practice_complete()
            self.session.update_last_activity()
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "practice")
            
            return {
                "success": True,
                "next_page": PageType.CLIENT_BRIEF.value
            }
        
        return {"success": False, "error": "Invalid action"}
    
    def _get_correct_answer(self) -> str:
        """Get correct answer for role check"""
        role_practice = self.config.practice.role_practice.get(self.condition.role, {})
        return role_practice.get("correct_answer", "")
    
    def _handle_client_brief_action(self, action: str, data: Dict) -> Dict:
        """Handle client brief page actions"""
        client_state = self.session.get_current_client()
        if not client_state:
            return {"success": False, "error": "No current client"}
        
        if action == "continue":
            # Mark brief as read
            client_state.brief_read = True
            self.session.update_last_activity()
            
            # Start task timer
            client_state.start_task_timer(self.config.session.task_timer_seconds)
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "client_brief")
            
            return {
                "success": True,
                "next_page": PageType.TASK.value
            }
        
        return {"success": False, "error": "Invalid action"}
    
    def _handle_task_action(self, action: str, data: Dict) -> Dict:
        """Handle task page actions"""
        client_state = self.session.get_current_client()
        client_id = self.session.get_current_client_id()
        
        if not client_state or not client_id:
            return {"success": False, "error": "No current client"}
        
        if action == "send_message":
            text = data.get("text", "")
            addressees = data.get("addressees", [])
            at_mention_used = data.get("at_mention_used", False)
            
            # Create message
            message = Message(
                text=text,
                sender="participant",
                timestamp=datetime.utcnow().isoformat(),
                addressees=addressees if addressees else None
            )
            
            # Add to transcript
            client_state.transcript.append(message)
            
            # Log message sent
            self.logger.log_message_sent(
                pid=self.pid,
                text=text,
                addressees=addressees,
                at_mention_used=at_mention_used,
                client=client_id,
                page="task"
            )
            
            # Route message to appropriate agents
            self._route_message(message, client_state, client_id)
            
            return {"success": True}
        
        elif action == "fold_toggle":
            block_id = data.get("block_id")
            
            # Find and toggle block
            for block in client_state.folded_blocks:
                if block.block_id == block_id:
                    block.is_open = not block.is_open
                    
                    # Log fold action
                    event_type = "fold_open" if block.is_open else "fold_close"
                    if event_type == "fold_open":
                        self.logger.log_fold_open(self.pid, block_id, client_id, "task")
                    else:
                        self.logger.log_fold_close(self.pid, block_id, client_id, "task")
                    
                    return {"success": True, "block_id": block_id, "is_open": block.is_open}
            
            return {"success": False, "error": "Block not found"}
        
        elif action == "card_action":
            # Handle card editing action
            field = data.get("field")
            span_id = data.get("span_id")
            card_action = data.get("action")  # type, delete, keep, cut, send_back
            reason = data.get("reason", "")
            
            result = self.card_editor.handle_card_action(
                action_type=card_action,
                field_id=field,
                span_id=span_id,
                reason=reason,
                text=data.get("text", ""),
                author="participant"
            )
            
            if result.get("success"):
                # Log card action
                action_log = result.get("action", {})
                self.logger.log_card_action(
                    pid=self.pid,
                    field=field,
                    span_id=span_id,
                    action=card_action,
                    reason=reason,
                    characters_added=action_log.get("characters_added"),
                    characters_removed=action_log.get("characters_removed"),
                    author=action_log.get("author"),
                    client=client_id,
                    page="task"
                )
            
            return result
        
        elif action == "timer_check":
            timer_remaining = ""
            if client_state.task_timer:
                timer_remaining = client_state.task_timer.get_remaining_formatted()
                
                if client_state.task_timer.is_expired():
                    self.logger.log_timer_expired(self.pid, client_id, "task")
                    return {"success": True, "expired": True, "timer": timer_remaining, "next_page": PageType.SUBMISSION.value}
            
            return {"success": True, "expired": False, "timer": timer_remaining}
        
        elif action == "submit":
            # Move to submission page
            self.session.update_last_activity()
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "task")
            
            return {
                "success": True,
                "next_page": PageType.SUBMISSION.value
            }
        
        return {"success": False, "error": "Invalid action"}
    
    def _route_message(self, message: Message, client_state: ClientState, client_id: str):
        """Route participant message to appropriate agents"""
        config = self.config
        client_config = config.get_client(client_id)
        
        # If message has addressees, send to those agents only
        if message.addressees:
            for addressee_raw in message.addressees:
                addressee = addressee_raw.lower()
                if addressee == "orchestrator":
                    # Handle orchestrator message
                    orchestrator = AgentFactory.get_agent("orchestrator")
                    orch_response = orchestrator.process_message(
                        message, client_state.transcript, 
                        self._load_brief_text(client_id), self.condition, self.card_editor.card
                    )
                    if orch_response:
                        client_state.transcript.append(orch_response)
                elif addressee in ["nia", "theo", "rhys", "mira"]:
                    # Send to specific specialist
                    agent = AgentFactory.get_agent(addressee)
                    agent.set_client(client_id)
                    
                    reply = agent.process_message(
                        message, client_state.transcript,
                        self._load_brief_text(client_id), self.condition, self.card_editor.card
                    )
                    client_state.transcript.append(reply)
                    
                    # Log agent reply
                    self.logger.log_agent_reply(
                        pid=self.pid,
                        agent=addressee,
                        in_reply_to=message.sender,
                        text=reply.text,
                        first_token_latency_ms=reply.first_token_latency_ms,
                        total_latency_ms=reply.total_latency_ms,
                        tokens_in=reply.tokens_in,
                        tokens_out=reply.tokens_out,
                        hidden_item_ids_disclosed=reply.hidden_item_ids_disclosed,
                        asked_for=reply.asked_for,
                        declined=reply.declined,
                        redirected_to=reply.redirected_to,
                        client=client_id,
                        page="task"
                    )
        else:
            # Message to whole team
            # Orchestrator generates coordination messages
            orchestrator = AgentFactory.get_agent("orchestrator")
            coord_messages = orchestrator.generate_coordination_message(
                message.text, client_state.transcript
            )
            
            # Create folded block for orchestrator messages
            if coord_messages:
                block = FoldedBlock()
                for coord_msg in coord_messages:
                    block.add_message(coord_msg)
                    client_state.transcript.append(coord_msg)
                    
                    # Log orchestrator message
                    self.logger.log_orch_message(
                        pid=self.pid,
                        to=coord_msg.addressees[0] if coord_msg.addressees else "",
                        text=coord_msg.text,
                        block_id=block.block_id,
                        client=client_id,
                        page="task"
                    )
                
                client_state.folded_blocks.append(block)
            
            # Each specialist decides if they should respond
            for agent_name in ["nia", "theo", "rhys", "mira"]:
                agent = AgentFactory.get_agent(agent_name)
                agent.set_client(client_id)
                
                # Check if message concerns this agent
                if self._message_concerns_agent(message.text, agent_name):
                    reply = agent.process_message(
                        message, client_state.transcript,
                        self._load_brief_text(client_id), self.condition, self.card_editor.card
                    )
                    client_state.transcript.append(reply)
                    
                    # Log agent reply
                    self.logger.log_agent_reply(
                        pid=self.pid,
                        agent=agent_name,
                        in_reply_to=message.sender,
                        text=reply.text,
                        first_token_latency_ms=reply.first_token_latency_ms,
                        total_latency_ms=reply.total_latency_ms,
                        tokens_in=reply.tokens_in,
                        tokens_out=reply.tokens_out,
                        hidden_item_ids_disclosed=reply.hidden_item_ids_disclosed,
                        asked_for=reply.asked_for,
                        declined=reply.declined,
                        redirected_to=reply.redirected_to,
                        client=client_id,
                        page="task"
                    )
    
    def _message_concerns_agent(self, text: str, agent_name: str) -> bool:
        """Check if message concerns a specific agent"""
        # Simple implementation - in production, use more sophisticated NLP
        domain_keywords = {
            "nia": ["client", "priority", "need", "requirement"],
            "theo": ["asset", "brand", "creative", "design"],
            "rhys": ["compliance", "licence", "legal", "restriction"],
            "mira": ["production", "specification", "timeline", "budget", "cost"]
        }
        
        text_lower = text.lower()
        keywords = domain_keywords.get(agent_name, [])
        return any(kw in text_lower for kw in keywords)
    
    def _handle_submission_action(self, action: str, data: Dict) -> Dict:
        """Handle submission page actions"""
        client_state = self.session.get_current_client()
        client_id = self.session.get_current_client_id()
        
        if not client_state or not client_id:
            return {"success": False, "error": "No current client"}
        
        if action in ("submit", "confirm_submission"):
            # Submit card
            result = self.card_editor.submit_card()
            
            if result.get("success"):
                card_data = result.get("card", {})
                
                # Log card submit
                self.logger.log_card_submit(
                    pid=self.pid,
                    full_card=card_data,
                    typed_share=self.card_editor.card.typed_share,
                    fields_complete=self.card_editor.card.is_complete(),
                    client=client_id,
                    page="submission"
                )
                
                # Mark submission complete
                client_state.submission_complete = True
                client_state.task_timer = None  # Stop timer
                
                self.session.update_last_activity()
                
                # Log page leave
                self.logger.log_page_leave(self.pid, "submission")
                
                return {
                    "success": True,
                    "next_page": PageType.CHECKIN.value
                }
            else:
                return result
        
        elif action == "back":
            # Go back to task page
            self.session.update_last_activity()
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "submission")
            
            return {
                "success": True,
                "next_page": PageType.TASK.value
            }
        
        return {"success": False, "error": "Invalid action"}
    
    def _handle_checkin_action(self, action: str, data: Dict) -> Dict:
        """Handle check-in page actions"""
        client_state = self.session.get_current_client()
        client_id = self.session.get_current_client_id()
        
        if not client_state or not client_id:
            return {"success": False, "error": "No current client"}
        
        if action in ("submit_answers", "submit_checkin"):
            answers = data.get("answers") or data.get("responses") or {}
            
            # Log each check-in answer
            for item_id, answer_data in answers.items():
                val = answer_data.get("value", "") if isinstance(answer_data, dict) else str(answer_data)
                ms = answer_data.get("time", 0) if isinstance(answer_data, dict) else 0
                self.logger.log_checkin_answer(
                    pid=self.pid,
                    item_id=item_id,
                    value=val,
                    milliseconds_to_answer=ms,
                    client=client_id,
                    page="checkin"
                )
            
            # Mark check-in complete
            client_state.checkin_complete = True
            self.session.update_last_activity()
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "checkin")
            
            # Check if we need to go to break after client 3 (index 2)
            if self.session.current_client_index == 2 and not self.session.break_after_client_3:
                # Start break after client 3
                self.session.break_after_client_3 = True
                self.session.start_break_timer()
                
                # Log break start
                self.logger.log_break_start(self.pid, False, client_id, "break")
                
                return {
                    "success": True,
                    "next_page": PageType.BREAK.value
                }
            else:
                # Advance to next client
                self.session.advance_to_next_client()
                self.card_editor = CardEditor(self.condition)
                
                # Check if this is the 7th client (last one)
                if self.session.current_client_index >= len(self.session.client_order) - 1:
                    # This is the 7th client - handle specially
                    return {
                        "success": True,
                        "next_page": PageType.SEVENTH_CLIENT.value
                    }
                else:
                    # Next regular client
                    return {
                        "success": True,
                        "next_page": PageType.CLIENT_BRIEF.value
                    }
        
        return {"success": False, "error": "Invalid action"}
    
    def _handle_break_action(self, action: str, data: Dict) -> Dict:
        """Handle break page actions"""
        if action in ("end_break", "continue"):
            ended_early = data.get("ended_early", True)
            
            # Log break end
            self.logger.log_break_end(self.pid, ended_early, None, "break")
            
            self.session.update_last_activity()
            
            # Log page leave
            self.logger.log_page_leave(self.pid, "break")
            
            # Continue to next client
            self.session.advance_to_next_client()
            self.card_editor = CardEditor(self.condition)
            
            return {
                "success": True,
                "next_page": PageType.CLIENT_BRIEF.value
            }
        
        elif action == "timer_check":
            if self.session.break_timer:
                if self.session.break_timer.is_expired():
                    # Break ended naturally
                    self.logger.log_break_end(self.pid, False, None, "break")
                    self.session.advance_to_next_client()
                    self.card_editor = CardEditor(self.condition)
                    return {
                        "success": True,
                        "expired": True,
                        "next_page": PageType.CLIENT_BRIEF.value
                    }
                
                return {
                    "success": True,
                    "expired": False,
                    "timer": self.session.break_timer.get_remaining_formatted()
                }
        
        return {"success": False, "error": "Invalid action"}
    
    def _handle_seventh_client_action(self, action: str, data: Dict) -> Dict:
        """Handle seventh client page actions"""
        client_state = self.session.get_current_client()
        client_id = self.session.get_current_client_id()
        
        if not client_state or not client_id:
            return {"success": False, "error": "No current client"}
        
        if action == "send_message":
            # In seventh client, no team - messages are just logged
            text = data.get("text", "")
            
            message = Message(
                text=text,
                sender="participant",
                timestamp=datetime.utcnow().isoformat()
            )
            client_state.transcript.append(message)
            
            self.logger.log_message_sent(
                pid=self.pid,
                text=text,
                addressees=[],
                at_mention_used=False,
                client=client_id,
                page="seventh_client"
            )
            
            return {"success": True}
        
        elif action == "card_action":
            # Handle card editing
            result = self.card_editor.handle_card_action(
                action_type=data.get("action"),
                field_id=data.get("field"),
                span_id=data.get("span_id"),
                reason=data.get("reason", ""),
                text=data.get("text", ""),
                author="participant"
            )
            
            if result.get("success"):
                action_log = result.get("action", {})
                self.logger.log_card_action(
                    pid=self.pid,
                    field=data.get("field"),
                    span_id=data.get("span_id"),
                    action=data.get("action"),
                    reason=data.get("reason", ""),
                    characters_added=action_log.get("characters_added"),
                    characters_removed=action_log.get("characters_removed"),
                    author=action_log.get("author"),
                    client=client_id,
                    page="seventh_client"
                )
            
            return result
        
        elif action == "submit":
            # Submit seventh client card
            result = self.card_editor.submit_card()
            
            if result.get("success"):
                card_data = result.get("card", {})
                
                self.logger.log_card_submit(
                    pid=self.pid,
                    full_card=card_data,
                    typed_share=self.card_editor.card.typed_share,
                    fields_complete=self.card_editor.card.is_complete(),
                    client=client_id,
                    page="seventh_client"
                )
                
                client_state.submission_complete = True
                self.session.seventh_client_complete = True
                self.session.update_last_activity()
                
                # Log page leave
                self.logger.log_page_leave(self.pid, "seventh_client")
                
                return {
                    "success": True,
                    "next_page": PageType.EXIT.value
                }
            else:
                return result
        
        return {"success": False, "error": "Invalid action"}
    
    def _handle_exit_action(self, action: str, data: Dict) -> Dict:
        """Handle exit page actions"""
        if action in ("complete", "continue"):
            # End session
            completion_code = self._generate_completion_code()
            
            self.logger.end_session(
                end_state="completed",
                completion_code=completion_code
            )
            
            self.session.state = SessionState.COMPLETED
            
            return {
                "success": True,
                "completion_code": completion_code
            }
        
        return {"success": False, "error": "Invalid action"}
    
    def get_next_page(self, current_page: PageType) -> PageType:
        """Get next page in flow"""
        # This is handled by the action handlers
        # Return current page as default
        return current_page
    
    def end_session(self, end_state: str = "abandoned"):
        """End session with specific state"""
        completion_code = self._generate_completion_code()
        self.logger.end_session(end_state, completion_code)
        self.session.state = SessionState.ABANDONED


# Global simulation manager
_simulations: Dict[str, SimulationV2] = {}


def get_simulation(pid: str, condition_code: str, user_agent: str, viewport: Dict) -> SimulationV2:
    """Get or create simulation for a participant"""
    if pid not in _simulations:
        _simulations[pid] = SimulationV2(pid, condition_code, user_agent, viewport)
    return _simulations[pid]


def cleanup_simulation(pid: str):
    """Clean up simulation for a participant"""
    if pid in _simulations:
        del _simulations[pid]


if __name__ == "__main__":
    # Test simulation
    print("Testing Interaction Portal v2 Simulation...")
    
    # Create a test simulation
    sim = SimulationV2(
        pid="test_001",
        condition_code="GEN-COORD",
        user_agent="Mozilla/5.0 (Test)",
        viewport={"width": 1920, "height": 1080}
    )
    
    print(f"Simulation created for {sim.pid}")
    print(f"Condition: {sim.condition_code}")
    print(f"Role: {sim.condition.role}")
    print(f"Panel level: {sim.condition.panel_level}")
    print(f"Client order: {sim.session.client_order}")
    print(f"Card order: {sim.session.specialist_card_order}")
    
    # Test getting page content
    orientation_content = sim.get_page_content(PageType.ORIENTATION)
    print(f"\nOrientation page: {orientation_content['title']}")
    
    meet_team_content = sim.get_page_content(PageType.MEET_TEAM)
    print(f"Meet Team page: {meet_team_content['title']}")
    print(f"Team members: {[m['name'] for m in meet_team_content['team_members']]}")
    
    # Test handling actions
    result = sim.handle_action(PageType.ORIENTATION, "continue", {})
    print(f"\nOrientation continue: {result}")
    
    result = sim.handle_action(PageType.MEET_TEAM, "continue", {})
    print(f"Meet Team continue: {result}")
    
    result = sim.handle_action(PageType.YOUR_ROLE, "continue", {})
    print(f"Role continue: {result}")
    
    # Test client brief
    client_brief_content = sim.get_page_content(PageType.CLIENT_BRIEF)
    print(f"\nClient Brief: {client_brief_content['title']}")
    
    result = sim.handle_action(PageType.CLIENT_BRIEF, "continue", {})
    print(f"Client Brief continue: {result}")
    
    # Test task page
    task_content = sim.get_page_content(PageType.TASK)
    print(f"\nTask page: {task_content['title']}")
    print(f"Role reminder: {task_content['role_reminder'][:50]}...")
    
    # Send a message
    result = sim.handle_action(PageType.TASK, "send_message", {
        "text": "Hello team, what do you think about this client?",
        "addressees": [],
        "at_mention_used": False
    })
    print(f"\nMessage sent: {result}")
    
    # Check transcript
    client_state = sim.session.get_current_client()
    if client_state:
        print(f"Transcript length: {len(client_state.transcript)}")
        for msg in client_state.transcript:
            print(f"  {msg.sender}: {msg.text[:50]}...")
