"""
Interaction Portal v2 - Conversation Logger

Implements comprehensive event logging as specified in section 9 of the spec.
Every event row carries: pid, UTC timestamp (ms), client, page.

Event types (20+):
- session_start / session_end
- page_enter / page_leave
- window_blur / window_focus
- practice_step
- role_check
- message_sent
- agent_reply
- orch_message
- fold_open / fold_close
- panel_shown
- card_action
- card_submit
- checkin_answer
- pack_open / pack_close
- timer_expired
- break_start / break_end
- agent_error
- scroll_sample
"""

import json
import csv
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any, Union
from pathlib import Path
import logging
import uuid

from config_loader_v2 import get_config

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Base event with common fields"""
    event_type: str
    pid: str
    timestamp: str  # UTC timestamp to millisecond
    client: Optional[str] = None
    page: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            "event_type": self.event_type,
            "pid": self.pid,
            "timestamp": self.timestamp,
            "client": self.client,
            "page": self.page
        }
    
    @classmethod
    def get_timestamp(cls) -> str:
        """Get current UTC timestamp in ISO format with milliseconds"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SessionStartEvent(Event):
    """Session start event"""
    condition_code: str
    config_version: str
    model_version: str
    user_agent: str
    viewport: Dict[str, Any]
    client_order: List[str]
    card_order: List[str]
    
    def __init__(self, pid: str, condition_code: str, config_version: str, 
                 model_version: str, user_agent: str, viewport: Dict[str, Any],
                 client_order: List[str], card_order: List[str], client: str = None, page: str = None):
        super().__init__(
            event_type="session_start",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.condition_code = condition_code
        self.config_version = config_version
        self.model_version = model_version
        self.user_agent = user_agent
        self.viewport = viewport
        self.client_order = client_order
        self.card_order = card_order
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "condition_code": self.condition_code,
            "config_version": self.config_version,
            "model_version": self.model_version,
            "user_agent": self.user_agent,
            "viewport": json.dumps(self.viewport),
            "client_order": json.dumps(self.client_order),
            "card_order": json.dumps(self.card_order)
        })
        return base


class SessionEndEvent(Event):
    """Session end event"""
    end_state: str
    completion_code: str
    duration_seconds: float
    
    def __init__(self, pid: str, end_state: str, completion_code: str, 
                 duration_seconds: float, client: str = None, page: str = None):
        super().__init__(
            event_type="session_end",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.end_state = end_state
        self.completion_code = completion_code
        self.duration_seconds = duration_seconds
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "end_state": self.end_state,
            "completion_code": self.completion_code,
            "duration_seconds": self.duration_seconds
        })
        return base


class PageEvent(Event):
    """Page enter/leave event"""
    page_id: str
    
    def __init__(self, event_type: str, pid: str, page_id: str, 
                 client: str = None, page: str = None):
        super().__init__(
            event_type=event_type,
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.page_id = page_id
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base["page_id"] = self.page_id
        return base


class WindowEvent(Event):
    """Window blur/focus event"""
    def __init__(self, event_type: str, pid: str, client: str = None, page: str = None):
        super().__init__(
            event_type=event_type,
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )


class PracticeStepEvent(Event):
    """Practice step completed"""
    step_id: str
    attempts: int
    seconds: float
    
    def __init__(self, pid: str, step_id: str, attempts: int, seconds: float,
                 client: str = None, page: str = None):
        super().__init__(
            event_type="practice_step",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.step_id = step_id
        self.attempts = attempts
        self.seconds = seconds
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "step_id": self.step_id,
            "attempts": self.attempts,
            "seconds": self.seconds
        })
        return base


class RoleCheckEvent(Event):
    """Role check event (practice exercise 4)"""
    answer: str
    correct: bool
    sent_back: bool
    
    def __init__(self, pid: str, answer: str, correct: bool, sent_back: bool,
                 client: str = None, page: str = None):
        super().__init__(
            event_type="role_check",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.answer = answer
        self.correct = correct
        self.sent_back = sent_back
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "answer": self.answer,
            "correct": self.correct,
            "sent_back": self.sent_back
        })
        return base


