"""
Interaction Portal v2 - Card Editor with Provenance Tracking

Implements the card editor with span-level provenance tracking.
Supports three modes: read-only (passive), evaluative (keep/cut/send-back), 
generative (typed text with minimum share).

From spec section 6.7:
- Card editor behaves differently per role (Figure 5)
- Every span of text on the card keeps its author and history
- Provenance tracking for all changes
"""

import uuid
import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime
from enum import Enum

from config_loader_v2 import get_config, ConditionConfig


class CardActionType(Enum):
    """Types of card actions"""
    TYPE = "type"
    DELETE = "delete"
    KEEP = "keep"
    CUT = "cut"
    SEND_BACK = "send_back"


class CardFieldStatus(Enum):
    """Status of a card field"""
    EMPTY = "empty"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass
class Span:
    """A span of text with provenance"""
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    author: str = ""  # "participant", "nia", "theo", "rhys", "mira", "orchestrator"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    history: List[Dict] = field(default_factory=list)  # Previous versions
    
    def __post_init__(self):
        if not self.span_id:
            self.span_id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat()
    
    def add_text(self, text: str, author: str) -> 'Span':
        """Add text to this span (for generative mode)"""
        old_content = self.text
        self.text += text
        self.history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "author": self.author,
            "old_text": old_content,
            "new_text": self.text,
            "action": "type",
            "characters_added": len(text)
        })
        self.author = author
        return self
    
    def replace_text(self, text: str, author: str) -> 'Span':
        """Replace span text"""
        self.history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "author": self.author,
            "old_text": self.text,
            "new_text": text,
            "action": "replace",
            "characters_added": len(text),
            "characters_removed": len(self.text)
        })
        self.text = text
        self.author = author
        return self
    
    def delete(self) -> Dict:
        """Delete this span, return deletion info"""
        return {
            "span_id": self.span_id,
            "text": self.text,
            "author": self.author,
            "action": "delete",
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "span_id": self.span_id,
            "text": self.text,
            "author": self.author,
            "created_at": self.created_at,
            "history": self.history
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Span':
        """Create span from dictionary"""
        return cls(
            span_id=data.get("span_id", str(uuid.uuid4())),
            text=data.get("text", ""),
            author=data.get("author", ""),
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
            history=data.get("history", [])
        )


@dataclass
class CardField:
    """A card field containing multiple spans"""
    field_id: str
    label: str
    placeholder: str = ""
    required: bool = True
    spans: List[Span] = field(default_factory=list)
    status: CardFieldStatus = CardFieldStatus.EMPTY
    
    def __post_init__(self):
        if not self.spans:
            # Start with empty span
            self.spans = [Span(author="")]
    
    def get_content(self) -> str:
        """Get full content of this field"""
        return "".join(span.text for span in self.spans)
    
    def set_content(self, text: str, author: str):
        """Set field content (replaces all spans)"""
        # Save history of current spans
        for span in self.spans:
            span.history.append({
                "timestamp": datetime.utcnow().isoformat(),
                "action": "field_replace"
            })
        
        # Create new span with full content
        self.spans = [Span(text=text, author=author)]
        self._update_status()
    
    def add_span(self, text: str, author: str) -> Span:
        """Add a new span to this field"""
        span = Span(text=text, author=author)
        self.spans.append(span)
        self._update_status()
        return span
    
    def insert_span(self, index: int, text: str, author: str) -> Span:
        """Insert a span at specific position"""
        span = Span(text=text, author=author)
        self.spans.insert(index, span)
        self._update_status()
        return span
    
    def type_text(self, text: str, author: str, span_index: int = -1):
        """Type text into field (generative mode)"""
        if span_index >= 0 and span_index < len(self.spans):
            # Type into existing span
            self.spans[span_index].add_text(text, author)
        else:
            # Add to last span or create new one
            if self.spans:
                self.spans[-1].add_text(text, author)
            else:
                self.spans = [Span(text=text, author=author)]
        self._update_status()
    
    def delete_span(self, span_index: int) -> Optional[Dict]:
        """Delete a span from this field"""
        if 0 <= span_index < len(self.spans):
            deletion_info = self.spans[span_index].delete()
            del self.spans[span_index]
            self._update_status()
            return deletion_info
        return None
    
    def keep_span(self, span_index: int) -> bool:
        """Mark span as kept (evaluative mode)"""
        if 0 <= span_index < len(self.spans):
            self.spans[span_index].history.append({
                "timestamp": datetime.utcnow().isoformat(),
                "action": "keep",
                "author": self.spans[span_index].author
            })
            self._update_status()
            return True
        return False
    
    def cut_span(self, span_index: int) -> Optional[Dict]:
        """Cut a span (evaluative mode) - mark for removal"""
        if 0 <= span_index < len(self.spans):
            cut_info = {
                "span_id": self.spans[span_index].span_id,
                "text": self.spans[span_index].text,
                "author": self.spans[span_index].author,
                "action": "cut",
                "timestamp": datetime.utcnow().isoformat()
            }
            # In cut mode, we mark it but don't remove immediately
            # The actual removal happens on send-back
            self.spans[span_index].history.append({
                "timestamp": datetime.utcnow().isoformat(),
                "action": "cut"
            })
            self._update_status()
            return cut_info
        return None
    
    def send_back_span(self, span_index: int, reason: str = "") -> Optional[Dict]:
        """Send back a span for revision (evaluative mode)"""
        if 0 <= span_index < len(self.spans):
            send_back_info = {
                "span_id": self.spans[span_index].span_id,
                "text": self.spans[span_index].text,
                "author": self.spans[span_index].author,
                "action": "send_back",
                "reason": reason,
                "timestamp": datetime.utcnow().isoformat()
            }
            self.spans[span_index].history.append({
                "timestamp": datetime.utcnow().isoformat(),
                "action": "send_back",
                "reason": reason
            })
            self._update_status()
            return send_back_info
        return None
    
    def is_empty(self) -> bool:
        """Check if field is empty"""
        return not any(span.text.strip() for span in self.spans)
    
    def is_complete(self) -> bool:
        """Check if field is complete"""
        return self.status == CardFieldStatus.COMPLETE
    
    def _update_status(self):
        """Update field status based on content"""
        content = self.get_content()
        if not content.strip():
            self.status = CardFieldStatus.EMPTY
        elif len(content.split()) >= 10:  # Arbitrary threshold
            self.status = CardFieldStatus.COMPLETE
        else:
            self.status = CardFieldStatus.PARTIAL
    
    def get_provenance(self) -> List[Dict]:
        """Get provenance for all spans in this field"""
        provenance = []
        for span in self.spans:
            provenance.append({
                "span_id": span.span_id,
                "author": span.author,
                "text": span.text,
                "created_at": span.created_at,
                "history": span.history
            })
        return provenance
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "field_id": self.field_id,
            "label": self.label,
            "placeholder": self.placeholder,
            "required": self.required,
            "content": self.get_content(),
            "status": self.status.value,
            "spans": [span.to_dict() for span in self.spans]
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'CardField':
        """Create card field from dictionary"""
        field = cls(
            field_id=data["field_id"],
            label=data["label"],
            placeholder=data.get("placeholder", ""),
            required=data.get("required", True),
            status=CardFieldStatus(data.get("status", "empty"))
        )
        field.spans = [Span.from_dict(span_data) for span_data in data.get("spans", [])]
        if not field.spans:
            field.spans = [Span(author="")]
        return field


class Card:
    """Complete card with 5 fields and provenance tracking"""
    
    def __init__(self, field_configs: Optional[List[Dict]] = None):
        if field_configs:
            for config in field_configs:
                field = CardField(
                    field_id=config["id"],
                    label=config["label"],
                    placeholder=config.get("placeholder", ""),
                    required=config.get("required", True)
                )
                self.fields[config["id"]] = field
        else:
            # Use default fields from config
            config = get_config()
            for field_config in config.card_editor.fields:
                field = CardField(
                    field_id=field_config.id,
                    label=field_config.label,
                    placeholder=field_config.placeholder,
                    required=field_config.required
                )
                self.fields[field_config.id] = field
    
    def get_field(self, field_id: str) -> Optional[CardField]:
        """Get field by ID"""
        return self.fields.get(field_id)
    
    def get_all_content(self) -> Dict[str, str]:
        """Get content of all fields"""
        return {fid: field.get_content() for fid, field in self.fields.items()}
    
    def is_complete(self) -> bool:
        """Check if all required fields have content"""
        for fid, field in self.fields.items():
            if field.required and field.is_empty():
                return False
        return True
    
    def get_typed_share_length(self) -> int:
        """Get length of typed share"""
        return len(self.typed_share)
    
    def add_typed_share(self, text: str, author: str):
        """Add to typed share (generative mode)"""
        self.typed_share += text
        self.typed_share_author = author
    
    def set_typed_share(self, text: str, author: str):
        """Set typed share"""
        self.typed_share = text
        self.typed_share_author = author
    
    def submit(self):
        """Mark card as submitted"""
        self.is_submitted = True
        self.submission_timestamp = datetime.utcnow().isoformat()
    
    def get_full_card_with_provenance(self) -> Dict:
        """Get full card with span-level provenance"""
        return {
            "fields": {fid: field.to_dict() for fid, field in self.fields.items()},
            "typed_share": {
                "content": self.typed_share,
                "author": self.typed_share_author
            },
            "is_submitted": self.is_submitted,
            "submission_timestamp": self.submission_timestamp,
            "fields_complete": self.is_complete()
        }
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return self.get_full_card_with_provenance()
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Card':
        """Create card from dictionary"""
        card = cls()
        card.fields = {}
        for fid, field_data in data.get("fields", {}).items():
            card.fields[fid] = CardField.from_dict(field_data)
        card.typed_share = data.get("typed_share", {}).get("content", "")
        card.typed_share_author = data.get("typed_share", {}).get("author", "")
        card.is_submitted = data.get("is_submitted", False)
        card.submission_timestamp = data.get("submission_timestamp")
        return card


class CardEditor:
    """Card editor with role-specific behavior"""
    
    def __init__(self, condition: ConditionConfig):
        self.condition = condition
        self.card = Card()
        self._initialize_editor()
    
    def _initialize_editor(self):
        """Initialize editor based on condition role"""
        self.role = self.condition.role
        self.editor_type = self.condition.editor.type
        self.min_typed_share = self.condition.editor.min_typed_share
        self.send_back_needs_reason = self.condition.editor.send_back_needs_reason
        
        # Set up allowed actions based on role
        self.allowed_actions = self._get_allowed_actions()
    
    def _get_allowed_actions(self) -> List[CardActionType]:
        """Get allowed actions based on role"""
        if self.role == "passive":
            return [CardActionType.TYPE]  # Read-only, can only view
        elif self.role == "evaluative":
            return [
                CardActionType.KEEP,
                CardActionType.CUT,
                CardActionType.SEND_BACK
            ]
        elif self.role == "generative":
            return [
                CardActionType.TYPE,
                CardActionType.DELETE
            ]
        else:  # none (NOAI)
            return [
                CardActionType.TYPE,
                CardActionType.DELETE
            ]
    
    def can_submit(self) -> Tuple[bool, str]:
        """Check if card can be submitted, return (can_submit, reason)"""
        if not self.card.is_complete():
            return False, "Not all required fields are complete"
        
        if self.role == "generative" and self.min_typed_share:
            if self.card.get_typed_share_length() < self.min_typed_share:
                return False, f"Typed share must be at least {self.min_typed_share} characters"
        
        return True, ""
    
    def type_text(self, field_id: str, text: str, author: str = "participant") -> Dict:
        """Type text into a field (generative mode)"""
        if CardActionType.TYPE not in self.allowed_actions:
            return {"success": False, "error": "Type action not allowed in this role"}
        
        field = self.card.get_field(field_id)
        if not field:
            return {"success": False, "error": f"Field {field_id} not found"}
        
        field.type_text(text, author)
        
        # Log action
        action_log = {
            "field": field_id,
            "span_id": field.spans[-1].span_id if field.spans else "",
            "action": "type",
            "characters_added": len(text),
            "author": author,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        return {"success": True, "action": action_log}
    
    def delete_text(self, field_id: str, span_index: int = -1) -> Dict:
        """Delete text from a field"""
        if CardActionType.DELETE not in self.allowed_actions:
            return {"success": False, "error": "Delete action not allowed in this role"}
        
        field = self.card.get_field(field_id)
        if not field:
            return {"success": False, "error": f"Field {field_id} not found"}
        
        if span_index >= 0:
            # Delete specific span
            deletion_info = field.delete_span(span_index)
            if deletion_info:
                action_log = {
                    "field": field_id,
                    "span_id": deletion_info["span_id"],
                    "action": "delete",
                    "characters_removed": len(deletion_info["text"]),
                    "author": deletion_info["author"],
                    "timestamp": datetime.utcnow().isoformat()
                }
                return {"success": True, "action": action_log}
        else:
            # Clear last span
            if field.spans:
                last_span = field.spans[-1]
                deletion_info = last_span.delete()
                field.spans = field.spans[:-1]
                action_log = {
                    "field": field_id,
                    "span_id": deletion_info["span_id"],
                    "action": "delete",
                    "characters_removed": len(deletion_info["text"]),
                    "author": deletion_info["author"],
                    "timestamp": datetime.utcnow().isoformat()
                }
                return {"success": True, "action": action_log}
        
        return {"success": False, "error": "No text to delete"}
    
    def keep_span(self, field_id: str, span_index: int) -> Dict:
        """Keep a span (evaluative mode)"""
        if CardActionType.KEEP not in self.allowed_actions:
            return {"success": False, "error": "Keep action not allowed in this role"}
        
        field = self.card.get_field(field_id)
        if not field:
            return {"success": False, "error": f"Field {field_id} not found"}
        
        if field.keep_span(span_index):
            action_log = {
                "field": field_id,
                "span_id": field.spans[span_index].span_id,
                "action": "keep",
                "author": field.spans[span_index].author,
                "timestamp": datetime.utcnow().isoformat()
            }
            return {"success": True, "action": action_log}
        
        return {"success": False, "error": "Invalid span index"}
    
    def cut_span(self, field_id: str, span_index: int) -> Dict:
        """Cut a span (evaluative mode)"""
        if CardActionType.CUT not in self.allowed_actions:
            return {"success": False, "error": "Cut action not allowed in this role"}
        
        field = self.card.get_field(field_id)
        if not field:
            return {"success": False, "error": f"Field {field_id} not found"}
        
        cut_info = field.cut_span(span_index)
        if cut_info:
            action_log = {
                "field": field_id,
                "span_id": cut_info["span_id"],
                "action": "cut",
                "author": cut_info["author"],
                "timestamp": datetime.utcnow().isoformat()
            }
            return {"success": True, "action": action_log}
        
        return {"success": False, "error": "Invalid span index"}
    
    def send_back_span(self, field_id: str, span_index: int, reason: str = "") -> Dict:
        """Send back a span for revision (evaluative mode)"""
        if CardActionType.SEND_BACK not in self.allowed_actions:
            return {"success": False, "error": "Send-back action not allowed in this role"}
        
        if self.send_back_needs_reason and not reason.strip():
            return {"success": False, "error": "Reason required for send-back"}
        
        field = self.card.get_field(field_id)
        if not field:
            return {"success": False, "error": f"Field {field_id} not found"}
        
        send_back_info = field.send_back_span(span_index, reason)
        if send_back_info:
            action_log = {
                "field": field_id,
                "span_id": send_back_info["span_id"],
                "action": "send_back",
                "reason": reason,
                "author": send_back_info["author"],
                "timestamp": datetime.utcnow().isoformat()
            }
            return {"success": True, "action": action_log}
        
        return {"success": False, "error": "Invalid span index"}
    
    def set_field_content(self, field_id: str, content: str, author: str = "participant") -> Dict:
        """Set entire field content (for read-only display or bulk edit)"""
        field = self.card.get_field(field_id)
        if not field:
            return {"success": False, "error": f"Field {field_id} not found"}
        
        # Check if allowed
        if self.role == "passive":
            return {"success": False, "error": "Cannot edit in passive mode"}
        
        old_content = field.get_content()
        field.set_content(content, author)
        
        action_log = {
            "field": field_id,
            "action": "set_content",
            "characters_added": len(content),
            "characters_removed": len(old_content),
            "author": author,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        return {"success": True, "action": action_log}
    
    def add_typed_share(self, text: str, author: str = "participant") -> Dict:
        """Add to typed share (generative mode)"""
        if self.role != "generative":
            return {"success": False, "error": "Typed share only available in generative mode"}
        
        self.card.add_typed_share(text, author)
        
        action_log = {
            "action": "typed_share",
            "characters_added": len(text),
            "author": author,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        return {"success": True, "action": action_log}
    
    def submit_card(self) -> Dict:
        """Submit the card"""
        can_submit, reason = self.can_submit()
        if not can_submit:
            return {"success": False, "error": reason}
        
        self.card.submit()
        
        submission_data = self.card.get_full_card_with_provenance()
        submission_data["submission_timestamp"] = datetime.utcnow().isoformat()
        
        return {"success": True, "card": submission_data}
    
    def get_card_state(self) -> Dict:
        """Get current card state for display"""
        return {
            "fields": {
                fid: {
                    "content": field.get_content(),
                    "label": field.label,
                    "placeholder": field.placeholder,
                    "status": field.status.value,
                    "spans": [
                        {
                            "text": span.text,
                            "author": span.author,
                            "span_id": span.span_id
                        } for span in field.spans
                    ]
                } for fid, field in self.card.fields.items()
            },
            "typed_share": self.card.typed_share,
            "typed_share_length": self.card.get_typed_share_length(),
            "min_typed_share": self.min_typed_share,
            "is_complete": self.card.is_complete(),
            "can_submit": self.can_submit()[0],
            "role": self.role,
            "editor_type": self.editor_type,
            "allowed_actions": [a.value for a in self.allowed_actions]
        }


if __name__ == "__main__":
    # Test card editor
    from v2.config_loader_v2 import get_config
    
    config = get_config()
    
    # Test with generative condition
    gen_condition = config.conditions["GEN-COORD"]
    editor = CardEditor(gen_condition)
    
    print(f"Editor role: {editor.role}")
    print(f"Editor type: {editor.editor_type}")
    print(f"Allowed actions: {[a.value for a in editor.allowed_actions]}")
    print(f"Min typed share: {editor.min_typed_share}")
    
    # Type some text
    result = editor.type_text("field_1", "This is a test of the card editor. ")
    print(f"\nType result: {result}")
    
    # Add more text
    result = editor.type_text("field_1", "Additional content here.")
    print(f"Type result 2: {result}")
    
    # Check card state
    state = editor.get_card_state()
    print(f"\nCard state: {json.dumps(state, indent=2)}")
    
    # Test with passive condition
    pas_condition = config.conditions["PAS-COORD"]
    pas_editor = CardEditor(pas_condition)
    print(f"\nPassive editor role: {pas_editor.role}")
    print(f"Passive allowed actions: {[a.value for a in pas_editor.allowed_actions]}")
    
    # Try to type in passive mode (should fail)
    result = pas_editor.type_text("field_1", "Test")
    print(f"Passive type result: {result}")
