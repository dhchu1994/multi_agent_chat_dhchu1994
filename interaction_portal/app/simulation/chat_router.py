"""
Message routing for the team chat and folded-block grouping.

* ``@name ...`` goes to that agent only (several leading @mentions are allowed).
* No leading @ goes to the whole team: specialists reply only if the message concerns them.
* A mistyped @name is never dropped silently: ``parse_message`` returns an error with the
  closest valid names so the UI can show an inline warning and nothing is sent.
* Folding is display only: ``group_blocks`` groups internal messages by ``block_id`` for the
  UI, while agents always receive the full transcript.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

BROADCAST_ALIASES = {"team", "all", "everyone"}
_MENTION = re.compile(r"^\s*@([A-Za-z][\w-]*)")


@dataclass
class Route:
    kind: str                         # broadcast | directed | error
    addressees: list[str] = field(default_factory=list)
    mention_used: bool = False
    text: str = ""                    # message text as sent (unchanged)
    error: str | None = None
    unknown: str | None = None
    suggestions: list[str] = field(default_factory=list)


def parse_message(text: str, names: list[str]) -> Route:
    """Resolve addressees from the message text. ``names`` = all addressable agents."""
    raw = text.strip()
    lower = {n.lower(): n for n in names}
    addressees: list[str] = []
    rest = raw
    mention_used = False
    while True:
        m = _MENTION.match(rest)
        if not m:
            break
        mention_used = True
        token = m.group(1)
        if token.lower() in BROADCAST_ALIASES:
            addressees = []
            rest = rest[m.end():]
            break
        if token.lower() not in lower:
            close = difflib.get_close_matches(token.lower(), list(lower), n=3, cutoff=0.4)
            return Route("error", mention_used=True, text=raw, unknown=token,
                         suggestions=[lower[c] for c in close] or list(names))
        name = lower[token.lower()]
        if name not in addressees:
            addressees.append(name)
        rest = rest[m.end():]
    if not mention_used:
        # a stray "@" later in the text is also checked, so a typo never silently goes nowhere
        for m in re.finditer(r"(?<!\w)@([A-Za-z][\w-]*)", raw):
            tok = m.group(1).lower()
            if tok not in lower and tok not in BROADCAST_ALIASES:
                close = difflib.get_close_matches(tok, list(lower), n=3, cutoff=0.4)
                return Route("error", mention_used=True, text=raw, unknown=m.group(1),
                             suggestions=[lower[c] for c in close] or list(names))
    if addressees:
        return Route("directed", addressees, True, raw)
    return Route("broadcast", [], mention_used, raw)


def format_options(options: list[str]) -> str:
    return ", ".join("@" + o for o in options)


def block_label(messages: list[dict], orchestrator: str) -> dict:
    """Header data for a folded block: who was involved, how many messages, when."""
    involved: list[str] = []
    for m in messages:
        for n in [m["sender"], *m.get("to", [])]:
            if n != orchestrator and n not in involved and n not in ("everyone", "Participant"):
                involved.append(n)
    first = messages[0]["ts"] if messages else ""
    return {"block_id": messages[0].get("block_id") if messages else None, "involved": involved,
            "count": len(messages), "ts": first}


def group_blocks(messages: list[dict]) -> list[dict]:
    """Group a flat transcript into display items: plain messages and folded blocks, in order."""
    items: list[dict] = []
    index: dict[str, dict] = {}
    for m in messages:
        bid = m.get("block_id")
        if not bid:
            items.append({"type": "message", "message": m})
            continue
        if bid not in index:
            blk = {"type": "block", "block_id": bid, "messages": []}
            index[bid] = blk
            items.append(blk)
        index[bid]["messages"].append(m)
    return items
