"""
Core simulation loop wired to AppConfig and ConversationLogger.

Used by main.py for the live task screen: it builds the agents (stable v0.5
prompts from config.yaml), runs the orchestrator after every user message and
streams the agents' replies back to the interface.

Every message (system, user, and agent) is persisted to disk the moment it
is produced, so that a full transcript survives even if the process exits
unexpectedly.
"""

from __future__ import annotations

import queue as _queue
import random
import re
import threading
import time
from dataclasses import dataclass, replace

from openai import OpenAI

from agents import Agent, ConversationState, ImageAgent, Role, parse_role
from config_loader import AgentEntry, AppConfig
from conversation_logger import ConversationLogger
from orchestrator import Orchestrator

_global_client = None
_global_image_client = None


def prewarm_clients(config: AppConfig) -> None:
    """Pre-initialize OpenAI clients in a background thread so the first
    Simulation creation is instant (no blocking HTTP/TLS handshake)."""
    global _global_client, _global_image_client
    if _global_client is None:
        _global_client = OpenAI(
            base_url=config.api.base_url,
            api_key=config.api.api_key,
            organization=config.api.organization,
            timeout=config.api.timeout,
        )
    if _global_image_client is None:
        _global_image_client = OpenAI(
            base_url=config.image_api.base_url,
            api_key=config.image_api.api_key,
            timeout=config.api.timeout,
        )





@dataclass
class AgentReply:
    agent_name: str
    content: str


@dataclass
class StreamEvent:
    """A single streaming event sent to the UI."""
    event_type: str  # "start", "chunk", "replace", "skip"
    agent_name: str
    text: str = ""
    agent_display_name: str = ""


