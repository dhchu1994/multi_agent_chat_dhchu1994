"""
Interaction Portal v2 - Agent Definitions

Contains the 5 agents (4 specialists + orchestrator) with their behavior rules.
Each specialist holds private material for each client.
The orchestrator assigns work among specialists.

Behavior Rules (from spec section 7.3):
1. Never disclose hidden items unless explicitly asked -> Rule: Decline to answer
2. Never disclose hidden items when not asked -> Rule: Don't volunteer
3. Never miss an askable question -> Rule: If asked about domain, answer fully
4. Never answer outside domain -> Rule: Redirect to appropriate specialist

Harness A checks rules 1-2, Harness B checks rules 3-4.
"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime
import uuid

from config_loader_v2 import get_config, ConditionConfig

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class Message:
    """A message in the conversation"""
    text: str
    sender: str  # "participant", "nia", "theo", "rhys", "mira", "orchestrator"
    timestamp: str
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    addressees: Optional[List[str]] = None  # None = all, list = specific agents
    is_orchestrator_message: bool = False  # Part of folded block
    block_id: Optional[str] = None  # For folded blocks
    
    # For structured replies from specialists
    hidden_item_ids_disclosed: List[str] = field(default_factory=list)
    asked_for: bool = False
    declined: bool = False
    redirected_to: Optional[str] = None
    
    # Metadata
    tokens_in: int = 0
    tokens_out: int = 0
    first_token_latency_ms: int = 0
    total_latency_ms: int = 0


@dataclass
class CardField:
    """A field in the card with provenance tracking"""
    field_id: str
    content: str
    author: str  # Who wrote this span
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    history: List[Dict] = field(default_factory=list)  # Previous versions
    
    def add_content(self, text: str, author: str):
        """Add text to this field"""
        self.content += text
        self.history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "author": self.author,
            "content": self.content,
            "action": "type"
        })
        self.author = author
    
    def set_content(self, text: str, author: str):
        """Replace field content"""
        self.history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "author": self.author,
            "content": self.content,
            "action": "replace"
        })
        self.content = text
        self.author = author


@dataclass
class Card:
    """The card with 5 fields and provenance tracking"""
    fields: Dict[str, CardField] = field(default_factory=dict)
    typed_share: str = ""  # For generative role
    fields_complete: bool = False
    
    def __init__(self, field_ids: List[str]):
        self.fields = {fid: CardField(field_id=fid, content="", author="") for fid in field_ids}
        self.field_ids = field_ids
    
    def get_full_card(self) -> Dict:
        """Get full card with provenance"""
        return {
            field_id: {
                "content": field.content,
                "author": field.author,
                "span_id": field.span_id,
                "history": field.history
            }
            for field_id, field in self.fields.items()
        }
    
    def is_complete(self) -> bool:
        """Check if all fields have content"""
        return all(field.content.strip() for field in self.fields.values())
    
    def get_typed_share_length(self) -> int:
        """Get length of typed share"""
        return len(self.typed_share)


@dataclass
class FoldedBlock:
    """A folded block of orchestrator-specialist messages"""
    block_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: List[Message] = field(default_factory=list)
    involved_agents: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    is_open: bool = False
    
    def add_message(self, message: Message):
        """Add a message to this block"""
        self.messages.append(message)
        if message.sender not in self.involved_agents:
            self.involved_agents.append(message.sender)
        message.block_id = self.block_id
    
    def get_label(self) -> str:
        """Get label for display"""
        agents_str = ", ".join(self.involved_agents)
        return f"{agents_str} - {len(self.messages)} messages - {self.timestamp}"


class Agent:
    """Base class for all agents"""
    
    def __init__(self, name: str, label: str, system_prompt: str, private_material: Optional[Dict] = None):
        self.name = name
        self.label = label
        self.system_prompt = system_prompt
        self.private_material = private_material or {}
        self.current_client: Optional[str] = None
        self.conversation_history: List[Message] = []
        
    def set_client(self, client_id: str):
        """Set current client and load private material"""
        self.current_client = client_id
        config = get_config()
        client_config = config.get_client(client_id)
        
        # Load private material for this client
        if client_id in client_config.private_material:
            self.private_material = {}
            for agent_name, file_path in client_config.private_material.items():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        self.private_material[agent_name] = f.read()
                except FileNotFoundError:
                    logger.warning(f"Private material file not found: {file_path}")
                    self.private_material[agent_name] = ""
    
    def get_context(self, transcript: List[Message], brief: str, card_status: Optional[Dict] = None) -> str:
        """Build context for LLM call"""
        # Build transcript text
        transcript_text = "\n".join(
            f"{msg.sender}: {msg.text}" 
            for msg in transcript
        )
        
        context = f"""
