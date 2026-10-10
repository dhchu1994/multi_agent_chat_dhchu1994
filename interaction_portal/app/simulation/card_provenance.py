"""
Span-level provenance for the five-field deliverable card.

Each field is an ordered list of spans ``{span_id, text, author, status, history}``. Authors are
a specialist's name (AI) or ``"user"``. The editor never works on a raw string: AI proposals are
spans; keep/cut/send-back change a span's status; typing is diffed against the existing spans so
untouched text keeps its original author and newly typed text becomes a ``user`` span.

Included text per role
  passive    : every AI span (the team wrote the card; nothing can be changed)
  evaluative : only spans the participant has KEPT
  generative : every live span (typed + inserted suggestions); typed share must reach the minimum
  none       : every live span (the participant writes the whole card)
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from ..event_store import utc_now_iso

AI_LIVE = {"proposed", "kept", "inserted"}


@dataclass
class Span:
    span_id: str
    text: str
    author: str          # specialist name or "user"
    status: str          # proposed | kept | cut | sent_back | typed | inserted | deleted
    history: list[dict] = field(default_factory=list)
    parent: str | None = None

    def to_dict(self) -> dict:
        return {"span_id": self.span_id, "text": self.text, "author": self.author, "status": self.status,
                "history": self.history, "parent": self.parent}

    @staticmethod
    def from_dict(d: dict) -> "Span":
        return Span(d["span_id"], d["text"], d["author"], d["status"], list(d.get("history", [])), d.get("parent"))


class CardError(ValueError):
    pass


class Card:
    def __init__(self, field_ids: list[str], role: str, send_back_needs_reason: bool = True,
                 min_typed_share: float = 0.25) -> None:
        self.role = role
        self.field_ids = list(field_ids)
        self.send_back_needs_reason = send_back_needs_reason
        self.min_typed_share = min_typed_share
        self.fields: dict[str, list[Span]] = {f: [] for f in field_ids}
        self.suggestions: dict[str, list[Span]] = {f: [] for f in field_ids}   # generative: AI suggestions
        self._n = 0

    # ---- ids / history -------------------------------------------------
    def _new_id(self) -> str:
        self._n += 1
        return f"spn_{self._n:02d}"

    @staticmethod
    def _h(action: str, by: str, **extra: Any) -> dict:
        return {"action": action, "by": by, "timestamp": utc_now_iso(), **extra}

    def _find(self, span_id: str) -> tuple[str, Span]:
        for f, spans in {**self.fields, **{f"sugg:{k}": v for k, v in self.suggestions.items()}}.items():
            for s in spans:
                if s.span_id == span_id:
                    return f.removeprefix("sugg:"), s
        raise CardError(f"unknown span {span_id}")

    # ---- AI proposals --------------------------------------------------
    def add_ai_text(self, field_id: str, text: str, author: str, replaces: str | None = None) -> tuple[Span, dict]:
        """A specialist proposes card text. Returns the span and a card_action-style event dict.

        Passive/evaluative: it becomes a card span. Generative/none: it is only a suggestion.
        """
        self._check_field(field_id)
        text = text.strip()
        if self.role in ("generative", "none"):
            span = Span(self._new_id(), text, author, "suggested", [self._h("created", author)])
            self.suggestions[field_id].append(span)
            return span, self._evt(field_id, span, "created", 0, 0, author)
        # Replace the previous AI draft of the same field by the same author (still proposed)
        for s in self.fields[field_id]:
            if s.author == author and s.status == "proposed":
                s.status = "deleted"
                s.history.append(self._h("replaced", author))
            if replaces and s.span_id == replaces:
                s.history.append(self._h("replaced_by_revision", author))
        span = Span(self._new_id(), text, author, "proposed", [self._h("created", author)], parent=replaces)
        self.fields[field_id].append(span)
        return span, self._evt(field_id, span, "created", len(text), 0, author)

    # ---- evaluative actions -------------------------------------------
    def evaluate(self, span_id: str, action: str, reason: str = "") -> tuple[str, Span, dict]:
        if self.role != "evaluative":
            raise CardError("this role cannot keep, cut or send back")
        field_id, span = self._find(span_id)
        if span.author == "user" or span.status in ("deleted",):
            raise CardError("only the team's spans can be evaluated")
        if action not in ("keep", "cut", "send_back"):
            raise CardError("unknown action")
        if action == "send_back" and self.send_back_needs_reason and not reason.strip():
            raise CardError("a reason is required to send a part back")
        span.status = {"keep": "kept", "cut": "cut", "send_back": "sent_back"}[action]
        span.history.append(self._h(action, "user", reason=reason.strip() or None))
        removed = len(span.text) if action in ("cut", "send_back") else 0
        return field_id, span, self._evt(field_id, span, action, 0, removed, span.author, reason.strip())

    # ---- generative / none: typing ---------------------------------------
    def set_field_text(self, field_id: str, new_text: str, max_chars: int = 600) -> list[dict]:
        """Apply the editor's full text for a field, diffing against the current spans."""
        if self.role not in ("generative", "none"):
            raise CardError("this role cannot type on the card")
        self._check_field(field_id)
        new_text = new_text[:max_chars]
        spans = [s for s in self.fields[field_id] if s.status not in ("deleted",)]
        old_chars: list[tuple[str, Span]] = [(ch, s) for s in spans for ch in s.text]
        old_text = "".join(c for c, _ in old_chars)
        if old_text == new_text:
            return []
        sm = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
        events: list[dict] = []
        pieces: list[Span] = []          # new live span list
        consumed: dict[str, int] = {}    # chars of original spans retained
        added = removed = 0

        def flush_retained(i1: int, i2: int) -> None:
            nonlocal pieces
            run: list[tuple[str, Span]] = []
            for ch, sp in old_chars[i1:i2]:
                if run and run[-1][1] is not sp:
                    emit(run); run = []
                run.append((ch, sp))
            if run:
                emit(run)

        def emit(run: list[tuple[str, Span]]) -> None:
            sp = run[0][1]
            txt = "".join(c for c, _ in run)
            consumed[sp.span_id] = consumed.get(sp.span_id, 0) + len(txt)
            if txt == sp.text and consumed[sp.span_id] == len(sp.text) and not any(p.span_id == sp.span_id for p in pieces):
                pieces.append(sp)
            else:
                pieces.append(Span(self._new_id(), txt, sp.author, sp.status,
                                   sp.history + [self._h("edited_split", "user")], parent=sp.span_id))

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                flush_retained(i1, i2)
                continue
            if i2 > i1:
                removed += i2 - i1
                gone = {sp.span_id: sp for _, sp in old_chars[i1:i2]}
                for sp in gone.values():
                    events.append(self._evt(field_id, sp, "delete", 0, sum(1 for _, s in old_chars[i1:i2] if s is sp), sp.author))
            if j2 > j1:
                txt = new_text[j1:j2]
                added += len(txt)
                new = Span(self._new_id(), txt, "user", "typed", [self._h("typed", "user")])
                pieces.append(new)
                events.append(self._evt(field_id, new, "type", len(txt), 0, "user"))
        # Mark originals that did not survive intact as deleted for the audit trail, keep order
        survivors = {p.span_id for p in pieces}
        graveyard = [s for s in self.fields[field_id] if s.status == "deleted"]
        for s in spans:
            if s.span_id not in survivors:
                s.status = "deleted"
                s.history.append(self._h("deleted_or_edited", "user"))
                graveyard.append(s)
        self.fields[field_id] = graveyard + pieces
        return events

    def insert_suggestion(self, span_id: str) -> tuple[str, Span, dict]:
        if self.role not in ("generative", "none"):
            raise CardError("suggestions can only be inserted by writers")
        field_id, sugg = self._find(span_id)
        if sugg.status != "suggested":
            raise CardError("not a suggestion")
        sugg.status = "inserted_source"
        sugg.history.append(self._h("inserted", "user"))
        live = [s for s in self.fields[field_id] if s.status != "deleted"]
        text = ("\n" if live and live[-1].text and not live[-1].text.endswith("\n") else "") + sugg.text
        span = Span(self._new_id(), text, sugg.author, "inserted", sugg.history + [self._h("inserted_into_card", "user")],
                    parent=sugg.span_id)
        self.fields[field_id].append(span)
        return field_id, span, self._evt(field_id, span, "type", len(text), 0, sugg.author)

    # ---- read-outs ---------------------------------------------------------
    def _included(self, s: Span) -> bool:
        if s.status == "deleted":
            return False
        if self.role == "passive":
            return s.status in ("proposed", "kept")
        if self.role == "evaluative":
            return s.status == "kept"
        return s.status in ("typed", "inserted")

    def field_text(self, field_id: str) -> str:
        return "".join(s.text for s in self.fields[field_id] if self._included(s))

    def full_text(self) -> dict[str, str]:
        return {f: self.field_text(f) for f in self.field_ids}

    def total_chars(self) -> int:
        return sum(len(t.strip()) for t in self.full_text().values())

    def typed_chars(self) -> int:
        n = 0
        for f in self.field_ids:
            for s in self.fields[f]:
                if self._included(s) and s.author == "user":
                    n += len(s.text)
        return n

    def typed_share(self) -> float:
        total = sum(len(s.text) for f in self.field_ids for s in self.fields[f] if self._included(s))
        return (self.typed_chars() / total) if total else 0.0

    def fields_complete(self) -> int:
        return sum(1 for t in self.full_text().values() if t.strip())

    def provenance_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.field_ids:
            for s in self.fields[f]:
                if self._included(s):
                    out[s.author] = out.get(s.author, 0) + len(s.text)
        return out

    def can_submit(self) -> tuple[bool, str]:
        if self.role == "generative" and self.typed_share() + 1e-9 < self.min_typed_share:
            return False, "min_typed_share"
        return True, ""

    # ---- serialisation -----------------------------------------------------
    def to_json(self, full: bool = True) -> dict:
        return {
            "role": self.role,
            "fields": {f: [s.to_dict() for s in spans if full or s.status != "deleted"] for f, spans in self.fields.items()},
            "suggestions": {f: [s.to_dict() for s in spans if s.status == "suggested"] for f, spans in self.suggestions.items()},
            "typed_share": round(self.typed_share(), 4),
            "fields_complete": self.fields_complete(),
            "text": self.full_text(),
            "provenance_summary": self.provenance_summary(),
            "counter": self._n,
        }

    @classmethod
    def from_json(cls, d: dict, send_back_needs_reason: bool, min_typed_share: float) -> "Card":
        c = cls(list(d["fields"].keys()), d["role"], send_back_needs_reason, min_typed_share)
        for f, spans in d["fields"].items():
            c.fields[f] = [Span.from_dict(s) for s in spans]
        for f, spans in d.get("suggestions", {}).items():
            c.suggestions[f] = [Span.from_dict(s) for s in spans]
        c._n = int(d.get("counter", 0))
        return c

    # ---- helpers --------------------------------------------------------------
    def _check_field(self, field_id: str) -> None:
        if field_id not in self.fields:
            raise CardError(f"unknown field {field_id}")

    @staticmethod
    def _evt(field_id: str, span: Span, action: str, added: int, removed: int, author: str, reason: str = "") -> dict:
        return {"field": field_id, "span_id": span.span_id, "action": action, "reason": reason or None,
                "chars_added": added, "chars_removed": removed, "author": author}
