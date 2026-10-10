"""
Interaction Portal v2 - Orchestrator Logic

Implements the orchestrator panel generation, folded blocks, and routing logic.
This module handles:
- Folded block management (collapsed orchestrator-specialist messages)
- Orchestrator panel generation with 3 levels
- Message routing (@mention handling)
- Team coordination

From spec sections 6.7, 7.2, 7.3:
- Folded blocks: collapsed groups of orchestrator-specialist messages
- Orchestrator panel: right-hand column, read-only, regenerated on team status changes
- Routing: @mention goes to specific agent, no @mention goes to whole team
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime

from config_loader_v2 import get_config, ConditionConfig
from agents_v2 import (
    AgentFactory, OrchestratorAgent, SpecialistAgent,
    Message, FoldedBlock, PanelGenerator, get_panel_generator
)
from card_editor import Card, CardEditor


class RoutingResult:
    """Result of routing a message"""
    
    def __init__(self, recipients: List[str], is_orchestrator_message: bool = False,
                 should_create_folded_block: bool = False):
        self.recipients = recipients
        self.is_orchestrator_message = is_orchestrator_message
        self.should_create_folded_block = should_create_folded_block


class MessageRouter:
    """Routes participant messages to appropriate agents"""
    
    def __init__(self):
        self.domain_keywords = {
            "nia": ["client", "priority", "need", "requirement", "customer", "user"],
            "theo": ["asset", "brand", "creative", "design", "visual", "style"],
            "rhys": ["compliance", "licence", "legal", "restriction", "regulation", "rule"],
            "mira": ["production", "specification", "timeline", "budget", "cost", "resource", "platform"]
        }
        self.agent_names = ["nia", "theo", "rhys", "mira", "orchestrator"]
    
    def route_message(self, text: str, addressees: Optional[List[str]] = None) -> RoutingResult:
        """Route a message to appropriate recipients"""
        if addressees:
            # Message has explicit addressees
            recipients = []
            for addressee in addressees:
                addr_lower = addressee.lower()
                if addr_lower in self.agent_names:
                    recipients.append(addr_lower)
                elif addr_lower == "all":
                    recipients = self.agent_names[:-1]  # All specialists (not orchestrator)
                    break
            
            # If orchestrator is in addressees, it's a direct message
            is_orchestrator = "orchestrator" in [a.lower() for a in addressees]
            
            return RoutingResult(
                recipients=recipients,
                is_orchestrator_message=is_orchestrator,
                should_create_folded_block=False
            )
        
        # No explicit addressees - parse for @mentions
        mentions = self._extract_mentions(text)
        
        if mentions:
            # Message has @mentions - send to mentioned agents only
            recipients = []
            for mention in mentions:
                m_lower = mention.lower()
                if m_lower in self.agent_names:
                    recipients.append(m_lower)
            
            return RoutingResult(
                recipients=recipients,
                is_orchestrator_message=False,
                should_create_folded_block=False
            )
        
        # No @mentions and no addressees - send to whole team
        # Orchestrator will generate coordination messages
        return RoutingResult(
            recipients=self.agent_names[:-1],  # All specialists
            is_orchestrator_message=False,
            should_create_folded_block=True
        )
    
    def _extract_mentions(self, text: str) -> List[str]:
        """Extract @mentions from text"""
        # Match @name patterns
        pattern = r'@(\w+)'
        matches = re.findall(pattern, text)
        return [m.lower() for m in matches]


class FoldedBlockManager:
    """Manages folded blocks of orchestrator-specialist messages"""
    
    def __init__(self):
        self.blocks: List[FoldedBlock] = []
        self.current_block: Optional[FoldedBlock] = None
    
    def start_block(self, orchestrator_message: Message) -> FoldedBlock:
        """Start a new folded block with orchestrator message"""
        self.current_block = FoldedBlock()
        self.current_block.add_message(orchestrator_message)
        self.blocks.append(self.current_block)
        return self.current_block
    
    def add_to_current_block(self, message: Message):
        """Add message to current folded block"""
        if self.current_block:
            self.current_block.add_message(message)
    
    def end_current_block(self):
        """End current folded block"""
        self.current_block = None
    
    def get_blocks(self) -> List[FoldedBlock]:
        """Get all folded blocks"""
        return self.blocks
    
    def get_block(self, block_id: str) -> Optional[FoldedBlock]:
        """Get block by ID"""
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        return None
    
    def toggle_block(self, block_id: str) -> bool:
        """Toggle block open/closed state"""
        block = self.get_block(block_id)
        if block:
            block.is_open = not block.is_open
            return block.is_open
        return False
    
    def get_open_blocks(self) -> List[FoldedBlock]:
        """Get all open blocks"""
        return [block for block in self.blocks if block.is_open]


class PanelManager:
    """Manages orchestrator panel generation and updates"""
    
    def __init__(self, condition: ConditionConfig):
        self.condition = condition
        self.panel_generator = get_panel_generator()
        self.last_panel_text: str = ""
        self.last_panel_timestamp: Optional[str] = None
        self.refresh_interval: int = condition.panel.refresh_min_seconds
        self.last_refresh_time: Optional[datetime] = None
    
    def should_refresh(self) -> bool:
        """Check if panel should be refreshed"""
        if not self.last_refresh_time:
            return True
        
        elapsed = (datetime.now() - self.last_refresh_time).total_seconds()
        return elapsed >= self.refresh_interval
    
    def generate_panel(self, brief: str, transcript: List[Message], 
                      card_status: Dict) -> Dict:
        """Generate panel content"""
        panel = self.panel_generator.generate_panel(
            level=self.condition.panel_level,
            brief=brief,
            transcript=transcript,
            card_status=card_status,
            word_budget=tuple(self.condition.panel.word_budget),
            forbidden_language=self._get_forbidden_language(),
            show_diagnosis=self.condition.panel.show_diagnosis,
            show_principle=self.condition.panel.show_principle,
            name_unasked=self.condition.panel.name_unasked
        )
        
        self.last_panel_text = panel["text"]
        self.last_panel_timestamp = datetime.now().isoformat()
        self.last_refresh_time = datetime.now()
        
        return panel
    
    def _get_forbidden_language(self) -> List[str]:
        """Get forbidden language for this condition's panel level"""
        config = get_config()
        panel_config = getattr(config, 'panel', {}) or {}
        level = self.condition.panel_level
        
        if level in panel_config:
            return panel_config[level].get("forbidden_language", [])
        return []
    
    def get_current_panel(self) -> Dict:
        """Get current panel content"""
        if not self.last_panel_text:
            return {"text": "", "word_count": 0, "sections": []}
        
        return {
            "text": self.last_panel_text,
            "word_count": len(self.last_panel_text.split()),
            "sections": self._get_sections()
        }
    
    def _get_sections(self) -> List[str]:
        """Get sections present in panel"""
        sections = ["team_status"]
        if self.condition.panel.show_diagnosis:
            sections.append("diagnosis")
        if self.condition.panel.show_principle:
            sections.append("principle")
        return sections