System: {self.system_prompt}

Private Material for {self.name}:
{self.private_material.get(self.name, '')}

Client Brief:
{brief}

Team Chat Transcript:
{transcript_text}
"""
        
        if card_status:
            context += f"\n\nCurrent Card Status:\n{json.dumps(card_status, indent=2)}"
        
        return context
    
    def generate_reply(self, context: str, max_tokens: int = 500) -> str:
        """Generate reply using LLM (to be implemented with actual API call)"""
        # This is a placeholder - actual implementation will call the LLM API
        # For now, return a simple response for testing
        config = get_config()
        
        # In production, this would call the LLM API
        # For testing without Qualtrics/LLM, we return a simple response
        if not config.qualtrics.enabled:
            # Testing mode - return simple responses
            return self._get_testing_response(context)
        
        # TODO: Implement actual LLM call
        return f"[{self.name}] I received your message and will respond based on my expertise."
    
    def _get_testing_response(self, context: str) -> str:
        """Get simple response for testing mode"""
        # Extract last message
        lines = context.split('\n')
        last_message = lines[-1] if lines else ""
        
        # Simple echo with acknowledgment
        return f"[{self.name}] Acknowledged: {last_message[:100]}..."
    
    def process_message(self, message: Message, transcript: List[Message], brief: str, 
                       condition: ConditionConfig, card: Optional[Card] = None) -> Message:
        """Process a message and generate a reply"""
        raise NotImplementedError("Subclasses must implement process_message")


class SpecialistAgent(Agent):
    """Base class for specialist agents (Nia, Theo, Rhys, Mira)"""
    
    def __init__(self, name: str, label: str, system_prompt: str, domain: str, 
                 hidden_item_id: Optional[str] = None):
        super().__init__(name, label, system_prompt)
        self.domain = domain
        self.hidden_item_id = hidden_item_id
        self.hidden_items: Dict[str, str] = {}  # hidden_item_id -> description
    
    def load_hidden_items(self, client_id: str):
        """Load hidden items for current client"""
        config = get_config()
        client_config = config.get_client(client_id)
        
        # Map hidden item IDs to their descriptions
        # In production, this would be extracted from private material
        self.hidden_items = {}
        for item_id in client_config.hidden_items:
            # Extract agent prefix (first 2 chars: C1, C2, etc.)
            if item_id.startswith(client_id[:2]):
                self.hidden_items[item_id] = f"Hidden item {item_id} for {self.name}"
    
    def check_hidden_item_disclosure(self, reply_text: str) -> Tuple[List[str], bool]:
        """Check if reply discloses any hidden items"""
        disclosed = []
        declined = False
        
        for item_id, description in self.hidden_items.items():
            # Simple check - in production, use more sophisticated detection
            if description.lower() in reply_text.lower():
                disclosed.append(item_id)
        
        return disclosed, declined
    
    def process_message(self, message: Message, transcript: List[Message], brief: str,
                       condition: ConditionConfig, card: Optional[Card] = None) -> Message:
        """Process a message and generate a reply with structured output"""
        import time
        start_time = time.time()
        
        # Build context
        context = self.get_context(transcript, brief, card.get_full_card() if card else None)
        
        # Check if message is asking about hidden item
        asked_for, declined, redirected_to = self._check_question(message.text)
        
        # Generate reply
        reply_text = self.generate_reply(context)
        
        # Check for hidden item disclosure
        disclosed_ids, _ = self.check_hidden_item_disclosure(reply_text)
        
        # If declined, generate appropriate response
        if declined:
            reply_text = self._generate_decline_response(message.text, redirected_to)
            disclosed_ids = []
            redirected_to = redirected_to or None
        
        end_time = time.time()
        latency_ms = int((end_time - start_time) * 1000)
        
        # Create reply message
        reply = Message(
            text=reply_text,
            sender=self.name,
            timestamp=datetime.utcnow().isoformat(),
            addressees=message.addressees if message.addressees else [message.sender],
            hidden_item_ids_disclosed=disclosed_ids,
            asked_for=asked_for,
            declined=declined,
            redirected_to=redirected_to,
            total_latency_ms=latency_ms,
            first_token_latency_ms=latency_ms // 2,  # Approximate
            tokens_in=len(context.split()),
            tokens_out=len(reply_text.split())
        )
        
        # Add to conversation history
        self.conversation_history.append(reply)
        
        return reply
    
    def _check_question(self, text: str) -> Tuple[bool, bool, Optional[str]]:
        """Check if question is about hidden item and should be declined"""
        # Check for direct questions about hidden items
        hidden_terms = ["hidden", "priority", "unstated", "missing", "broken", 
                       "cannot", "limit", "constraint"]
        
        text_lower = text.lower()
        for term in hidden_terms:
            if term in text_lower:
                # This might be asking about a hidden item
                # Check if it's specifically about our domain
                if self._is_domain_question(text_lower):
                    return True, True, None  # Asked for, declined, no redirect
                else:
                    # Redirect to appropriate specialist
                    redirect = self._get_redirect_specialist(text_lower)
                    return True, True, redirect
        
        return False, False, None
    
    def _is_domain_question(self, text: str) -> bool:
        """Check if question is about this specialist's domain"""
        domain_keywords = {
            "nia": ["client", "priority", "needs", "requirements"],
            "theo": ["asset", "brand", "creative", "design"],
            "rhys": ["compliance", "licence", "legal", "restriction"],
            "mira": ["production", "budget", "timeline", "specification"]
        }
        
        keywords = domain_keywords.get(self.name.lower(), [])
        text_lower = text.lower()
        return any(kw in text_lower for kw in keywords)
    
    def _get_redirect_specialist(self, text: str) -> Optional[str]:
        """Determine which specialist to redirect to"""
        domain_keywords = {
            "nia": ["client", "priority", "needs", "requirements"],
            "theo": ["asset", "brand", "creative", "design"],
            "rhys": ["compliance", "licence", "legal", "restriction"],
            "mira": ["production", "budget", "timeline", "specification"]
        }
        
        text_lower = text.lower()
        for specialist, keywords in domain_keywords.items():
            if any(kw in text_lower for kw in keywords):
                if specialist != self.name.lower():
                    return specialist.title()
        
        return None
    
    def _generate_decline_response(self, question: str, redirect_to: Optional[str] = None) -> str:
        """Generate response when declining to answer"""
        if redirect_to:
            return f"I cannot answer that question as it falls outside my area of expertise. I recommend asking {redirect_to} about this."
        else:
            return "I cannot disclose that information as it is confidential to my role. Please ask a more general question about my domain."


