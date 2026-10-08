"""
Agent data types, conversation state, and LLM response generation.

Defines the Role enum, Message and ConversationState dataclasses, the Agent
class that wraps an OpenAI-compatible chat model, and ImageAgent (the
image-generating agent, e.g. "Picasso"). All agent parameters (temperature,
max_tokens, talkativeness, system_prompt) are read from the AgentEntry
dataclass loaded from YAML via config_loader.

This is the stable v0.5 prompt layer and the single source of agent behaviour
for the beta v1 study interface in main.py: the agent personalities, their
descriptions and the shared scenario prompt are defined in config.yaml (agents
and scenario sections), never in main.py.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from openai import OpenAI

from config_loader import AgentEntry


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

class Role(enum.Enum):
    CHALLENGER = "challenger"
    COMEDIAN = "comedian"
    CREATIVE = "creative"
    DEVIL_ADVOCATE = "devil_advocate"
    EXPERT = "expert"
    IMAGE_GENERATOR = "image_generator"
    MANAGER = "manager"
    MEDIATOR = "mediator"
    QUIET_OBSERVER = "quiet_observer"
    SKEPTIC = "skeptic"
    SUMMARIZER = "summarizer"
    SUPPORTER = "supporter"
    USER_PROXY = "user_proxy"


def parse_role(value: str) -> Role:
    """Convert a string from YAML into a Role enum, falling back to SUPPORTER."""
    try:
        return Role(value.lower())
    except ValueError:
        return Role.SUPPORTER


@dataclass
class Message:
    sender: str
    content: str
    role: str = "assistant"


@dataclass
class ConversationState:
    history: list[Message] = field(default_factory=list)
    turn_count: int = 0

    def add(self, sender: str, content: str, role: str = "assistant") -> None:
        self.history.append(Message(sender=sender, content=content, role=role))
        self.turn_count += 1

    def to_openai_messages(
        self, system_prompt: str, agent_name: str | None = None, context_window: int = 40
    ) -> list[dict[str, str]]:
        """Build the OpenAI messages list from the conversation history.

        Each agent gets a first-person view of the conversation:
        - Its own past messages → role "assistant", no speaker prefix.
          This is the idiomatic use of the chat API: the model IS the
          assistant, so it naturally speaks as itself and never needs to
          write "[Name]:" prefixes.
        - All other messages (user, other agents, system) → role "user",
          with "[Speaker]: " prefix so the agent knows who said what.

        Consecutive messages with the same role are merged into one
        to satisfy the API's strict user/assistant alternation rule.
        """
        # Simple system prompt — role structure handles the formatting,
        # so we don't need heavy "don't write [Name]:" instructions.
        full_prompt = system_prompt
        if agent_name:
            full_prompt += (
                f"\n\nYou are {agent_name}. Respond only as yourself — "
                "write your reply directly without any name prefix."
            )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": full_prompt},
        ]

        # AI Budget Optimization: Limit agent memory to the configured context window. 
        # This prevents exponential token usage and quadratic costs in long conversations.
        recent_history = self.history[-context_window:] if len(self.history) > context_window else self.history

        for msg in recent_history:
            if msg.role == "system":
                # Skip the scenario system message — it's already in the
                # system prompt via the scenario description.
                continue

            import re
            cleaned_content = re.sub(r'!\[.*?\]\(data:image/[^;]+;base64,[^\)]+\)', '[Image omitted from history]', msg.content)

            if agent_name and msg.sender == agent_name:
                # This agent's own past turn → assistant role, no prefix.
                role = "assistant"
                content = cleaned_content
            else:
                # Everyone else → user role, labelled with speaker name.
                role = "user"
                content = f"[{msg.sender}]: {cleaned_content}"

            # Merge consecutive messages with the same role (API requirement).
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n\n{content}"
            else:
                messages.append({"role": role, "content": content})

        # The API requires the last message to be from the user.
        # If the last message ended up as "assistant" (e.g. this agent
        # spoke last), append a minimal prompt to solicit a response.
        if messages and messages[-1]["role"] == "assistant":
            messages.append({
                "role": "user",
                "content": "[System]: Please continue the discussion.",
            })

        return messages


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    """A single conversational agent backed by an OpenAI-compatible chat model."""

    def __init__(self, entry: AgentEntry, client: OpenAI, context_window: int = 40) -> None:
        self.entry = entry
        self.role = parse_role(entry.role)
        self._client = client
        self.context_window = context_window

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.entry.title})" if self.entry.title else self.name

    @property
    def talkativeness(self) -> float:
        return self.entry.talkativeness

    def generate_reply(
        self,
        state: ConversationState,
        model: str = "gpt-4o",
    ) -> str:
        messages = state.to_openai_messages(
            self.entry.system_prompt, agent_name=self.name, context_window=self.context_window,
        )
        response = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.entry.temperature,
            max_tokens=self.entry.max_tokens,
            stop=["\n[", "\n*[", "*["],
        )
        return response.choices[0].message.content.strip()

    def generate_reply_stream(
        self,
        state: ConversationState,
        model: str = "gpt-4o",
    ):
        """Yield response chunks using the streaming API."""
        messages = state.to_openai_messages(
            self.entry.system_prompt, agent_name=self.name, context_window=self.context_window,
        )
        response = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.entry.temperature,
            max_tokens=self.entry.max_tokens,
            stream=True,
            stop=["\n[", "\n*[", "*["],
        )
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


_IMAGE_API_WORKING = None

class ImageAgent(Agent):
    """An agent that generates images using an external endpoint."""
    
    def __init__(self, entry: AgentEntry, client: OpenAI, image_model: str = "gpt-image-1", dummy_image_url: str = "", context_window: int = 40) -> None:
        super().__init__(entry, client, context_window=context_window)
        self.image_model = image_model
        self.dummy_image_url = dummy_image_url

    def generate_reply(self, state: ConversationState, model: str = None) -> str:
        global _IMAGE_API_WORKING
        if _IMAGE_API_WORKING is None:
            try:
                self._client.models.list()
                _IMAGE_API_WORKING = True
            except Exception:
                _IMAGE_API_WORKING = False

        # Extract the user's latest prompt
        # We search backward for the last user message
        prompt = "A beautiful scene"
        for msg in reversed(state.history):
            if msg.role == "user":
                prompt = msg.content
                break

        if not _IMAGE_API_WORKING:
            img_url = self.dummy_image_url
            return f"Here is a dummy image for testing:\n\n![Generated Image]({img_url})\n\n*(Click anywhere on this message to save this specific poster!)*"

        try:
            response = self._client.images.generate(
                model=self.image_model,
                prompt=prompt,
                size="1024x1024",
                response_format="url"
            )
            image_url = response.data[0].url
            return f"Here is your generated image:\n\n![Generated Image]({image_url})\n\n*(Click anywhere on this message to save this specific poster!)*"
        except Exception as e:
            return f"I could not generate the image due to an error: {e}"

    def generate_reply_stream(self, state: ConversationState, model: str = None):
        # Image generation isn't natively streamable chunk-by-chunk in the same way,
        # so we just yield the final markdown string.
        yield self.generate_reply(state, model)