"""
OrchestratorAgent: assigns work among the specialists and writes the side panel.

It holds NO client material. v1's probabilistic "who replies next" selector is gone; the
orchestrator now has two jobs:
  * ``plan`` (coordination burst): short assignments to specialists, shown as a folded block;
  * ``generate_panel``: the read-only right-hand panel at one of three levels, held to a fixed
    word budget by a check-and-retry guardrail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config_loader import ClientConfig, PortalConfig
from .base import LLMClient, LLMResult, call_with_retry, format_transcript
from .panel_validator import proportional_trim, validate_panel, word_count
from .schemas import Assignment, OrchestratorOutput, extract_json
from .specialist import clean_reply


@dataclass
class PanelResult:
    text: str
    word_count: int
    sections_present: list[str]
    retried: bool = False
    truncated: bool = False
    validation_errors: list[str] = field(default_factory=list)
    panel_degraded: bool = False
    llm: LLMResult | None = None


def sections_in(text: str, labels: list[str]) -> list[str]:
    return [l for l in labels if re.search(rf"(?:^|\s|\n){re.escape(l)}\s*:", text, re.I)]


class OrchestratorAgent:
    name_key = "Orchestrator"

    def __init__(self, cfg: PortalConfig, client: ClientConfig, llm: LLMClient) -> None:
        self.cfg, self.client, self.llm = cfg, client, llm
        self.on_error = None

    @property
    def name(self) -> str:
        return self.cfg.orchestrator_name

    def _brief(self) -> str:
        return "\n".join(f"- {r}" for r in self.client.requirements) + "\n" + self.client.body

    def system_prompt(self) -> str:
        return self.cfg.prompts["orchestrator_plan"].format(
            team_roster=self.cfg.team_roster(exclude=self.name), client_name=self.client.name,
            brief=self._brief(), max_assignments=self.cfg.orchestrator.get("max_assignments", 3),
        )

    # ---- coordination burst -------------------------------------------
    def plan(self, transcript: list[dict], kind: str, text: str = "") -> tuple[OrchestratorOutput, LLMResult]:
        """kind: opening | broadcast | mention (participant addressed the orchestrator)."""
        if kind == "opening":
            ins = ("The session has just started. Assign a first piece of work to the specialists from the three stated "
                   "requirements only, and set draft_card to true.")
        elif kind == "broadcast":
            ins = (f"The participant sent this to the whole team: \"{text}\"\nDecide whether to assign the question to the "
                   "specialist(s) it concerns. Do not speak to the participant.")
        else:
            ins = (f"The participant is speaking to YOU: \"{text}\"\nReply to the participant in to_participant. You hold no "
                   "client material: if they ask for client facts, say so and name the specialist who covers it.")
        user = "Team chat so far:\n" + format_transcript(transcript) + "\n\n" + ins + "\n\nReturn the JSON object now."
        meta = {"role": "orchestrator", "kind": kind, "text": text, "client": self.client, "transcript": transcript}
        system = self.system_prompt()

        def run():
            res = self.llm.complete(system, user, temperature=self.cfg.llm.orchestrator_temperature,
                                    max_tokens=self.cfg.llm.orchestrator_max_tokens, json_mode=True, meta=meta)
            return OrchestratorOutput.model_validate(extract_json(res.text)), res

        out, res = call_with_retry(self.llm, self.name, run, self.cfg.llm.retries, on_error=self.on_error)
        valid = {n.lower(): n for n in self.cfg.specialist_names}
        cleaned = []
        for a in out.assignments[: self.cfg.orchestrator.get("max_assignments", 3)]:
            n = valid.get(a.to.strip().lstrip("@").lower())
            if n:
                cleaned.append(Assignment(to=n, text=clean_reply(a.text, self.name, self.cfg.all_agent_names)))
        out.assignments = cleaned
        out.to_participant = clean_reply(out.to_participant, self.name, self.cfg.all_agent_names)
        return out, res

    # ---- panel ----------------------------------------------------------
    def generate_panel(
        self,
        level: str,
        transcript: list[dict],
        status: str,
        card_status: str,
        unasked_specialists: list[str] | None = None,
    ) -> PanelResult:
        p = self.cfg.panel
        lo, hi = p.word_budget
        labels = p.sections[level]
        name_rule = ("You may name specialists who have not been asked yet." if p.name_unasked
                     else "Do not name specialists who have not been asked yet.")
        common = self.cfg.prompts["panel__common"].format(
            word_min=lo, word_max=hi, word_target=p.word_target, forbidden=p.forbidden_language,
            name_unasked_rule=name_rule, brief=self._brief(), status=status, card_status=card_status,
            transcript=format_transcript(transcript, limit=25),
        )
        prompt = self.cfg.prompts[f"panel_{level}"].replace("{common}", common)
        meta = {"role": "panel", "level": level, "labels": labels, "client": self.client,
                "transcript": transcript, "status": status, "card_status": card_status, "word_budget": (lo, hi)}

        hidden_passages = {}
        for pm in self.client.private.values():
            hidden_passages.update(pm.hidden_passages)

        def run():
            return self.llm.complete("You write concise status panels.", prompt, temperature=self.cfg.llm.panel_temperature,
                                     max_tokens=self.cfg.llm.panel_max_tokens, json_mode=False, meta=meta)

        res = call_with_retry(self.llm, self.name, run, self.cfg.llm.retries, on_error=self.on_error)
        text = res.text.strip()
        retried = truncated = panel_degraded = False

        errs = validate_panel(
            text, level, labels, (lo, hi),
            hidden_passages=hidden_passages,
            name_unasked=p.name_unasked,
            unasked_specialists=unasked_specialists,
        )

        retries_done = 0
        while errs and retries_done < 2:
            retried = True
            retries_done += 1
            retry_prompt = (
                prompt
                + f"\n\nCRITICAL CORRECTION REQUIRED: The previous draft had validation errors:\n"
                + "\n".join(f"- {e}" for e in errs)
                + f"\nPlease rewrite strictly following all instructions and keeping within {lo}-{hi} words."
            )
            try:
                res = call_with_retry(
                    self.llm, self.name,
                    lambda: self.llm.complete("You write concise status panels.", retry_prompt,
                                              temperature=self.cfg.llm.panel_temperature,
                                              max_tokens=self.cfg.llm.panel_max_tokens, json_mode=False, meta=meta),
                    0, on_error=self.on_error
                )
                text = res.text.strip()
                errs = validate_panel(
                    text, level, labels, (lo, hi),
                    hidden_passages=hidden_passages,
                    name_unasked=p.name_unasked,
                    unasked_specialists=unasked_specialists,
                )
            except Exception:
                break

        if errs and word_count(text) > hi:
            retried = True
            cprompt = self.cfg.prompts["panel_compress"].format(
                word_min=lo, word_max=hi, word_target=p.word_target, labels=", ".join(labels), text=text)
            cmeta = {**meta, "role": "compress", "text": text}
            try:
                res2 = call_with_retry(self.llm, self.name,
                                      lambda: self.llm.complete("You edit text to a strict word range.", cprompt,
                                                               temperature=0.0, max_tokens=self.cfg.llm.panel_max_tokens,
                                                               json_mode=False, meta=cmeta),
                                      0, on_error=self.on_error)
                candidate = res2.text.strip()
                c_errs = validate_panel(
                    candidate, level, labels, (lo, hi),
                    hidden_passages=hidden_passages,
                    name_unasked=p.name_unasked,
                    unasked_specialists=unasked_specialists,
                )
                if len(c_errs) <= len(errs):
                    text = candidate
                    errs = c_errs
            except Exception:
                pass

        if errs or word_count(text) > hi:
            text = proportional_trim(text, labels, hi)
            truncated = True
            panel_degraded = True
            errs = validate_panel(
                text, level, labels, (lo, hi),
                hidden_passages=hidden_passages,
                name_unasked=p.name_unasked,
                unasked_specialists=unasked_specialists,
            )

        return PanelResult(
            text=text, word_count=word_count(text), sections_present=sections_in(text, labels),
            retried=retried, truncated=truncated, validation_errors=errs,
            panel_degraded=panel_degraded, llm=res
        )