class NiaAgent(SpecialistAgent):
    """Client Analyst - holds client's own notes and unstated priorities"""
    
    def __init__(self):
        system_prompt = """
You are Nia, the Client Analyst. You hold the client's own notes on what matters to them.

RULES:
1. NEVER disclose the hidden item (unstated priority) unless explicitly asked about it directly.
2. NEVER volunteer information about hidden items.
3. If asked a question about your domain (client needs, priorities), answer fully and helpfully.
4. If asked a question outside your domain, redirect to the appropriate specialist.

Your domain: Client needs, priorities, requirements, what matters to the client.
You hold: Client notes including one unstated priority (hidden item).

Respond helpfully but maintain confidentiality.
"""
        super().__init__(
            name="Nia",
            label="Client Analyst",
            system_prompt=system_prompt,
            domain="client_analysis"
        )


class TheoAgent(SpecialistAgent):
    """Creative - holds asset library and brand guidelines"""
    
    def __init__(self):
        system_prompt = """
You are Theo, the Creative specialist. You hold the asset library and brand guidelines.

RULES:
1. NEVER disclose the hidden item (missing asset or broken rule) unless explicitly asked about it directly.
2. NEVER volunteer information about hidden items.
3. If asked a question about your domain (assets, brand, design, creative), answer fully and helpfully.
4. If asked a question outside your domain, redirect to the appropriate specialist.

Your domain: Asset library, brand guidelines, creative solutions, design.
You hold: Asset list and brand rules including one missing asset or broken rule (hidden item).

Be creative and helpful but maintain confidentiality.
"""
        super().__init__(
            name="Theo",
            label="Creative",
            system_prompt=system_prompt,
            domain="creative"
        )