class OrchestratorV2:
    """Main orchestrator class combining routing, folded blocks, and panel management"""
    
    def __init__(self, condition: ConditionConfig):
        self.condition = condition
        self.router = MessageRouter()
        self.folded_block_manager = FoldedBlockManager()
        self.panel_manager = PanelManager(condition)
        self.orchestrator_agent = AgentFactory.get_agent("orchestrator")
        
        # Track team status
        self.team_status = {
            "nia": {"active": True, "last_message": None},
            "theo": {"active": True, "last_message": None},
            "rhys": {"active": True, "last_message": None},
            "mira": {"active": True, "last_message": None}
        }
    
    def route_message(self, text: str, addressees: Optional[List[str]] = None) -> RoutingResult:
        """Route a participant message"""
        return self.router.route_message(text, addressees)
    
    def process_message(self, message: Message, transcript: List[Message], 
                       brief: str, card: Optional[Card] = None) -> Tuple[List[Message], Optional[FoldedBlock]]:
        """Process a message and return responses and any new folded blocks"""
        responses = []
        new_block: Optional[FoldedBlock] = None
        
        # Route the message
        routing = self.route_message(message.text, message.addressees)
        
        if routing.is_orchestrator_message:
            # Direct message to orchestrator
            orch_response = self.orchestrator_agent.process_message(
                message, transcript, brief, self.condition, card
            )
            if orch_response:
                responses.append(orch_response)
        
        elif routing.should_create_folded_block:
            # Message to whole team - orchestrator generates coordination
            coord_messages = self.orchestrator_agent.generate_coordination_message(
                message.text, transcript
            )
            
            if coord_messages:
                # Start a new folded block
                new_block = self.folded_block_manager.start_block(coord_messages[0])
                responses.extend(coord_messages)
                
                # Add to block
                for coord_msg in coord_messages[1:]:
                    self.folded_block_manager.add_to_current_block(coord_msg)
                    responses.append(coord_msg)
        
        else:
            # Message to specific agents
            for recipient in routing.recipients:
                if recipient == "orchestrator":
                    orch_response = self.orchestrator_agent.process_message(
                        message, transcript, brief, self.condition, card
                    )
                    if orch_response:
                        responses.append(orch_response)
                else:
                    # Get specialist agent
                    agent = AgentFactory.get_agent(recipient)
                    reply = agent.process_message(
                        message, transcript, brief, self.condition, card
                    )
                    responses.append(reply)
        
        return responses, new_block
    
    def generate_panel(self, brief: str, transcript: List[Message], 
                      card: Optional[Card] = None) -> Dict:
        """Generate orchestrator panel"""
        card_status = card.get_all_content() if card else {}
        return self.panel_manager.generate_panel(brief, transcript, card_status)
    
    def should_refresh_panel(self) -> bool:
        """Check if panel should be refreshed"""
        return self.panel_manager.should_refresh()
    
    def get_folded_blocks(self) -> List[Dict]:
        """Get all folded blocks for display"""
        blocks = []
        for block in self.folded_block_manager.get_blocks():
            blocks.append({
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
            })
        return blocks
    
    def toggle_block(self, block_id: str) -> bool:
        """Toggle a folded block"""
        return self.folded_block_manager.toggle_block(block_id)
    
    def update_team_status(self, agent_name: str, message: Message):
        """Update team status after agent message"""
        if agent_name in self.team_status:
            self.team_status[agent_name]["last_message"] = message.timestamp
    
    def get_team_status_summary(self) -> Dict:
        """Get summary of team status for panel"""
        status = {}
        for agent_name, agent_status in self.team_status.items():
            status[agent_name] = {
                "active": agent_status["active"],
                "last_active": agent_status["last_message"]
            }
        return status