class Simulation:
    """Manages a full multi-agent simulation session."""

    def __init__(
        self,
        config: AppConfig,
        user_name: str = "User",
        logger: ConversationLogger | None = None,
    ) -> None:
        self._config = config
        self._user_name = user_name.strip() or "User"

        global _global_client
        if _global_client is None:
            _global_client = OpenAI(
                base_url=config.api.base_url,
                api_key=config.api.api_key,
                organization=config.api.organization,
                timeout=config.api.timeout,
            )
        self._client = _global_client

        # Resolve agent name conflicts with the user's name and augment
        # each agent's system prompt with user-name awareness and
        # self-reference prevention.
        entries = self._prepare_agent_entries(
            config.agents, self._user_name, config.scenario.agent_extra_system_prompt,
            config.ui.task_description, config.ui.backup_names
        )

        # Conversation state.
        self.state = ConversationState()
        self.state.add(
            sender="System",
            content=f"[Task]: {config.ui.task_description}",
            role="system",
        )

        # Build the secondary OpenAI-compatible client for images.
        global _global_image_client
        if _global_image_client is None:
            _global_image_client = OpenAI(
                base_url=config.image_api.base_url,
                api_key=config.image_api.api_key,
                timeout=config.api.timeout,
            )
        self._image_client = _global_image_client

        # Agents.
        agents = []
        for entry in entries:
            if parse_role(entry.role) == Role.IMAGE_GENERATOR:
                agents.append(ImageAgent(entry, self._image_client, config.image_api.model, config.image_api.dummy_image_url, context_window=config.orchestrator.agent_context_window))
            else:
                agents.append(Agent(entry, self._client, context_window=config.orchestrator.agent_context_window))

        # Orchestrator.
        self.orchestrator = Orchestrator(
            agents=agents,
            client=self._client,
            config=config.orchestrator,
            user_name=self._user_name,
        )

        # Logger (reuse session logger if provided, otherwise create immediately).
        agent_names = [entry.name for entry in entries]
        if logger is not None:
            self._logger = logger
        else:
            self._logger = ConversationLogger(
                output_dir=config.logging.output_dir,
                scenario=config.ui.task_description,
                save_json=config.logging.save_json,
                agent_names=agent_names,
            )
        # Log the initial scenario message.
        self._logger.record(
            sender="System",
            content=f"[Task]: {config.ui.task_description}",
            turn_index=0,
        )

        # Typing state: managed via a threading.Event so _wait_for_turn_gate
        # can block efficiently instead of busy-polling.
        # _not_typing is set (signalled) when the user is NOT typing.
        self._not_typing = threading.Event()
        self._not_typing.set()          # Initially not typing → gate is open.
        self._typing_cleared_at: float = 0.0

        # Pause state: cleared (blocked) when the conversation is paused.
        self._not_paused = threading.Event()
        self._not_paused.set()          # Initially not paused → gate is open.

        # Generation counter: incremented each time a new user message is sent
        # or cancel() is called, so running generators can detect they are stale.
        self._generation: int = 0

    # ------------------------------------------------------------------
    # Agent entry preparation
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_agent_entries(
        entries: list[AgentEntry], user_name: str, extra_prompt_template: str = "",
        task_description: str = "", backup_names: list[str] | None = None,
    ) -> list[AgentEntry]:
        """Resolve name conflicts and augment system prompts."""
        all_names_lower = {e.name.lower() for e in entries}
        resolved: list[AgentEntry] = []
        backup_names = backup_names or []

        for entry in entries:
            if entry.name.lower() == user_name.lower():
                # Pick a backup name that doesn't collide.
                new_name: str | None = None
                for backup in backup_names:
                    if (
                        backup.lower() not in all_names_lower
                        and backup.lower() != user_name.lower()
                    ):
                        new_name = backup
                        break
                if new_name is None:
                    new_name = entry.name + "_Agent"
                new_prompt = entry.system_prompt.replace(entry.name, new_name)
                all_names_lower.discard(entry.name.lower())
                all_names_lower.add(new_name.lower())
                entry = replace(entry, name=new_name, system_prompt=new_prompt)

            # Add user-name awareness, natural conversation guidance, and task.
            extra = ""
            if task_description:
                extra += f"\n\n[Your Task]:\n{task_description.strip()}"
            if extra_prompt_template:
                extra += "\n\n" + extra_prompt_template.format(user_name=user_name)
                
            if extra:
                entry = replace(
                    entry, system_prompt=entry.system_prompt + extra,
                )
            resolved.append(entry)

        return resolved

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_user_typing(self, is_typing: bool) -> None:
        """Called by the UI when the user starts or stops typing.

        While ``is_typing`` is True, agents will not start new turns.
        When the user clears the textbox (``is_typing`` becomes False without
        sending), a configurable pause is enforced before agents resume.
        When the user *submits* a message, :meth:`cancel` is called to skip
        the pause and unblock any waiting generator immediately.
        """
        if is_typing:
            self._not_typing.clear()    # Block the gate: user is typing.
            self._typing_cleared_at = 0.0
        else:
            # Only enforce the post-typing pause when the user was genuinely
            # typing (the not-typing gate was blocked). If the gate is already
            # open the clearance is spurious — e.g. a programmatic value=""
            # update from the UI immediately after the user submitted a message.
            if not self._not_typing.is_set():
                self._typing_cleared_at = time.time()
            self._not_typing.set()      # Open the gate: user stopped typing.

    def reset_typing_state(self) -> None:
        """Reset typing flags so agents respond immediately after a send."""
        self._typing_cleared_at = 0.0
        self._not_typing.set()

    def pause(self) -> None:
        """Pause agent turns after the current agent finishes its response."""
        self._not_paused.clear()

    def resume(self) -> None:
        """Resume agent turns after a pause."""
        self._not_paused.set()

    def cancel(self) -> None:
        """Cancel the currently running agent-turn generator.

        Increments the generation counter so any blocked
        :meth:`_wait_for_turn_gate` call will detect it is stale and exit.
        Also unblocks typing/pause gates so the old generator can exit
        quickly rather than waiting for the gate to open naturally.
        """
        self._generation += 1
        self._not_paused.set()
        self.reset_typing_state()

    def _wait_for_turn_gate(self, generation: int) -> bool:
        """Block until neither typing nor paused, then return.

        Returns ``True`` if this generator's ``generation`` was superseded
        (i.e. a new user message arrived or :meth:`cancel` was called), in
        which case the caller should stop producing events.

        Polls with a short timeout so cancellation is detected quickly even
        when the generator is blocked inside this method.
        """
        pause_secs = self._config.ui.user_typing_pause_seconds

        # Wait until both typing and pause gates are open.
        while True:
            if self._generation != generation:
                return True
            # Wait for not-typing, with timeout to re-check cancellation.
            if not self._not_typing.wait(timeout=0.05):
                continue
            if self._generation != generation:
                return True
            # Typing clear; now check pause.
            if not self._not_paused.wait(timeout=0.05):
                continue
            if self._generation != generation:
                return True
            break

        # Post-typing pause: wait the configured delay before the next agent
        # turn, checking for cancellation every 50 ms.
        if self._typing_cleared_at > 0:
            elapsed = time.time() - self._typing_cleared_at
            remaining = pause_secs - elapsed
            while remaining > 0:
                if self._generation != generation:
                    return True
                time.sleep(min(0.05, remaining))
                elapsed = time.time() - self._typing_cleared_at
                remaining = pause_secs - elapsed

        return self._generation != generation

    def step_user(self, user_message: str) -> list[AgentReply]:
        self.state.add(
            sender=self._user_name, content=user_message, role="user",
        )
        self._logger.record(
            sender=self._user_name,
            content=user_message,
            turn_index=self.state.turn_count - 1,
        )
        return self._run_agent_turns()

    def step_user_stream(self, user_message: str):
        """Generator that yields StreamEvent objects for UI streaming."""
        self.state.add(
            sender=self._user_name, content=user_message, role="user",
        )
        self._logger.record(
            sender=self._user_name,
            content=user_message,
            turn_index=self.state.turn_count - 1,
        )
        # Start a new generation for this user message so any stale generator
        # from a previous turn detects it has been superseded.
        self._generation += 1
        current_gen = self._generation
        # Clear typing flags and resume from any pause so agents start immediately.
        self.reset_typing_state()
        self.resume()
        yield from self._run_agent_turns_stream(current_gen)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_agent_turns(self) -> list[AgentReply]:
        replies: list[AgentReply] = []
        total_turns = 0
        # Include user name so [Tester]: is caught by Layer 1 of _clean_content.
        all_names = list(self.orchestrator.agents.keys()) + [self._user_name]
        # Allow multiple orchestrator rounds per user turn so agents can
        # respond to each other without flooding the conversation.
        max_total = self._config.orchestrator.max_agent_turns * self._config.orchestrator.max_orchestrator_rounds
        last_speaker: str | None = None

        while total_turns < max_total:
            agent_names = self.orchestrator.decide_responding_agents(
                self.state,
                model=self._config.api.orchestrator_model,
                last_speaker=last_speaker,
            )

            if not agent_names:
                break

            round_had_response = False
            for agent_name in agent_names:
                if total_turns >= max_total:
                    break

                agent = self.orchestrator.get_agent(agent_name)
                if agent is None:
                    continue

                content = agent.generate_reply(
                    self.state,
                    model=self._config.api.agent_model,
                )
                content = self._clean_content(
                    content, agent.name, all_names,
                )

                if not content.strip():
                    continue

                self.state.add(
                    sender=agent.name,
                    content=content,
                    role="assistant",
                )
                self._logger.record(
                    sender=agent.display_name,
                    content=content,
                    turn_index=self.state.turn_count - 1,
                )
                replies.append(
                    AgentReply(agent_name=agent.name, content=content),
                )
                last_speaker = agent.name
                total_turns += 1
                round_had_response = True

            if not round_had_response:
                break

        return replies

    def _run_agent_turns_stream(self, generation: int):
        """Generator: yields StreamEvent objects for each agent's reply.

        ``generation`` is the value of ``self._generation`` captured when this
        generator was started.  If ``_generation`` is incremented by a
        concurrent call to :meth:`cancel` or :meth:`step_user_stream`, the
        gate-check will return ``True`` and this generator will exit, allowing
        the Gradio queue to start the new request.

        Each agent "thinks" for a randomised delay, then the response is
        streamed chunk by chunk so the text appears progressively in the UI.
        The LLM streaming API is started in a background thread that feeds
        chunks into a queue so the API call overlaps with the thinking delay,
        minimising perceived latency while still showing the typing effect.
        """
        total_turns = 0
        # Include user name so [Tester]: is caught by Layer 1 of _clean_content.
        all_names = list(self.orchestrator.agents.keys()) + [self._user_name]
        # Allow multiple orchestrator rounds per user turn so agents can
        # respond to each other without flooding the conversation.
        max_total = self._config.orchestrator.max_agent_turns * self._config.orchestrator.max_orchestrator_rounds
        last_speaker: str | None = None
        skipped_agents: set[str] = set()

        while total_turns < max_total:
            # Before asking the orchestrator for the next round, pause if the
            # user is typing or the conversation is paused.  Also exits early
            # if the generation was superseded (user sent a new message).
            if self._wait_for_turn_gate(generation):
                return

            agent_names = self.orchestrator.decide_responding_agents(
                self.state,
                model=self._config.api.orchestrator_model,
                last_speaker=last_speaker,
                exclude_agents=skipped_agents,
            )

            if not agent_names:
                break

            for agent_name in agent_names:
                if total_turns >= max_total:
                    break

                # Check gate between individual agent turns as well.
                if self._wait_for_turn_gate(generation):
                    return

                agent = self.orchestrator.get_agent(agent_name)
                if agent is None:
                    continue

                # --- Start LLM stream in background thread so the API call
                #     overlaps with the true-delay window (reduces latency). ---
                chunk_q: _queue.Queue[str | None] = _queue.Queue()
                stream_error: list[Exception] = []

                def _stream_worker(
                    _agent=agent, _state=self.state,
                    _model=self._config.api.agent_model,
                ) -> None:
                    try:
                        for _chunk in _agent.generate_reply_stream(_state, model=_model):
                            chunk_q.put(_chunk)
                    except Exception as exc:  # noqa: BLE001
                        stream_error.append(exc)
                    finally:
                        chunk_q.put(None)  # sentinel

                stream_thread = threading.Thread(target=_stream_worker, daemon=True)
                stream_thread.start()

                # --- True delay: agent "thinks" before responding ---
                target_delay = random.uniform(
                    self._config.ui.true_delay_min,
                    self._config.ui.true_delay_max,
                )
                delay_end = time.time() + target_delay
                while time.time() < delay_end:
                    if self._generation != generation:
                        return
                    time.sleep(min(0.05, delay_end - time.time()))

                if self._generation != generation:
                    return

                # Re-check gate after the delay in case the user paused or
                # started typing during the wait.
                if self._wait_for_turn_gate(generation):
                    return

                # --- Open a bubble and stream chunks into it ---
                yield StreamEvent(
                    event_type="start",
                    agent_name=agent.name,
                    agent_display_name=agent.display_name,
                )

                if self._generation != generation:
                    return

                full_content = ""
                bleed_detected = False
                timed_out = False

                while True:
                    try:
                        chunk = chunk_q.get(timeout=0.5)
                    except _queue.Empty:
                        if self._generation != generation:
                            return
                        if not stream_thread.is_alive() and chunk_q.empty():
                            timed_out = True
                            break
                        continue

                    if self._generation != generation:
                        return

                    if chunk is None:
                        # Sentinel: stream finished (or error).
                        break

                    full_content += chunk

                    # Check for multi-speaker bleed mid-stream; truncate early
                    # so the UI never shows fabricated dialogue.
                    candidate = self._clean_content(full_content, agent.name, all_names)
                    if len(candidate) < len(full_content.strip()):
                        bleed_detected = True
                        full_content = candidate
                        break

                    yield StreamEvent(
                        event_type="chunk",
                        agent_name=agent.name,
                        text=chunk,
                        agent_display_name=agent.display_name,
                    )
                    # Small delay between chunks so the text appears to be
                    # typed live rather than appearing all at once when the
                    # LLM has already buffered its entire response.
                    time.sleep(self._config.ui.live_typing_delay)

                if self._generation != generation:
                    return

                if stream_error:
                    print(f"[simulation] Stream error for {agent.name}: {stream_error[0]}")
                    yield StreamEvent(
                        event_type="skip",
                        agent_name=agent.name,
                        agent_display_name=agent.display_name,
                    )
                    skipped_agents.add(agent.name)
                    continue

                if timed_out:
                    print(f"[simulation] Stream timed out for {agent.name}")
                    yield StreamEvent(
                        event_type="skip",
                        agent_name=agent.name,
                        agent_display_name=agent.display_name,
                    )
                    skipped_agents.add(agent.name)
                    continue

                cleaned = full_content if bleed_detected else self._clean_content(
                    full_content, agent.name, all_names
                )

                if not cleaned.strip():
                    yield StreamEvent(
                        event_type="skip",
                        agent_name=agent.name,
                        agent_display_name=agent.display_name,
                    )
                    skipped_agents.add(agent.name)
                    continue

                # If cleaning changed the text (e.g. removed a self-prefix that
                # was already streamed), replace the entire bubble.
                if cleaned != full_content.strip() or bleed_detected:
                    yield StreamEvent(
                        event_type="replace",
                        agent_name=agent.name,
                        text=cleaned,
                        agent_display_name=agent.display_name,
                    )

                self.state.add(
                    sender=agent.name, content=cleaned, role="assistant",
                )
                self._logger.record(
                    sender=agent.display_name,
                    content=cleaned,
                    turn_index=self.state.turn_count - 1,
                )
                last_speaker = agent.name
                total_turns += 1
                skipped_agents.clear()

            # If no agent responded in this round, we loop again to ask the orchestrator
            # for different agents (since the skipped ones are now in skipped_agents).
            # The loop will naturally break if decide_responding_agents returns an empty list.

    @staticmethod
    def _clean_content(
        content: str,
        agent_name: str,
        all_agent_names: list[str] | None = None,
    ) -> str:
        """Remove self-referencing name prefix and truncate multi-speaker bleed.

        The LLM sometimes generates dialogue for other speakers, including
        invented names not in the agent list (e.g. ``[Eve]: ...``, or even
        the user's name ``[Tester]: ...``). We use two layers of defence:

        Layer 1 – known-name match: truncate at any known agent/user name.
        Layer 2 – generic bracketed-tag match: truncate at the first
            ``[AnyWord]:`` pattern at the start of a line, which catches
            completely fabricated names that aren't in any list.
        """
        content = content.strip()

        # 1. Remove self-referencing prefix like '[Bob]:' or 'Bob:'.
        self_pattern = rf"^\[?{re.escape(agent_name)}\]?\s*:\s*"
        content = re.sub(self_pattern, "", content, count=1)

        # 2. Build a set of "foreign" names to watch for:
        #    known agent names (excluding self) + user name if available.
        foreign_names: list[str] = []
        if all_agent_names:
            foreign_names = [
                n for n in all_agent_names if n.lower() != agent_name.lower()
            ]

        # 3. Layer 1 – known-name truncation (handles "Name:" without brackets too).
        if foreign_names:
            escaped = "|".join(re.escape(n) for n in foreign_names)
            known_pattern = rf"(?m)^\[?(?:{escaped})\]?\s*:"
            m = re.search(known_pattern, content)
            if m:
                content = content[: m.start()]

        # 4. Layer 2 – generic bracketed-tag truncation.
        #    Catches any "[SomeName]:" at the start of a line that we didn't
        #    catch above (invented names, user name, etc.).
        #    We explicitly exclude the current agent's own bracket tag.
        generic_pattern = rf"(?m)^\[(?!{re.escape(agent_name)}\])[^\]\n]+\]\s*:"
        m2 = re.search(generic_pattern, content)
        if m2:
            content = content[: m2.start()]

        return content.strip()