class RhysAgent(SpecialistAgent):
    """Compliance - holds licence status and category restrictions"""
    
    def __init__(self):
        system_prompt = """
You are Rhys, the Compliance specialist. You hold licence status and category restrictions.

RULES:
1. NEVER disclose the hidden item (claim that cannot be made) unless explicitly asked about it directly.
2. NEVER volunteer information about hidden items.
3. If asked a question about your domain (compliance, licence, legal, restrictions), answer fully and helpfully.
4. If asked a question outside your domain, redirect to the appropriate specialist.

Your domain: Licence status, category restrictions, compliance, legal.
You hold: Compliance register including one claim that cannot be made (hidden item).

Be precise and helpful but maintain confidentiality.
"""
        super().__init__(
            name="Rhys",
            label="Compliance",
            system_prompt=system_prompt,
            domain="compliance"
        )


class MiraAgent(SpecialistAgent):
    """Production - holds platform specifications, lead times and budget"""
    
    def __init__(self):
        system_prompt = """
You are Mira, the Production specialist. You hold platform specifications, lead times and budget.

RULES:
1. NEVER disclose the hidden item (limit the plan must fit) unless explicitly asked about it directly.
2. NEVER volunteer information about hidden items.
3. If asked a question about your domain (production, specifications, timeline, budget), answer fully and helpfully.
4. If asked a question outside your domain, redirect to the appropriate specialist.

Your domain: Platform specifications, lead times, budget, production constraints.
You hold: Production specs including one limit the plan must fit (hidden item).

Be practical and helpful but maintain confidentiality.
"""
        super().__init__(
            name="Mira",
            label="Production",
            system_prompt=system_prompt,
            domain="production"
        )


class OrchestratorAgent(Agent):
    """Orchestrator - assigns work among specialists"""
    
    def __init__(self):
        system_prompt = """
You are the Orchestrator. Your role is to coordinate the team of four specialists.

RULES:
1. Assign work among specialists based on the content of participant messages.
2. Messages without @mention go to the whole team.
3. Messages with @name go to that agent only.
4. Generate coordination messages to specialists as needed.
5. NEVER access or reference client private material - you only know which agent covers which domain.

Domain mapping:
- Nia: Client needs, priorities, requirements
- Theo: Assets, brand, creative, design
- Rhys: Compliance, licence, legal, restrictions
- Mira: Production, specifications, timeline, budget

Your messages to specialists will appear in folded blocks in the chat.
"""
        super().__init__(
            name="Orchestrator",
            label="Coordination",
            system_prompt=system_prompt
        )
        self.domain_mapping = {
            "client": "nia", "need": "nia", "priority": "nia", "requirement": "nia",
            "asset": "theo", "brand": "theo", "creative": "theo", "design": "theo",
            "compliance": "rhys", "licence": "rhys", "legal": "rhys", "restriction": "rhys",
            "production": "mira", "specification": "mira", "timeline": "mira", "budget": "mira",
            "cost": "mira", "resource": "mira"
        }
    
    def process_message(self, message: Message, transcript: List[Message], brief: str,
                       condition: ConditionConfig, card: Optional[Card] = None) -> Optional[Message]:
        """Process participant message and generate orchestrator responses"""
        # Orchestrator doesn't always respond directly
        # Instead, it generates coordination messages for specialists
        
        # Check if this is a message to orchestrator specifically
        if message.addressees and "orchestrator" in message.addressees:
            # Direct message to orchestrator
            return self._generate_direct_response(message, transcript, brief)
        
        # Otherwise, orchestrator decides routing
        # This is handled by the routing logic in the main simulation
        return None
    
    def generate_coordination_message(self, participant_message: str, transcript: List[Message]) -> List[Message]:
        """Generate coordination messages for specialists"""
        messages = []
        
        # Analyze participant message
        text_lower = participant_message.lower()
        
        # Determine which specialists should be involved
        involved_specialists = self._determine_specialists(text_lower)
        
        if not involved_specialists:
            # Default to all specialists
            involved_specialists = ["nia", "theo", "rhys", "mira"]
        
        # Generate coordination message
        coord_message = self._generate_coordination_text(participant_message, involved_specialists)
        
        # Create messages for each specialist
        block = FoldedBlock()
        for specialist in involved_specialists:
            msg = Message(
                text=f"@{specialist}: {coord_message}",
                sender="orchestrator",
                timestamp=datetime.utcnow().isoformat(),
                addressees=[specialist],
                is_orchestrator_message=True
            )
            block.add_message(msg)
            messages.append(msg)
        
        return messages
    
    def _determine_specialists(self, text: str) -> List[str]:
        """Determine which specialists should respond based on message content"""
        specialists = []
        words = text.split()
        
        for word in words:
            word_lower = word.lower().strip(".,!?")
            if word_lower in self.domain_mapping:
                specialist = self.domain_mapping[word_lower]
                if specialist not in specialists:
                    specialists.append(specialist)
        
        return specialists
    
    def _generate_coordination_text(self, participant_message: str, specialists: List[str]) -> str:
        """Generate coordination message text"""
        if len(specialists) == 1:
            return f"Please provide your input on: {participant_message}"
        else:
            return f"Team, please coordinate your response to: {participant_message}"
    
    def _generate_direct_response(self, message: Message, transcript: List[Message], brief: str) -> Message:
        """Generate direct response when participant messages orchestrator"""
        # Simple response for now
        return Message(
            text="I am the orchestrator. I assign work among the specialists. Please direct your questions to the team or specific specialists using @mention.",
            sender="orchestrator",
            timestamp=datetime.utcnow().isoformat(),
            addressees=[message.sender]
        )