class MessageSentEvent(Event):
    """Participant sends a message"""
    text: str
    addressees: List[str]
    at_mention_used: bool
    character_count: int
    
    def __init__(self, pid: str, text: str, addressees: List[str], 
                 at_mention_used: bool, client: str = None, page: str = None):
        super().__init__(
            event_type="message_sent",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.text = text
        self.addressees = addressees
        self.at_mention_used = at_mention_used
        self.character_count = len(text)
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "text": self.text,
            "addressees": json.dumps(self.addressees),
            "at_mention_used": self.at_mention_used,
            "character_count": self.character_count
        })
        return base


class AgentReplyEvent(Event):
    """Specialist reply event"""
    agent: str
    in_reply_to: str
    text: str
    first_token_latency_ms: int
    total_latency_ms: int
    tokens_in: int
    tokens_out: int
    hidden_item_ids_disclosed: List[str]
    asked_for: bool
    declined: bool
    redirected_to: Optional[str]
    
    def __init__(self, pid: str, agent: str, in_reply_to: str, text: str,
                 first_token_latency_ms: int, total_latency_ms: int,
                 tokens_in: int, tokens_out: int,
                 hidden_item_ids_disclosed: List[str], asked_for: bool,
                 declined: bool, redirected_to: Optional[str] = None,
                 client: str = None, page: str = None):
        super().__init__(
            event_type="agent_reply",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.agent = agent
        self.in_reply_to = in_reply_to
        self.text = text
        self.first_token_latency_ms = first_token_latency_ms
        self.total_latency_ms = total_latency_ms
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.hidden_item_ids_disclosed = hidden_item_ids_disclosed
        self.asked_for = asked_for
        self.declined = declined
        self.redirected_to = redirected_to
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "agent": self.agent,
            "in_reply_to": self.in_reply_to,
            "text": self.text,
            "first_token_latency_ms": self.first_token_latency_ms,
            "total_latency_ms": self.total_latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "hidden_item_ids_disclosed": json.dumps(self.hidden_item_ids_disclosed),
            "asked_for": self.asked_for,
            "declined": self.declined,
            "redirected_to": self.redirected_to
        })
        return base


class OrchMessageEvent(Event):
    """Orchestrator to specialist message"""
    to: str
    text: str
    block_id: str
    
    def __init__(self, pid: str, to: str, text: str, block_id: str,
                 client: str = None, page: str = None):
        super().__init__(
            event_type="orch_message",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.to = to
        self.text = text
        self.block_id = block_id
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "to": self.to,
            "text": self.text,
            "block_id": self.block_id
        })
        return base


class FoldEvent(Event):
    """Fold open/close event"""
    block_id: str
    
    def __init__(self, event_type: str, pid: str, block_id: str,
                 client: str = None, page: str = None):
        super().__init__(
            event_type=event_type,
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.block_id = block_id
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base["block_id"] = self.block_id
        return base


class PanelShownEvent(Event):
    """Panel text appears or changes"""
    level: str
    text: str
    word_count: int
    sections_present: List[str]
    
    def __init__(self, pid: str, level: str, text: str, word_count: int,
                 sections_present: List[str], client: str = None, page: str = None):
        super().__init__(
            event_type="panel_shown",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.level = level
        self.text = text
        self.word_count = word_count
        self.sections_present = sections_present
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "level": self.level,
            "text": self.text,
            "word_count": self.word_count,
            "sections_present": json.dumps(self.sections_present)
        })
        return base


class CardActionEvent(Event):
    """Card action event"""
    field: str
    span_id: str
    action: str  # keep, cut, send_back, type, delete
    reason: Optional[str] = None
    characters_added: Optional[int] = None
    characters_removed: Optional[int] = None
    author: Optional[str] = None
    
    def __init__(self, pid: str, field: str, span_id: str, action: str,
                 reason: Optional[str] = None, characters_added: Optional[int] = None,
                 characters_removed: Optional[int] = None, author: Optional[str] = None,
                 client: str = None, page: str = None):
        super().__init__(
            event_type="card_action",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.field = field
        self.span_id = span_id
        self.action = action
        self.reason = reason
        self.characters_added = characters_added
        self.characters_removed = characters_removed
        self.author = author
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "field": self.field,
            "span_id": self.span_id,
            "action": self.action,
            "reason": self.reason,
            "characters_added": self.characters_added,
            "characters_removed": self.characters_removed,
            "author": self.author
        })
        return base