if __name__ == "__main__":
    # Test orchestrator
    from config_loader_v2 import get_config
    
    config = get_config()
    condition = config.conditions["GEN-TASK"]
    
    orchestrator = OrchestratorV2(condition)
    
    print("Testing Orchestrator V2...")
    print(f"Condition: {condition.code}")
    print(f"Panel level: {condition.panel_level}")
    
    # Test routing
    print("\n--- Testing Message Routing ---")
    
    # Message to all
    routing = orchestrator.route_message("Hello team!")
    print(f"'Hello team!' -> recipients: {routing.recipients}, folded_block: {routing.should_create_folded_block}")
    
    # Message with @mention
    routing = orchestrator.route_message("@nia What about the client needs?")
    print(f"'@nia What about...' -> recipients: {routing.recipients}")
    
    # Direct message to orchestrator
    routing = orchestrator.route_message("Orchestrator, what's the status?", ["orchestrator"])
    print(f"Direct to orchestrator -> recipients: {routing.recipients}, is_orch: {routing.is_orchestrator_message}")
    
    # Test panel generation
    print("\n--- Testing Panel Generation ---")
    panel = orchestrator.generate_panel(
        brief="Test brief for the client",
        transcript=[],
        card=None
    )
    print(f"Panel text: {panel['text'][:100]}...")
    print(f"Word count: {panel['word_count']}")
    print(f"Sections: {panel['sections']}")
    
    # Test folded blocks
    print("\n--- Testing Folded Blocks ---")
    
    # Create a message
    from agents_v2 import Message
    msg = Message(
        text="Orchestrator: Please respond to this",
        sender="orchestrator",
        timestamp="2024-01-01T12:00:00Z"
    )
    
    # Start a block
    block = orchestrator.folded_block_manager.start_block(msg)
    print(f"Created block: {block.block_id}")
    
    # Add more messages
    msg2 = Message(
        text="Nia: I'll handle this",
        sender="nia",
        timestamp="2024-01-01T12:00:01Z"
    )
    orchestrator.folded_block_manager.add_to_current_block(msg2)
    
    # Get blocks
    blocks = orchestrator.get_folded_blocks()
    print(f"Number of blocks: {len(blocks)}")
    print(f"Block label: {blocks[0]['label']}")
    
    # Toggle block
    is_open = orchestrator.toggle_block(block.block_id)
    print(f"Block is now: {'open' if is_open else 'closed'}")
