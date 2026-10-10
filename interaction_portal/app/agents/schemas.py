"""Pydantic models for structured agent outputs (single-pass JSON contracts)."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field


class CardProposal(BaseModel):
    field: str
    text: str


class SpecialistOutput(BaseModel):
    reply_text: str = ""
    disclosed_hidden_items: list[str] = Field(default_factory=list)
    declined: bool = False
    redirect_to: str | None = None
    card_proposal: CardProposal | None = None


class Assignment(BaseModel):
    to: str
    text: str


class OrchestratorOutput(BaseModel):
    to_participant: str = ""
    assignments: list[Assignment] = Field(default_factory=list)
    draft_card: bool = False


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model reply, tolerating code fences and prose around it."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("no JSON object in model reply")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("model reply is not a JSON object")
    return obj