class CardSubmitEvent(Event):
    """Card submission event"""
    full_card: Dict
    typed_share: str
    fields_complete: bool
    
    def __init__(self, pid: str, full_card: Dict, typed_share: str, 
                 fields_complete: bool, client: str = None, page: str = None):
        super().__init__(
            event_type="card_submit",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.full_card = full_card
        self.typed_share = typed_share
        self.fields_complete = fields_complete
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "full_card": json.dumps(self.full_card),
            "typed_share": self.typed_share,
            "fields_complete": self.fields_complete
        })
        return base


class CheckinAnswerEvent(Event):
    """Check-in answer event"""
    item_id: str
    value: str
    milliseconds_to_answer: float
    
    def __init__(self, pid: str, item_id: str, value: str, 
                 milliseconds_to_answer: float, client: str = None, page: str = None):
        super().__init__(
            event_type="checkin_answer",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.item_id = item_id
        self.value = value
        self.milliseconds_to_answer = milliseconds_to_answer
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "item_id": self.item_id,
            "value": self.value,
            "milliseconds_to_answer": self.milliseconds_to_answer
        })
        return base


class PackEvent(Event):
    """Reference pack open/close event (NOAI condition)"""
    section: str
    
    def __init__(self, event_type: str, pid: str, section: str,
                 client: str = None, page: str = None):
        super().__init__(
            event_type=event_type,
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.section = section
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base["section"] = self.section
        return base


class TimerExpiredEvent(Event):
    """Timer expired event"""
    def __init__(self, pid: str, client: str = None, page: str = None):
        super().__init__(
            event_type="timer_expired",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )


class BreakEvent(Event):
    """Break start/end event"""
    ended_early: bool
    
    def __init__(self, event_type: str, pid: str, ended_early: bool,
                 client: str = None, page: str = None):
        super().__init__(
            event_type=event_type,
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.ended_early = ended_early
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base["ended_early"] = self.ended_early
        return base


class AgentErrorEvent(Event):
    """Agent error event"""
    agent: str
    error_type: str
    retry_number: int
    
    def __init__(self, pid: str, agent: str, error_type: str, 
                 retry_number: int, client: str = None, page: str = None):
        super().__init__(
            event_type="agent_error",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.agent = agent
        self.error_type = error_type
        self.retry_number = retry_number
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base.update({
            "agent": self.agent,
            "error_type": self.error_type,
            "retry_number": self.retry_number
        })
        return base


class ScrollSampleEvent(Event):
    """Scroll position sample event"""
    scroll_position: float
    
    def __init__(self, pid: str, scroll_position: float,
                 client: str = None, page: str = None):
        super().__init__(
            event_type="scroll_sample",
            pid=pid,
            timestamp=Event.get_timestamp(),
            client=client,
            page=page
        )
        self.scroll_position = scroll_position
    
    def to_dict(self) -> Dict:
        base = super().to_dict()
        base["scroll_position"] = self.scroll_position
        return base


@dataclass
class SessionRecord:
    """Complete session record"""
    pid: str
    condition_code: str
    config_version: str
    model_version: str
    start_time: str
    end_time: str
    end_state: str
    resume_count: int
    specialist_card_order: List[str]
    client_order: List[str]
    completion_code: str
    
    def to_dict(self) -> Dict:
        return {
            "pid": self.pid,
            "condition_code": self.condition_code,
            "config_version": self.config_version,
            "model_version": self.model_version,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "end_state": self.end_state,
            "resume_count": self.resume_count,
            "specialist_card_order": json.dumps(self.specialist_card_order),
            "client_order": json.dumps(self.client_order),
            "completion_code": self.completion_code
        }


class ConversationLoggerV2:
    """Logger for all events in Interaction Portal v2"""
    
    def __init__(self, config_path: str = "v2/config_v2.yaml"):
        self.config = get_config(config_path)
        self.log_file = self.config.logging.log_file
        self.json_log_file = self.config.logging.json_log_file
        self.session_store = self.config.logging.session_store
        self.enabled_events = self.config.logging.log_events
        
        # Ensure directories exist
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        os.makedirs(os.path.dirname(self.json_log_file), exist_ok=True)
        os.makedirs(os.path.dirname(self.session_store), exist_ok=True)
        
        # Session data
        self.current_session: Optional[Dict] = None
        self.events: List[Event] = []
        
        logger.info(f"Logger initialized: {self.log_file}")
    
    def start_session(self, pid: str, condition_code: str, user_agent: str, 
                     viewport: Dict, client_order: List[str], card_order: List[str]) -> SessionStartEvent:
        """Start a new session and log session_start event"""
        config = self.config
        
        event = SessionStartEvent(
            pid=pid,
            condition_code=condition_code,
            config_version=config.config_version,
            model_version=config.model.version,
            user_agent=user_agent,
            viewport=viewport,
            client_order=client_order,
            card_order=card_order,
            page="entry"
        )
        
        self.current_session = {
            "pid": pid,
            "condition_code": condition_code,
            "start_time": event.timestamp,
            "user_agent": user_agent,
            "viewport": viewport,
            "client_order": client_order,
            "card_order": card_order,
            "events": [],
            "resume_count": 0
        }
        
        self.log_event(event)
        return event
    
    def end_session(self, end_state: str, completion_code: str) -> SessionEndEvent:
        """End current session and log session_end event"""
        if not self.current_session:
            raise ValueError("No active session")
        
        start_time = datetime.fromisoformat(self.current_session["start_time"].replace("Z", "+00:00"))
        end_time = datetime.now(timezone.utc)
        duration_seconds = (end_time - start_time).total_seconds()
        
        event = SessionEndEvent(
            pid=self.current_session["pid"],
            end_state=end_state,
            completion_code=completion_code,
            duration_seconds=duration_seconds,
            page="exit"
        )
        
        # Save session record
        self._save_session_record(end_state, completion_code, duration_seconds)
        
        self.log_event(event)
        
        # Clear current session
        self.current_session = None
        self.events = []
        
        return event
    
    def _save_session_record(self, end_state: str, completion_code: str, duration_seconds: float):
        """Save complete session record"""
        if not self.current_session:
            return
        
        session_record = SessionRecord(
            pid=self.current_session["pid"],
            condition_code=self.current_session["condition_code"],
            config_version=self.config.config_version,
            model_version=self.config.model.version,
            start_time=self.current_session["start_time"],
            end_time=Event.get_timestamp(),
            end_state=end_state,
            resume_count=self.current_session.get("resume_count", 0),
            specialist_card_order=self.current_session.get("card_order", []),
            client_order=self.current_session.get("client_order", []),
            completion_code=completion_code
        )
        
        # Save to session store
        self._append_to_json_file(self.session_store, session_record.to_dict())
    
    def log_event(self, event: Event):
        """Log an event if its type is enabled"""
        if not self.enabled_events.get(event.event_type, True):
            return
        
        # Add to current session events
        if self.current_session:
            self.current_session["events"].append(event.to_dict())
        
        # Add to in-memory events
        self.events.append(event)
        
        # Write to log files
        self._write_to_log_file(event)
        self._write_to_json_log_file(event)
    
    def _write_to_log_file(self, event: Event):
        """Write event to plain text log file"""
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(f"{event.to_dict()}\n")
        except Exception as e:
            logger.error(f"Error writing to log file: {e}")
    
    def _write_to_json_log_file(self, event: Event):
        """Write event to JSON lines file"""
        try:
            with open(self.json_log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(event.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"Error writing to JSON log file: {e}")
    
    def _append_to_json_file(self, filepath: str, data: Dict):
        """Append data to JSON file (for session store)"""
        try:
            # Check if file exists and read existing data
            sessions = []
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    try:
                        sessions = json.load(f)
                        if not isinstance(sessions, list):
                            sessions = []
                    except json.JSONDecodeError:
                        sessions = []
            
            sessions.append(data)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(sessions, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving session record: {e}")
    
    def log_page_enter(self, pid: str, page_id: str, client: str = None):
        """Log page enter event"""
        event = PageEvent("page_enter", pid, page_id, client, page_id)
        self.log_event(event)
    
    def log_page_leave(self, pid: str, page_id: str, client: str = None):
        """Log page leave event"""
        event = PageEvent("page_leave", pid, page_id, client, page_id)
        self.log_event(event)
    
    def log_window_blur(self, pid: str, client: str = None, page: str = None):
        """Log window blur event"""
        event = WindowEvent("window_blur", pid, client, page)
        self.log_event(event)
    
    def log_window_focus(self, pid: str, client: str = None, page: str = None):
        """Log window focus event"""
        event = WindowEvent("window_focus", pid, client, page)
        self.log_event(event)
    
    def log_practice_step(self, pid: str, step_id: str, attempts: int, seconds: float,
                         client: str = None, page: str = None):
        """Log practice step event"""
        event = PracticeStepEvent(pid, step_id, attempts, seconds, client, page)
        self.log_event(event)
    
    def log_role_check(self, pid: str, answer: str, correct: bool, sent_back: bool,
                       client: str = None, page: str = None):
        """Log role check event"""
        event = RoleCheckEvent(pid, answer, correct, sent_back, client, page)
        self.log_event(event)
    
    def log_message_sent(self, pid: str, text: str, addressees: List[str], 
                         at_mention_used: bool, client: str = None, page: str = None):
        """Log message sent event"""
        event = MessageSentEvent(pid, text, addressees, at_mention_used, client, page)
        self.log_event(event)
    
    def log_agent_reply(self, pid: str, agent: str, in_reply_to: str, text: str,
                       first_token_latency_ms: int, total_latency_ms: int,
                       tokens_in: int, tokens_out: int,
                       hidden_item_ids_disclosed: List[str], asked_for: bool,
                       declined: bool, redirected_to: Optional[str] = None,
                       client: str = None, page: str = None):
        """Log agent reply event"""
        event = AgentReplyEvent(
            pid, agent, in_reply_to, text,
            first_token_latency_ms, total_latency_ms,
            tokens_in, tokens_out,
            hidden_item_ids_disclosed, asked_for, declined, redirected_to,
            client, page
        )
        self.log_event(event)
    
    def log_orch_message(self, pid: str, to: str, text: str, block_id: str,
                         client: str = None, page: str = None):
        """Log orchestrator message event"""
        event = OrchMessageEvent(pid, to, text, block_id, client, page)
        self.log_event(event)
    
    def log_fold_open(self, pid: str, block_id: str, client: str = None, page: str = None):
        """Log fold open event"""
        event = FoldEvent("fold_open", pid, block_id, client, page)
        self.log_event(event)
    
    def log_fold_close(self, pid: str, block_id: str, client: str = None, page: str = None):
        """Log fold close event"""
        event = FoldEvent("fold_close", pid, block_id, client, page)
        self.log_event(event)
    
    def log_panel_shown(self, pid: str, level: str, text: str, word_count: int,
                       sections_present: List[str], client: str = None, page: str = None):
        """Log panel shown event"""
        event = PanelShownEvent(pid, level, text, word_count, sections_present, client, page)
        self.log_event(event)
    
    def log_card_action(self, pid: str, field: str, span_id: str, action: str,
                        reason: Optional[str] = None, characters_added: Optional[int] = None,
                        characters_removed: Optional[int] = None, author: Optional[str] = None,
                        client: str = None, page: str = None):
        """Log card action event"""
        event = CardActionEvent(
            pid, field, span_id, action, reason, characters_added,
            characters_removed, author, client, page
        )
        self.log_event(event)
    
    def log_card_submit(self, pid: str, full_card: Dict, typed_share: str, 
                        fields_complete: bool, client: str = None, page: str = None):
        """Log card submit event"""
        event = CardSubmitEvent(pid, full_card, typed_share, fields_complete, client, page)
        self.log_event(event)
    
    def log_checkin_answer(self, pid: str, item_id: str, value: str, 
                          milliseconds_to_answer: float, client: str = None, page: str = None):
        """Log check-in answer event"""
        event = CheckinAnswerEvent(pid, item_id, value, milliseconds_to_answer, client, page)
        self.log_event(event)
    
    def log_pack_open(self, pid: str, section: str, client: str = None, page: str = None):
        """Log pack open event (NOAI)"""
        event = PackEvent("pack_open", pid, section, client, page)
        self.log_event(event)
    
    def log_pack_close(self, pid: str, section: str, client: str = None, page: str = None):
        """Log pack close event (NOAI)"""
        event = PackEvent("pack_close", pid, section, client, page)
        self.log_event(event)
    
    def log_timer_expired(self, pid: str, client: str = None, page: str = None):
        """Log timer expired event"""
        event = TimerExpiredEvent(pid, client, page)
        self.log_event(event)
    
    def log_break_start(self, pid: str, ended_early: bool = False, 
                       client: str = None, page: str = None):
        """Log break start event"""
        event = BreakEvent("break_start", pid, ended_early, client, page)
        self.log_event(event)
    
    def log_break_end(self, pid: str, ended_early: bool = False,
                      client: str = None, page: str = None):
        """Log break end event"""
        event = BreakEvent("break_end", pid, ended_early, client, page)
        self.log_event(event)
    
    def log_agent_error(self, pid: str, agent: str, error_type: str, 
                       retry_number: int, client: str = None, page: str = None):
        """Log agent error event"""
        event = AgentErrorEvent(pid, agent, error_type, retry_number, client, page)
        self.log_event(event)
    
    def log_scroll_sample(self, pid: str, scroll_position: float,
                          client: str = None, page: str = None):
        """Log scroll sample event"""
        event = ScrollSampleEvent(pid, scroll_position, client, page)
        self.log_event(event)
    
    def export_events(self, format: str = "csv") -> str:
        """Export all events in specified format"""
        if format == "csv":
            return self._export_csv()
        elif format == "json":
            return self._export_json()
        else:
            raise ValueError(f"Unsupported format: {format}")
    
    def _export_csv(self) -> str:
        """Export events as CSV"""
        import io
        
        output = io.StringIO()
        
        # Get all field names from all events
        fieldnames = set()
        for event in self.events:
            fieldnames.update(event.to_dict().keys())
        
        writer = csv.DictWriter(output, fieldnames=sorted(fieldnames))
        writer.writeheader()
        
        for event in self.events:
            writer.writerow(event.to_dict())
        
        return output.getvalue()
    
    def _export_json(self) -> str:
        """Export events as JSON"""
        return json.dumps([e.to_dict() for e in self.events], indent=2)
    
    def backup(self):
        """Create backup of logs"""
        import shutil
        from datetime import datetime
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(self.config.admin.backup_directory, timestamp)
        
        os.makedirs(backup_dir, exist_ok=True)
        
        # Backup log files
        if os.path.exists(self.log_file):
            shutil.copy(self.log_file, os.path.join(backup_dir, "events.log"))
        if os.path.exists(self.json_log_file):
            shutil.copy(self.json_log_file, os.path.join(backup_dir, "events.jsonl"))
        if os.path.exists(self.session_store):
            shutil.copy(self.session_store, os.path.join(backup_dir, "sessions.json"))
        
        logger.info(f"Backup created: {backup_dir}")
        
        # Clean old backups
        self._clean_old_backups()
    
    def _clean_old_backups(self):
        """Clean backups older than max_backups days"""
        import shutil
        from datetime import datetime, timedelta
        
        backup_dir = self.config.admin.backup_directory
        if not os.path.exists(backup_dir):
            return
        
        max_backups = self.config.admin.max_backups
        cutoff = datetime.now() - timedelta(days=max_backups)
        
        for item in os.listdir(backup_dir):
            item_path = os.path.join(backup_dir, item)
            if os.path.isdir(item_path):
                try:
                    # Extract timestamp from directory name
                    item_time = datetime.strptime(item, "%Y%m%d_%H%M%S")
                    if item_time < cutoff:
                        shutil.rmtree(item_path)
                        logger.info(f"Removed old backup: {item_path}")
                except ValueError:
                    pass


# Global logger instance
_logger: Optional[ConversationLoggerV2] = None


def get_logger(config_path: str = "v2/config_v2.yaml") -> ConversationLoggerV2:
    """Get or create global logger instance"""
    global _logger
    if _logger is None:
        _logger = ConversationLoggerV2(config_path)
    return _logger


def reset_logger():
    """Reset global logger (for testing)"""
    global _logger
    _logger = None


if __name__ == "__main__":
    # Test logger
    logger = get_logger()
    
    # Start a session
    session_event = logger.start_session(
        pid="test_pid_001",
        condition_code="GEN-COORD",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        viewport={"width": 1920, "height": 1080},
        client_order=["C1", "C2", "C3", "C4", "C5", "C6", "C7"],
        card_order=["nia", "theo", "rhys", "mira"]
    )
    print(f"Session started: {session_event.to_dict()}")
    
    # Log some events
    logger.log_page_enter("test_pid_001", "orientation", "C1")
    logger.log_message_sent("test_pid_001", "Hello team!", ["all"], False, "C1", "task")
    logger.log_card_action("test_pid_001", "field_1", "span_1", "type", characters_added=10, author="participant", client="C1", page="task")
    
    # End session
    end_event = logger.end_session("completed", "ABC123")
    print(f"Session ended: {end_event.to_dict()}")
    
    # Export events
    csv_export = logger.export_events("csv")
    print(f"\nCSV export:\n{csv_export[:200]}...")
    
    json_export = logger.export_events("json")
    print(f"\nJSON export:\n{json_export[:200]}...")
