"""
Deterministic detection of undisclosed private / hidden client items.

Used by:
- SpecialistAgent pre-send guardrail (blocks leaks before sending to participant)
- Orchestrator panel generator (ensures panel never states private client facts)
- Automated Harness A and B verification
"""

from __future__ import annotations

import re
from typing import Iterable

_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "of", "in", "on", "at",
    "to", "for", "with", "by", "about", "as", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "this", "that",
    "these", "those", "it", "its", "they", "them", "their", "we", "us", "our",
    "you", "your", "he", "him", "his", "she", "her", "i", "me", "my", "from",
    "not", "no", "can", "could", "will", "would", "shall", "should", "may", "might",
    "must", "there", "here", "all", "any", "both", "each", "few", "more", "most",
    "other", "some", "such", "than", "too", "very", "only", "also", "into", "over"
}

_WORD = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _extract_distinctive_terms(passage: str) -> list[str]:
    """Find specific numbers, quotes, or key multi-word phrases in the passage."""
    terms = []
    # Numbers and amounts e.g. 9,000, 6,000, 30 seconds, 10 working days
    nums = re.findall(r"\b(?:\$|€|£|eur|gbp|usd)?\s*\d+(?:[,\.]\d+)?(?:\s*(?:seconds?|days?|months?|years?|percent|%|words?|teas?|people))?\b", passage, re.I)
    terms.extend(t.lower().strip() for t in nums if len(t.strip()) > 1)

    # Quoted terms e.g. 'detox', 'cleanse', 'boosts immunity'
    quotes = re.findall(r"['\"]([^'\"]{3,})['\"]", passage)
    terms.extend(q.lower().strip() for q in quotes)
    return terms


def detect_disclosure(reply: str, passages: dict[str, str], threshold: float = 0.5) -> list[str]:
    """Check if `reply` discloses any hidden items from `passages` (item_id -> passage_text).
    
    Returns a list of item IDs that were detected as disclosed.
    """
    if not reply or not passages:
        return []

    reply_tokens = _tokenize(reply)
    if not reply_tokens:
        return []

    reply_content_tokens = [t for t in reply_tokens if t not in _STOPWORDS]
    reply_3grams = _ngrams(reply_tokens, 3)
    reply_4grams = _ngrams(reply_tokens, 4)
    reply_lower = " " + " ".join(reply_tokens) + " "

    disclosed = []

    for item_id, raw_passage in passages.items():
        # Remove markdown/comment markers
        passage = re.sub(r"<!--.*?-->", "", raw_passage).strip()
        p_tokens = _tokenize(passage)
        if not p_tokens:
            continue

        # 1. Check for specific quoted constraints or numbers
        distinctive = _extract_distinctive_terms(passage)
        hit_distinctive = False
        for term in distinctive:
            term_clean = " ".join(_tokenize(term))
            if term_clean and f" {term_clean} " in reply_lower:
                hit_distinctive = True
                break

        # 2. Check 4-gram overlap
        p_4grams = _ngrams(p_tokens, 4)
        overlap_4 = p_4grams & reply_4grams

        # 3. Check 3-grams with content words
        p_3grams = _ngrams(p_tokens, 3)
        overlap_3 = p_3grams & reply_3grams
        # Filter 3-grams that are just stopwords
        content_3grams = {g for g in overlap_3 if any(w not in _STOPWORDS for w in g)}

        # 4. Content words overlap
        p_content_tokens = set(t for t in p_tokens if t not in _STOPWORDS)
        if p_content_tokens:
            common_content = p_content_tokens & set(reply_content_tokens)
            content_ratio = len(common_content) / len(p_content_tokens)
        else:
            common_content = set()
            content_ratio = 0.0

        # Detection criteria:
        # - Any 4-gram matching
        # - At least two distinctive 3-grams matching
        # - Distinctive term (number/quoted constraint) AND significant content overlap
        # - High overall content token ratio
        if (
            len(overlap_4) >= 1
            or len(content_3grams) >= 2
            or (hit_distinctive and len(common_content) >= 3)
            or content_ratio >= threshold
        ):
            disclosed.append(item_id)

    return disclosed
