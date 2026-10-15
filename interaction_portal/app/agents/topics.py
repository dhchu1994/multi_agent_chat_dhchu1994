"""Keyword helpers shared by the offline mock model, the event log and the test harnesses."""

from __future__ import annotations

import re

from ..config_loader import PortalConfig, SpecialistConfig

_WORD = re.compile(r"[a-z']+")


def _stem(w: str) -> str:
    w = w.lower()
    if len(w) > 4:
        if w.endswith("ies") and len(w) > 5:
            return w[:-3] + "y"
        if w.endswith("es") and len(w) > 4:
            return w[:-2]
        if w.endswith("s") and not w.endswith("ss"):
            return w[:-1]
        if w.endswith("ing") and len(w) > 5:
            return w[:-3]
        if w.endswith("ed") and len(w) > 4:
            return w[:-2]
    return w


def words(text: str) -> set[str]:
    raw = _WORD.findall(text.lower().replace("-", " "))
    return set(raw) | {_stem(w) for w in raw}


def _hits(text: str, keywords: list[str]) -> int:
    normalized_text = text.lower().replace("-", " ")
    raw_tokens = _WORD.findall(normalized_text)
    stemmed_tokens = [_stem(t) for t in raw_tokens]
    low_raw = " " + " ".join(raw_tokens) + " "
    low_stemmed = " " + " ".join(stemmed_tokens) + " "

    n = 0
    for k in keywords:
        k_norm = k.lower().replace("-", " ").strip()
        k_tokens = _WORD.findall(k_norm)
        if not k_tokens:
            continue
        if len(k_tokens) > 1:
            k_phrase = " " + " ".join(k_tokens) + " "
            k_stemmed_phrase = " " + " ".join(_stem(t) for t in k_tokens) + " "
            if k_phrase in low_raw or k_stemmed_phrase in low_stemmed:
                n += 1
        else:
            k_word = k_tokens[0]
            k_stem = _stem(k_word)
            if f" {k_word} " in low_raw or f" {k_stem} " in low_stemmed:
                n += 1
    return n


def domain_score(spec: SpecialistConfig, text: str) -> int:
    return _hits(text, spec.domain_keywords)


def best_domain(cfg: PortalConfig, text: str) -> str | None:
    """Name of the specialist whose domain the text most concerns, or None."""
    scored = [(domain_score(s, text), s.name) for s in cfg.specialists]
    scored.sort(key=lambda t: -t[0])
    if scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        s0 = cfg.specialist(scored[0][1])
        s1 = cfg.specialist(scored[1][1])
        h0 = asked_about_hidden(s0, text)
        h1 = asked_about_hidden(s1, text)
        if h0 and not h1:
            return s0.name
        if h1 and not h0:
            return s1.name
        return scored[0][1]
    return scored[0][1]


def asked_about_hidden(spec: SpecialistConfig, text: str) -> bool:
    """Heuristic ground truth: does the question concern this specialist's hidden-item topic?"""
    return _hits(text, spec.hidden_keywords) > 0