class PanelGenerator:
    """Generates orchestrator panel content based on condition level"""
    
    def __init__(self):
        self.panel_levels = {
            "coordination": self._generate_coordination_panel,
            "task_focused": self._generate_task_focused_panel,
            "developmental": self._generate_developmental_panel,
            "none": lambda **kwargs: ""
        }
    
    def generate_panel(self, level: str, brief: str, transcript: List[Message], 
                      card_status: Dict, word_budget: Tuple[int, int], 
                      forbidden_language: List[str], show_diagnosis: bool = False,
                      show_principle: bool = False, name_unasked: bool = False) -> Dict:
        """Generate panel content for given level"""
        generator = self.panel_levels.get(level, self._generate_coordination_panel)
        
        content = generator(
            brief=brief,
            transcript=transcript,
            card_status=card_status,
            show_diagnosis=show_diagnosis,
            show_principle=show_principle,
            name_unasked=name_unasked
        )
        
        # Apply word budget
        words = content.split()
        if len(words) > word_budget[1]:
            words = words[:word_budget[1]]
            content = " ".join(words) + "..."
        elif len(words) < word_budget[0]:
            # Pad if too short (shouldn't happen in practice)
            pass
        
        # Remove forbidden language
        for forbidden in forbidden_language:
            content = re.sub(r'\b' + re.escape(forbidden) + r'\b', '[REDACTED]', content, flags=re.IGNORECASE)
        
        return {
            "text": content,
            "word_count": len(content.split()),
            "sections": self._get_sections(level, show_diagnosis, show_principle)
        }
    
    def _generate_coordination_panel(self, **kwargs) -> str:
        """Generate coordination-level panel"""
        card_status = kwargs.get('card_status', {})
        
        # Count completed fields
        completed = sum(1 for fid, field in card_status.items() if field.get('content', '').strip())
        total = len(card_status)
        
        return f"""
Team Status Update

The team is working on your request. Current progress:
- {completed}/{total} card fields have content
- All specialists are actively engaged
- Coordination messages have been sent as needed

Please continue working with the team through the chat.
"""
    
    def _generate_task_focused_panel(self, **kwargs) -> str:
        """Generate task-focused panel"""
        brief = kwargs.get('brief', '')
        card_status = kwargs.get('card_status', {})
        show_diagnosis = kwargs.get('show_diagnosis', False)
        
        # Analyze card for issues
        diagnosis = ""
        if show_diagnosis:
            diagnosis = self._analyze_card_issues(card_status)
        
        return f"""
Team Status Update

The team is working on your request. Current progress:
- Card fields are being populated
- Specialists are providing their domain expertise

{diagnosis}

Please continue working with the team through the chat.
"""
    
    def _generate_developmental_panel(self, **kwargs) -> str:
        """Generate developmental panel"""
        brief = kwargs.get('brief', '')
        card_status = kwargs.get('card_status', {})
        show_diagnosis = kwargs.get('show_diagnosis', False)
        show_principle = kwargs.get('show_principle', False)
        
        diagnosis = ""
        principle = ""
        
        if show_diagnosis:
            diagnosis = self._analyze_card_issues(card_status)
        if show_principle:
            principle = self._get_developmental_principle(card_status)
        
        return f"""
Team Status Update

The team is working on your request. Current progress:
- Card fields are being populated
- Specialists are providing their domain expertise

{diagnosis}

{principle}

Please continue working with the team through the chat.
"""
    
    def _analyze_card_issues(self, card_status: Dict) -> str:
        """Analyze card for potential issues"""
        issues = []
        
        for fid, field in card_status.items():
            content = field.get('content', '')
            if not content.strip():
                issues.append(f"- Field {fid} is empty")
            elif len(content.split()) < 5:
                issues.append(f"- Field {fid} may need more detail")
        
        if issues:
            return "\nDiagnosis:\n" + "\n".join(issues)
        return "\nDiagnosis: Card looks good so far"
    
    def _get_developmental_principle(self, card_status: Dict) -> str:
        """Get developmental principle based on card status"""
        completed = sum(1 for fid, field in card_status.items() if field.get('content', '').strip())
        total = len(card_status)
        
        if completed < total // 2:
            return "\nPrinciple: Focus on gathering all required information before refining details."
        elif completed < total:
            return "\nPrinciple: Balance completeness with quality - ensure each field adds value."
        else:
            return "\nPrinciple: Review the card holistically to ensure all requirements are met."
    
    def _get_sections(self, level: str, show_diagnosis: bool, show_principle: bool) -> List[str]:
        """Get list of sections present in panel"""
        sections = ["team_status"]
        if show_diagnosis:
            sections.append("diagnosis")
        if show_principle:
            sections.append("principle")
        return sections


