"""
Orchestrator: controls turn-taking in the multi-agent simulation.

All hyper-parameters (temperature, max_tokens, context window, max agent
turns) and the orchestrator system prompt are read from the OrchestratorConfig
dataclass loaded from YAML (orchestrator section of config.yaml). Explicit
@mentions bypass the LLM and select the tagged agents directly; otherwise the
LLM assigns response probabilities that are combined with each agent's
talkativeness.

This is the stable v0.5 turn-taking logic used unchanged by the beta v1 study
interface in main.py.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

from openai import OpenAI

from agents import Agent, ConversationState
from config_loader import OrchestratorConfig


@dataclass
class OrchestratorDecision:
    responding_agents: list[str]
    reasoning: str
    response_probabilities: dict[str, float] | None = None


class Orchestrator:
    """Decides turn order among agents and the user."""

    def __init__(
        self,
        agents: list[Agent],
        client: OpenAI,
        config: OrchestratorConfig,
        user_name: str = "User",
    ) -> None:
        self.agents = {a.name: a for a in agents}
        self._client = client
        self._config = config
        self._user_name = user_name
        self._agent_speak_counts: dict[str, int] = {a.name: 0 for a in agents}

    def decide_responding_agents(
        self,
        state: ConversationState,
        model: str = "gpt-4o-mini",
        last_speaker: str | None = None,
        exclude_agents: set[str] | None = None,
    ) -> list[str]:
        """Return a list of agent names who should respond next."""
        # Check if the most recent user message contains explicit @mentions for valid agents
        user_msg = None
        for m in reversed(state.history):
            if m.sender == self._user_name:
                user_msg = m
                break
                
        if user_msg:
            # Create a case-insensitive map of agent names
            name_map = {name.lower(): name for name in self.agents}
            mentions = re.findall(r'@(\w+)', user_msg.content)
            
            mentioned_agents = []
            for m in mentions:
                matched_name = name_map.get(m.lower())
                if matched_name and matched_name not in mentioned_agents:
                    mentioned_agents.append(matched_name)
            
            if mentioned_agents:
                # If this is a follow-up orchestrator round (last_speaker is not None),
                # we return an empty list to stop anyone else from chiming in.
                if last_speaker is not None:
                    return []
                
                # First round: return the mentioned agents
                for name in mentioned_agents:
                    self._agent_speak_counts[name] = self._agent_speak_counts.get(name, 0) + 1
                return mentioned_agents

        agent_descriptions = "\n".join(
            f"- {a.name} (role={a.role.value}, talkativeness={a.talkativeness}, "
            f"times_spoken={self._agent_speak_counts.get(a.name, 0)})"
            for a in self.agents.values() if a.role.value != "image_generator"
        )
        system = self._config.system_prompt.format(
            max_agent_turns=self._config.max_agent_turns,
            user_name=self._user_name,
            last_speaker=last_speaker or "N/A",
            min_responders=self._config.min_responders,
            response_threshold=self._config.response_threshold,
            high_relevance_threshold=self._config.high_relevance_threshold,
        )

        recent = state.history[-self._config.context_window :]
        
        # Sanitize history by stripping @mentions. This strictly enforces via Python
        # that past @mentions do not bias the LLM into thinking an agent is still
        # being addressed in subsequent turns.
        history_lines = []
        for m in recent:
            cleaned_content = re.sub(r'@\w+', '', m.content).strip()
            cleaned_content = re.sub(r'!\[.*?\]\(data:image/[^;]+;base64,[^\)]+\)', '[Image omitted from history]', cleaned_content)
            history_lines.append(f"[{m.sender}]: {cleaned_content}")
            
        history_text = "\n".join(history_lines)

        user_msg = (
            f"Agents in the simulation:\n{agent_descriptions}\n\n"
            f"Recent conversation:\n{history_text}\n\n"
            "Which agents should respond next? Assign probabilities and select responders."
        )

        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)

        # Parse response probabilities if provided.
        probabilities = data.get("response_probabilities", {})
        if not isinstance(probabilities, dict):
            probabilities = {}

        # Get the responding_agents list.
        agent_names = data.get("responding_agents", [])
        if isinstance(agent_names, str):
            agent_names = [agent_names]
        if not isinstance(agent_names, list):
            agent_names = []

        # If probabilities are provided but responding_agents is empty or
        # insufficient, derive responders from probabilities.
        if probabilities and len(agent_names) < self._config.min_responders:
            # Sort agents by probability descending.
            sorted_agents = sorted(
                probabilities.items(), key=lambda x: x[1], reverse=True,
            )
            # Pick agents above threshold, or top-N if not enough.
            above_threshold = [
                name for name, prob in sorted_agents
                if prob >= self._config.response_threshold
                and name in self.agents
                and self.agents[name].role.value != "image_generator"
                and name != last_speaker
                and (not exclude_agents or name not in exclude_agents)
            ]
            if len(above_threshold) >= self._config.min_responders:
                agent_names = above_threshold
            else:
                # Fall back to top-N by probability.
                agent_names = [
                    name for name, _ in sorted_agents
                    if name in self.agents 
                    and self.agents[name].role.value != "image_generator"
                    and name != last_speaker 
                    and (not exclude_agents or name not in exclude_agents)
                ][: self._config.min_responders]

        # Filter to valid agent names, exclude last speaker, cap at max_agent_turns.
        valid = [n for n in agent_names if n in self.agents and self.agents[n].role.value != "image_generator"]
        if last_speaker:
            valid = [n for n in valid if n != last_speaker]
        if exclude_agents:
            valid = [n for n in valid if n not in exclude_agents]
        valid = valid[: self._config.max_agent_turns]

        # Detect if the orchestrator explicitly chose silence (empty responding_agents
        # AND all probabilities below threshold). In that case, respect the decision
        # and skip stochastic filtering + min_responders guarantee — the agents should
        # stay quiet (e.g. a question was directed at the user).
        orchestrator_chose_silence = (
            not data.get("responding_agents")
            and bool(probabilities)
            and all(
                prob < self._config.response_threshold
                for prob in probabilities.values()
            )
        )
        # If the user was the last speaker (last_speaker is None), we MUST
        # guarantee at least one response, so we ignore orchestrator_chose_silence.
        if orchestrator_chose_silence and last_speaker is not None:
            return []

        # Stochastic talkativeness filter: reduce overall agent vocality and
        # make each agent's participation rate reflect their talkativeness.
        # Agents whose LLM-assigned probability >= high_relevance_threshold
        # (directly addressed or role-critical) always pass through.
        # All others pass with probability: talkativeness * base_vocal_factor.
        stochastic_valid: list[str] = []
        for name in valid:
            # `valid` was already filtered to self.agents keys above, so
            # get() will always return an Agent — the fallback is defensive only.
            agent = self.agents.get(name)
            talkativeness = agent.talkativeness if agent else 0.5
            llm_prob = probabilities.get(name, 0.5)
            if llm_prob >= self._config.high_relevance_threshold or random.random() < talkativeness * self._config.base_vocal_factor:
                stochastic_valid.append(name)

        # Guard: if the LLM explicitly chose agents but the stochastic filter
        # silenced all of them, keep the highest-probability one so the user
        # always receives a reply when the orchestrator intended one.
        # Fall back to talkativeness when the LLM did not supply probabilities.
        if valid and not stochastic_valid:
            def _agent_score(n: str) -> float:
                if n in probabilities:
                    return probabilities[n]
                agent = self.agents.get(n)
                return agent.talkativeness if agent else 0.5

            best = max(valid, key=_agent_score)
            stochastic_valid = [best]

        valid = stochastic_valid

        # Ensure minimum responders if possible (and if we have enough agents).
        if len(valid) < self._config.min_responders:
            available = [
                n for n in self.agents
                if n not in valid 
                and self.agents[n].role.value != "image_generator"
                and n != last_speaker 
                and (not exclude_agents or n not in exclude_agents)
            ]
            # Sort by least spoken first for fairness.
            available.sort(key=lambda n: self._agent_speak_counts.get(n, 0))
            while len(valid) < self._config.min_responders and available:
                valid.append(available.pop(0))

        for name in valid:
            self._agent_speak_counts[name] = (
                self._agent_speak_counts.get(name, 0) + 1
            )

        return valid

    def get_agent(self, name: str) -> Agent | None:
        return self.agents.get(name)