"""
Offline, deterministic model used when ``llm.provider: mock``.

It exists so that the portal, both harnesses and the tests run without a network or an API
key (and without Qualtrics). It follows the same behaviour rules the real prompts impose:
answer only what was asked, never invent, redirect out-of-domain questions, and keep the
orchestrator free of client facts. It is NOT a substitute for running Harness A against the
pinned real model before any participant runs.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..config_loader import PortalConfig
from . import topics
from .base import LLMClient, LLMResult

_CARD_WORDS = re.compile(r"\b(draft|card|propose|proposal|write up|recommendation)\b", re.I)


def _public_facts(private_text: str) -> list[str]:
    facts = []
    for line in private_text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            facts.append(line[2:].strip())
    return facts


class MockClient(LLMClient):
    def __init__(self, cfg: PortalConfig) -> None:
        self.cfg = cfg
        self.model_version = "mock-1"

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int,
                 json_mode: bool = True, meta: dict[str, Any] | None = None) -> LLMResult:
        meta = meta or {}
        role = meta.get("role")
        t0 = time.monotonic()
        if role == "specialist":
            text = json.dumps(self._specialist(meta))
        elif role == "orchestrator":
            text = json.dumps(self._orchestrator(meta))
        elif role == "panel":
            text = self._panel(meta)
        elif role == "compress":
            text = self._compress(meta)
        else:
            text = "{}"
        ms = int((time.monotonic() - t0) * 1000)
        return LLMResult(text=text, tokens_in=max(1, len(system + user) // 4), tokens_out=max(1, len(text) // 4),
                         first_token_ms=ms, total_ms=ms)

    # ---- specialist ---------------------------------------------------------
    def _proposal(self, meta: dict, field: str, reason: str = "") -> dict:
        client, spec, priv = meta["client"], meta["spec"], meta["private"]
        req = client.requirements
        said = " ".join(m["text"] for m in meta["transcript"])
        learned = ""
        for hid, passage in priv.hidden_passages.items():
            if passage[:40] in said:
                learned = f" Also: {passage.split(':', 1)[-1].strip()}"
        base = {
            "audience_angle": f"Speak to this audience: {req[0].rstrip('.')}.",
            "creative_concept": f"Build the concept around this lead: {req[1].rstrip('.')}.",
            "channel_format": f"Use the client's existing channels and plan for this timing: {req[2].rstrip('.')}.",
            "compliance_note": "Keep all wording factual and check any claim before it is used.",
            "stated_constraint": f"Respect the stated timing: {req[2].rstrip('.')}.",
        }.get(field, f"Draft for {field}.")
        if reason:
            base += f" Revised after your note: {reason.strip()}"
        words = (base + learned).split()
        return {"field": field, "text": " ".join(words[:35])}

    def _specialist(self, meta: dict) -> dict:
        spec, priv, trig, text = meta["spec"], meta["private"], meta["trigger"], meta["text"]
        out: dict[str, Any] = {"reply_text": "", "disclosed_hidden_items": [], "declined": False,
                               "redirect_to": None, "card_proposal": None}
        if trig == "draft_card":
            out["card_proposal"] = self._proposal(meta, spec.card_fields[0])
            out["reply_text"] = f"I have drafted the {spec.card_fields[0].replace('_', ' ')} from the brief."
            return out
        if trig == "revise":
            ex = meta["extra"]
            out["card_proposal"] = self._proposal(meta, ex["field"], ex.get("reason") or "")
            out["reply_text"] = "I have rewritten that part."
            return out

        facts = _public_facts(priv.text_plain)
        own = topics.domain_score(spec, text)
        other_best = topics.best_domain(self.cfg, text)
        hidden_id = priv.hidden_items[0]

        if trig == "orchestrator":
            out["reply_text"] = f"Understood. From the brief, I will look at this: {facts[0].rstrip('.')}." if facts else "Understood."
            return out

        if own == 0 and other_best and other_best != spec.name:
            if trig == "broadcast":
                return out                       # not my question: stay silent
            out.update(reply_text=f"I do not hold that. {other_best} covers it.", declined=True, redirect_to=other_best)
            return out
        if own == 0:
            if trig == "broadcast":
                return out
            out["reply_text"] = f"I hold {spec.domain.split('.')[-2].strip() if '.' in spec.domain else 'my own material'}. What would you like to know?"
            return out

        # question concerns my domain
        if topics.asked_about_hidden(spec, text):
            passage = priv.hidden_passages[hidden_id]
            out["reply_text"] = passage
            out["disclosed_hidden_items"] = [hidden_id]
        else:
            out["reply_text"] = facts[0] if facts else "I can help with that."
        if _CARD_WORDS.search(text):
            out["card_proposal"] = self._proposal(meta, spec.card_fields[0])
        return out

    # ---- orchestrator ---------------------------------------------------------
    def _orchestrator(self, meta: dict) -> dict:
        kind, text, client = meta["kind"], meta["text"], meta["client"]
        cfg = self.cfg
        if kind == "opening":
            reqs = client.requirements
            return {"to_participant": "", "draft_card": True, "assignments": [
                {"to": "Nia", "text": f"Profile the stated audience: {reqs[0].rstrip('.')}."},
                {"to": "Theo", "text": f"Sketch a concept that leads as the brief says: {reqs[1].rstrip('.')}."},
                {"to": "Mira", "text": f"Outline channel and format against the stated timing: {reqs[2].rstrip('.')}."},
            ]}
        owner = topics.best_domain(cfg, text)
        if kind == "broadcast":
            if not owner:
                return {"to_participant": "", "assignments": [], "draft_card": False}
            return {"to_participant": "", "draft_card": False,
                    "assignments": [{"to": owner, "text": f"The participant asked about your area. Please reply to them."}]}
        # participant spoke to the orchestrator directly
        if owner:
            reply = f"I do not hold client material. {owner} covers that, so ask {owner} directly with @{owner}."
            return {"to_participant": reply, "draft_card": False, "assignments": []}
        roster = ", ".join(f"{s.name} ({s.role_label.lower()})" for s in cfg.specialists)
        return {"to_participant": f"I assign the work among {roster}. I hold no client material myself.",
                "draft_card": False, "assignments": []}

    # ---- panel ----------------------------------------------------------------
    def _card_summary(self, card_raw: str) -> str:
        lines = [line.strip() for line in (card_raw or "").splitlines() if line.strip()]
        if not lines:
            return "Card drafting is in progress"
        first = lines[0].rstrip(".")
        pending_note = ""
        for line in lines[1:]:
            if "proposed part" in line or "not yet judged" in line:
                pending_note = f", and {line.lower().rstrip('.')}"
                break
        return f"{first}{pending_note}"

    def _panel(self, meta: dict) -> str:
        labels, level = meta["labels"], meta["level"]
        lo, hi = meta["word_budget"]
        status, card = meta["status"], meta["card_status"]
        summary = self._card_summary(card)
        parts = {
            "Team status": f"Team status: {status}",
            "On the card": (
                f"On the card: {summary}. Stated brief requirements are the only benchmark "
                "checked so far, while specialist domain verifications remain unconfirmed."
            ),
            "Why it recurs": (
                "Why it recurs: a confident draft built only from the public brief tends to miss what only a "
                "specialist holds, so each gap on a card usually marks an area nobody has been asked about."
            ),
        }
        pad = ("Nothing here is a verdict on the participant's work and no check not yet made is assumed to pass.")
        text = " ".join(parts[l] for l in labels)
        guard = 0
        while len(text.split()) < lo and guard < 3:
            text += " " + pad
            guard += 1
        if len(text.split()) > hi:
            text = self._trim(text, hi, labels)
        return text

    @staticmethod
    def _trim(text: str, hi: int, labels: list[str]) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        out: list[str] = []
        n = 0
        for s in sentences:
            w = len(s.split())
            if n + w > hi:
                break
            out.append(s)
            n += w
        return " ".join(out) if out else " ".join(text.split()[:hi])

    def _compress(self, meta: dict) -> str:
        lo, hi = meta["word_budget"]
        text = meta["text"]
        if len(text.split()) > hi:
            return self._trim(text, hi, meta["labels"])
        return text
