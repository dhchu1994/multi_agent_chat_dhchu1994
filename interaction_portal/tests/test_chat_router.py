"""Tests for Chat Router message parsing, suggestions, and folded block grouping."""

import pytest
from app.simulation.chat_router import parse_message, group_blocks, block_label


NAMES = ["Nia", "Theo", "Rhys", "Mira", "Orchestrator"]


def test_direct_mentions():
    route = parse_message("@Nia what do you think?", NAMES)
    assert route.kind == "directed"
    assert route.addressees == ["Nia"]
    assert route.mention_used is True

    # Multiple mentions
    route2 = parse_message("@Nia @Theo please coordinate", NAMES)
    assert route2.kind == "directed"
    assert route2.addressees == ["Nia", "Theo"]


def test_broadcast_messages():
    # Explicit broadcast alias
    route = parse_message("@team let's begin", NAMES)
    assert route.kind == "broadcast"
    assert route.addressees == []

    route2 = parse_message("@everyone what is the plan?", NAMES)
    assert route2.kind == "broadcast"

    # No mention -> broadcast
    route3 = parse_message("Hello all", NAMES)
    assert route3.kind == "broadcast"
    assert route3.addressees == []
    assert route3.mention_used is False


def test_typo_handling_and_suggestions():
    # Typo: @Niaa
    route = parse_message("@Niaa what is the audience?", NAMES)
    assert route.kind == "error"
    assert route.unknown == "Niaa"
    assert "Nia" in route.suggestions

    # Stray typo in middle of message
    route2 = parse_message("Can someone ask @Theoo about visuals?", NAMES)
    assert route2.kind == "error"
    assert route2.unknown == "Theoo"
    assert "Theo" in route2.suggestions


def test_folded_blocks_grouping():
    messages = [
        {"id": "m1", "sender": "Participant", "text": "Hello", "block_id": None, "ts": "2026-10-08T10:00:00Z"},
        {"id": "m2", "sender": "Orchestrator", "text": "Assigning to Nia", "block_id": "blk_01", "ts": "2026-10-08T10:00:01Z", "to": ["Nia"]},
        {"id": "m3", "sender": "Nia", "text": "Understood", "block_id": "blk_01", "ts": "2026-10-08T10:00:02Z", "to": ["Orchestrator"]},
        {"id": "m4", "sender": "Participant", "text": "Thanks", "block_id": None, "ts": "2026-10-08T10:00:03Z"},
    ]
    items = group_blocks(messages)
    assert len(items) == 3
    assert items[0]["type"] == "message"
    assert items[1]["type"] == "block"
    assert items[1]["block_id"] == "blk_01"
    assert len(items[1]["messages"]) == 2
    assert items[2]["type"] == "message"

    label = block_label(items[1]["messages"], "Orchestrator")
    assert label["block_id"] == "blk_01"
    assert label["involved"] == ["Nia"]
    assert label["count"] == 2
