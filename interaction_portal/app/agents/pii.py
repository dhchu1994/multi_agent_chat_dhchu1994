"""
PII detection and redaction safeguards for participant text (chat and card fields).
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{3,4}\b")
_URL_RE = re.compile(r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_+.~#?&/=]*)")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b")
_CC_RE = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")
_NAME_INTRO_RE = re.compile(r"\b(?:my\s+name\s+is|i\s+am|i'm|call\s+me)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b", re.IGNORECASE)


def scan_pii(text: str) -> list[str]:
    """Scan text for common personally identifiable information (PII).
    
    Returns a list of detected category strings (e.g. ['email', 'phone']).
    """
    if not text:
        return []

    flags = []
    if _EMAIL_RE.search(text):
        flags.append("email")
    # Only flag phone numbers if sequence of digits is at least 7 digits
    m_phone = _PHONE_RE.search(text)
    if m_phone:
        digits = re.sub(r"\D", "", m_phone.group(0))
        if len(digits) >= 8:
            flags.append("phone")
    if _IBAN_RE.search(text):
        flags.append("iban")
    if _CC_RE.search(text):
        flags.append("credit_card")
    if _URL_RE.search(text):
        flags.append("url")
    if _NAME_INTRO_RE.search(text):
        flags.append("name_intro")

    return flags


def redact_pii(text: str) -> str:
    """Replace detected PII with generic placeholder tags for anonymized exports."""
    if not text:
        return text

    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _CC_RE.sub("[REDACTED_CARD]", text)
    text = _IBAN_RE.sub("[REDACTED_IBAN]", text)
    text = _URL_RE.sub("[REDACTED_URL]", text)

    def redact_phone(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= 8:
            return "[REDACTED_PHONE]"
        return m.group(0)

    text = _PHONE_RE.sub(redact_phone, text)
    text = _NAME_INTRO_RE.sub("my name is [REDACTED_NAME]", text)
    return text
