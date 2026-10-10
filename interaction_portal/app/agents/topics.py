"""Keyword helpers shared by the offline mock model, the event log and the test harnesses."""

from __future__ import annotations

import re

from ..config_loader import PortalConfig, SpecialistConfig

_WORD = re.compile(r"[a-z']+")


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _hits(text: str, keywords: list[str]) -> int:
    low = " " + " ".join(_WORD.findall(text.lower())) + " "
    n = 0
    for k in keywords:
        if f" {k.lower()} " in low:
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
        # ambiguous between two: prefer the hidden-topic owner if any, else no single owner
        return scored[0][1]
    return scored[0][1]


def asked_about_hidden(spec: SpecialistConfig, text: str) -> bool:
    """Heuristic ground truth: does the question concern this specialist's hidden-item topic?"""
    return _hits(text, spec.hidden_keywords) > 0
