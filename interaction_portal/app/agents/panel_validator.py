"""
Panel validation and proportional trimming for the Orchestrator panel.

Enforces:
- Required sections present and in strict order
- No extra sections for lower manipulation levels (COORD must not have card/dev sections)
- Strict word count budget (60-80 words)
- Absence of forbidden imperatives ("you should", "ask", "try", "make sure", etc.)
- Absence of hidden client facts (via disclosure detector)
- No mention of unasked specialists when name_unasked is false
"""

from __future__ import annotations

import re
from typing import Sequence

from .disclosure import detect_disclosure

SECTION_NAMES = ["Team status", "On the card", "Why it recurs"]

FORBIDDEN_IMPERATIVES = re.compile(
    r"\b(you\s+(?:should|must|need\s+to|have\s+to|ought\s+to)|"
    r"(?:please\s+)?(?:ask|try|make\s+sure|ensure|remember\s+to|don't\s+forget))\b",
    re.IGNORECASE,
)


def word_count(text: str) -> int:
    return len(text.split())


def extract_sections(text: str) -> dict[str, str]:
    """Split panel text into sections keyed by header label."""
    positions = []
    for label in SECTION_NAMES:
        m = re.search(rf"(?:^|\s|\n)({re.escape(label)})\s*:", text, re.IGNORECASE)
        if m:
            positions.append((m.start(1), label))
    positions.sort(key=lambda t: t[0])

    sections = {}
    for i, (pos, label) in enumerate(positions):
        next_pos = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        content = text[pos:next_pos].strip()
        # Strip header from content
        header_re = rf"^{re.escape(label)}\s*:\s*"
        body = re.sub(header_re, "", content, flags=re.IGNORECASE).strip()
        sections[label] = body
    return sections


def validate_panel(
    text: str,
    level: str,
    expected_sections: list[str],
    word_budget: tuple[int, int],
    hidden_passages: dict[str, str] | None = None,
    name_unasked: bool = True,
    unasked_specialists: list[str] | None = None,
) -> list[str]:
    """Validate panel text against experimental design rules. Returns a list of error strings."""
    errors: list[str] = []
    wc = word_count(text)
    lo, hi = word_budget

    if wc < lo:
        errors.append(f"Word count {wc} is below minimum {lo}")
    elif wc > hi:
        errors.append(f"Word count {wc} exceeds maximum {hi}")

    # Check sections present
    sections = extract_sections(text)
    found_labels = list(sections.keys())

    for req in expected_sections:
        if req not in sections or not sections[req].strip():
            errors.append(f"Required section '{req}' is missing or empty")

    # Check forbidden extra sections for lower levels
    for s_name in SECTION_NAMES:
        if s_name not in expected_sections and s_name in sections:
            errors.append(f"Section '{s_name}' is forbidden for level '{level}'")

    # Check section order
    expected_order = [s for s in SECTION_NAMES if s in expected_sections]
    if found_labels != expected_order:
        errors.append(f"Section order {found_labels} does not match expected {expected_order}")

    # Check forbidden imperatives
    m_imp = FORBIDDEN_IMPERATIVES.search(text)
    if m_imp:
        errors.append(f"Contains forbidden imperative phrase: '{m_imp.group(0)}'")

    # Check hidden facts / client disclosures
    if hidden_passages:
        leaks = detect_disclosure(text, hidden_passages)
        if leaks:
            errors.append(f"Contains private client facts from hidden items: {leaks}")

    # Check unasked specialists if name_unasked is false
    if not name_unasked and unasked_specialists:
        for name in unasked_specialists:
            if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
                errors.append(f"Mentions unasked specialist '{name}' when name_unasked is false")

    return errors


def proportional_trim(text: str, expected_sections: list[str], max_words: int) -> str:
    """Trim a multi-section panel down to max_words without dropping any required section."""
    sections = extract_sections(text)
    if not sections:
        words = text.split()[:max_words]
        trimmed = " ".join(words).rstrip(",;:")
        return trimmed + ("." if not trimmed.endswith((".", "!", "?")) else "")

    # Retain only expected sections
    retained = {s: sections.get(s, "") for s in expected_sections if s in sections}
    if not retained:
        retained = sections

    # Allocate target word count per section proportionally based on original length
    orig_lens = {s: max(1, len(content.split())) for s, content in retained.items()}
    # Reserve header words ("Label:")
    header_overhead = sum(len(s.split()) for s in retained)
    available_words = max(len(retained) * 5, max_words - header_overhead)

    total_orig = sum(orig_lens.values())
    target_lens = {
        s: max(3, int(available_words * (l / total_orig)))
        for s, l in orig_lens.items()
    }

    # Adjust sum to fit exactly
    while sum(target_lens.values()) > available_words:
        # reduce from largest
        largest = max(target_lens, key=lambda k: target_lens[k])
        if target_lens[largest] <= 3:
            break
        target_lens[largest] -= 1

    _DANGLING = {"a", "an", "the", "and", "or", "but", "to", "of", "in", "on", "for", "with", "by", "at", "from", "as", "is", "are", "that", "which"}
    parts = []
    for s in expected_sections:
        if s not in retained:
            continue
        body_words = retained[s].split()[: target_lens[s]]
        while len(body_words) > 3 and body_words[-1].lower().rstrip(",;:") in _DANGLING:
            body_words.pop()
        body_text = " ".join(body_words).rstrip(",;:")
        if body_text and not body_text.endswith((".", "!", "?")):
            body_text += "."
        parts.append(f"{s}: {body_text}")

    result = " ".join(parts)
    # Ensure not over max_words
    res_words = result.split()
    if len(res_words) > max_words:
        result = " ".join(res_words[:max_words]).rstrip(",;:")
        if not result.endswith((".", "!", "?")):
            result += "."
    return result
