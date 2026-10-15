"""SpecialistAgent: Nia, Theo, Rhys and Mira, each with private client material."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config_loader import ClientConfig, PortalConfig, SpecialistConfig
from . import topics
from .base import LLMClient, LLMResult, call_with_retry, format_transcript
from .disclosure import detect_disclosure
from .schemas import CardProposal, SpecialistOutput, extract_json


@dataclass
class Trigger:
    """What the specialist is being asked to react to."""
    kind: str                      # broadcast | mention | orchestrator | draft_card | revise
    sender: str = "Participant"
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)   # revise: field, old_text, reason


@dataclass
class SpecialistResult:
    output: SpecialistOutput
    llm: LLMResult
    asked_for: bool = False


def clean_reply(text: str, own_name: str, all_names: list[str]) -> str:
    """Two-layer defence against speaker-tag bleed (carried over from v1's _clean_content).

    Layer 1 strips a leading ``[Name]:`` / ``Name:`` tag. Layer 2 cuts the reply at the first
    line where the model starts speaking as somebody else.
    """
    text = text.strip()
    text = re.sub(rf"^\[?{re.escape(own_name)}\]?\s*(?:->[^:\]]*)?\]?\s*:\s*", "", text, flags=re.I)
    names = "|".join(re.escape(n) for n in all_names + ["Participant"])
    m = re.search(rf"\n\s*\[?(?:{names})\]?\s*(?:->[^:\]]*)?:", text, re.I)
    if m:
        text = text[: m.start()]
    return text.strip()


class SpecialistAgent:
    def __init__(self, cfg: PortalConfig, spec: SpecialistConfig, client: ClientConfig, llm: LLMClient) -> None:
        self.cfg, self.spec, self.client, self.llm = cfg, spec, client, llm
        self.private = client.private[spec.name]

    @property
    def name(self) -> str:
        return self.spec.name

    def system_prompt(self) -> str:
        brief = "\n".join(f"- {r}" for r in self.client.requirements) + "\n" + self.client.body
        return self.cfg.prompts["specialists_system"].format(
            name=self.spec.name, role_label=self.spec.role_label, domain=self.spec.domain,
            team_roster=self.cfg.team_roster(exclude=self.spec.name), client_name=self.client.name,
            brief=brief, private_material=self.private.text_marked, card_rule="",
            card_field_ids=", ".join(self.spec.card_fields),
        )

    def _user_prompt(self, transcript: list[dict], t: Trigger) -> str:
        head = "Team chat so far (internal messages between the orchestrator and specialists included):\n"
        head += format_transcript(transcript) + "\n\n"
        if t.kind == "broadcast":
            ins = (f"The participant sent this to the WHOLE TEAM: \"{t.text}\"\nReply only if it concerns your domain "
                   "or your material; otherwise return an empty reply_text. Answer only what was asked.")
        elif t.kind == "mention":
            ins = f"The participant sent this to YOU ALONE: \"{t.text}\"\nReply to the participant. Answer only what was asked."
        elif t.kind == "orchestrator":
            ins = (f"The orchestrator assigned you this: \"{t.text}\"\nReply to the orchestrator in one or two sentences, "
                   "from the public brief and what has already been said in the chat. Do not volunteer items from your private material.")
        elif t.kind == "draft_card":
            assign = f"The orchestrator asked you: \"{t.text}\".\n" if t.text else ""
            ins = (f"{assign}Draft the card field(s) you cover as a card_proposal from the public brief and what the chat has "
                   "established. Do not volunteer anything from your private material that has not been asked about.")
        elif t.kind == "revise":
            ins = (f"The participant sent back your card text for field \"{t.extra.get('field')}\": \"{t.extra.get('old_text')}\". "
                   f"Reason: \"{t.extra.get('reason') or 'none given'}\". Write a replacement card_proposal for that field.")
        else:
            raise ValueError(t.kind)
        return head + ins + "\n\nReturn the JSON object now."

    def reply(self, transcript: list[dict], trigger: Trigger) -> SpecialistResult:
        system = self.system_prompt()
        user = self._user_prompt(transcript, trigger)
        meta = {"role": "specialist", "name": self.name, "trigger": trigger.kind, "text": trigger.text,
                "extra": trigger.extra, "spec": self.spec, "client": self.client, "private": self.private,
                "transcript": transcript}

        def run() -> tuple[SpecialistOutput, LLMResult]:
            res = self.llm.complete(system, user, temperature=self.cfg.llm.specialist_temperature,
                                    max_tokens=self.cfg.llm.specialist_max_tokens, json_mode=True, meta=meta)
            return SpecialistOutput.model_validate(extract_json(res.text)), res

        out, res = call_with_retry(self.llm, self.name, run, self.cfg.llm.retries,
                                   on_error=getattr(self, "on_error", None))
        out.reply_text = clean_reply(out.reply_text, self.name, self.cfg.all_agent_names)
        out.disclosed_hidden_items = [h for h in out.disclosed_hidden_items if h in self.private.hidden_items]
        if out.redirect_to and out.redirect_to.lower() not in [n.lower() for n in self.cfg.specialist_names if n != self.name]:
            out.redirect_to = None
        if out.card_proposal and out.card_proposal.field not in self.spec.card_fields:
            out.card_proposal = None
        if out.card_proposal:
            out.card_proposal = CardProposal(field=out.card_proposal.field, text=out.card_proposal.text.strip()[: self.cfg.editor.max_span_chars])
        asked = topics.asked_about_hidden(self.spec, trigger.text) if trigger.kind in ("broadcast", "mention") else False

        # Guardrail: check for unprompted disclosure of hidden material in both reply_text and card_proposal
        if self.private.hidden_passages:
            if asked:
                detected_actual = detect_disclosure(out.reply_text, self.private.hidden_passages)
                for hid in detected_actual:
                    if hid in self.private.hidden_items and hid not in out.disclosed_hidden_items:
                        out.disclosed_hidden_items.append(hid)
            else:
                combined_check = out.reply_text + ("\n" + out.card_proposal.text if out.card_proposal else "")
                detected = detect_disclosure(combined_check, self.private.hidden_passages)
                if detected:
                    leak_str = ", ".join(detected)
                    strict_user = user + f"\n\nCRITICAL: In your previous draft you disclosed hidden/private material ({leak_str}) without being asked. Answer WITHOUT mentioning {leak_str} or private constraints."
                    try:
                        def retry_run() -> tuple[SpecialistOutput, LLMResult]:
                            r_res = self.llm.complete(system, strict_user, temperature=self.cfg.llm.specialist_temperature,
                                                      max_tokens=self.cfg.llm.specialist_max_tokens, json_mode=True, meta=meta)
                            return SpecialistOutput.model_validate(extract_json(r_res.text)), r_res
                        out2, res2 = call_with_retry(self.llm, self.name, retry_run, 1)
                        out2.reply_text = clean_reply(out2.reply_text, self.name, self.cfg.all_agent_names)
                        if out2.card_proposal and out2.card_proposal.field not in self.spec.card_fields:
                            out2.card_proposal = None
                        if out2.card_proposal:
                            out2.card_proposal = CardProposal(
                                field=out2.card_proposal.field,
                                text=out2.card_proposal.text.strip()[: self.cfg.editor.max_span_chars],
                            )
                        combined_check2 = out2.reply_text + ("\n" + out2.card_proposal.text if out2.card_proposal else "")
                        detected2 = detect_disclosure(combined_check2, self.private.hidden_passages)
                        if not detected2:
                            out = out2
                            res = res2
                            out.disclosed_hidden_items = [h for h in out.disclosed_hidden_items if h in self.private.hidden_items]
                        else:
                            if detect_disclosure(out.reply_text, self.private.hidden_passages):
                                out.reply_text = "I do not have enough information on that yet."
                            if out.card_proposal and detect_disclosure(out.card_proposal.text, self.private.hidden_passages):
                                out.card_proposal = None
                            out.disclosed_hidden_items = []
                    except Exception:
                        if detect_disclosure(out.reply_text, self.private.hidden_passages):
                            out.reply_text = "I do not have enough information on that yet."
                        if out.card_proposal and detect_disclosure(out.card_proposal.text, self.private.hidden_passages):
                            out.card_proposal = None
                        out.disclosed_hidden_items = []

        return SpecialistResult(out, res, asked)