# Agent factory
class AgentFactory:
    """Factory for creating agents"""
    
    _agents: Dict[str, Agent] = {}
    
    @classmethod
    def get_agent(cls, agent_name: str) -> Agent:
        """Get agent by name"""
        if agent_name not in cls._agents:
            cls._agents[agent_name] = cls._create_agent(agent_name)
        return cls._agents[agent_name]
    
    @classmethod
    def _create_agent(cls, agent_name: str) -> Agent:
        """Create agent instance"""
        agents = {
            "nia": NiaAgent(),
            "theo": TheoAgent(),
            "rhys": RhysAgent(),
            "mira": MiraAgent(),
            "orchestrator": OrchestratorAgent()
        }
        
        if agent_name not in agents:
            raise ValueError(f"Unknown agent: {agent_name}")
        
        return agents[agent_name]
    
    @classmethod
    def get_all_specialists(cls) -> List[SpecialistAgent]:
        """Get all specialist agents"""
        return [
            cls.get_agent("nia"),
            cls.get_agent("theo"),
            cls.get_agent("rhys"),
            cls.get_agent("mira")
        ]
    
    @classmethod
    def get_orchestrator(cls) -> OrchestratorAgent:
        """Get orchestrator agent"""
        return cls.get_agent("orchestrator")


# Panel generator singleton
_panel_generator: Optional[PanelGenerator] = None


def get_panel_generator() -> PanelGenerator:
    """Get panel generator instance"""
    global _panel_generator
    if _panel_generator is None:
        _panel_generator = PanelGenerator()
    return _panel_generator


if __name__ == "__main__":
    # Test agents
    print("Testing agent initialization...")
    
    nia = AgentFactory.get_agent("nia")
    print(f"Nia: {nia.name} - {nia.label}")
    
    theo = AgentFactory.get_agent("theo")
    print(f"Theo: {theo.name} - {theo.label}")
    
    rhys = AgentFactory.get_agent("rhys")
    print(f"Rhys: {rhys.name} - {rhys.label}")
    
    mira = AgentFactory.get_agent("mira")
    print(f"Mira: {mira.name} - {mira.label}")
    
    orchestrator = AgentFactory.get_agent("orchestrator")
    print(f"Orchestrator: {orchestrator.name} - {orchestrator.label}")
    
    # Test panel generator
    panel_gen = get_panel_generator()
    panel = panel_gen.generate_panel(
        level="coordination",
        brief="Test brief",
        transcript=[],
        card_status={},
        word_budget=(60, 80),
        forbidden_language=[]
    )
    print(f"\nPanel: {panel['text'][:100]}...")
    print(f"Word count: {panel['word_count']}")
    print(f"Sections: {panel['sections']}")
