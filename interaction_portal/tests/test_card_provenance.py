"""Tests for Card Provenance and role rules (passive, evaluative, generative, none)."""

import pytest
from app.simulation.card_provenance import Card, CardError


FIELDS = ["audience_angle", "creative_concept", "compliance_note"]


def test_passive_role_card():
    card = Card(FIELDS, role="passive")
    span, evt = card.add_ai_text("audience_angle", "Young professionals", "Nia")
    assert span.author == "Nia"
    assert span.status == "proposed"
    assert evt["action"] == "created"

    # Evaluative action on passive card must raise CardError
    with pytest.raises(CardError, match="this role cannot keep, cut or send back"):
        card.evaluate(span.span_id, "keep")

    # Typing directly on passive card must raise CardError
    with pytest.raises(CardError, match="this role cannot type on the card"):
        card.set_field_text("audience_angle", "Overwritten text")

    # can_submit returns True in passive
    can_sub, _ = card.can_submit()
    assert can_sub is True


def test_evaluative_role_card():
    card = Card(FIELDS, role="evaluative", send_back_needs_reason=True)
    span1, _ = card.add_ai_text("audience_angle", "Initial draft by Nia", "Nia")
    span2, _ = card.add_ai_text("creative_concept", "Initial draft by Theo", "Theo")
    span3, _ = card.add_ai_text("compliance_note", "Initial draft by Rhys", "Rhys")

    # Keep span1
    card.evaluate(span1.span_id, "keep")
    assert span1.status == "kept"

    # Cut span2
    card.evaluate(span2.span_id, "cut")
    assert span2.status == "cut"

    # Send back span3 requires a reason
    with pytest.raises(CardError, match="reason is required"):
        card.evaluate(span3.span_id, "send_back", reason="")

    card.evaluate(span3.span_id, "send_back", reason="Too restrictive")
    assert span3.status == "sent_back"

    # Text included on evaluative card must only be kept spans
    full = card.full_text()
    assert full["audience_angle"] == "Initial draft by Nia"
    assert full["creative_concept"] == ""  # cut
    assert full["compliance_note"] == ""   # sent back


def test_generative_role_card_and_typed_share():
    card = Card(FIELDS, role="generative", min_typed_share=0.25)
    # AI proposals in generative become suggestions
    span, evt = card.add_ai_text("audience_angle", "AI suggestion text", "Nia")
    assert span.status == "suggested"
    assert len(card.suggestions["audience_angle"]) == 1
    assert len(card.fields["audience_angle"]) == 0

    # User types text
    card.set_field_text("audience_angle", "User typed words for the audience.")
    full = card.full_text()
    assert full["audience_angle"] == "User typed words for the audience."

    # Typed share should be 1.0 (100%)
    share = card.typed_share()
    assert share == 1.0
    can_sub, _ = card.can_submit()
    assert can_sub is True


def test_serialization_and_deserialization():
    card = Card(FIELDS, role="evaluative")
    card.add_ai_text("audience_angle", "Draft text", "Nia")
    data = card.to_json(full=True)

    loaded = Card.from_json(data, send_back_needs_reason=True, min_typed_share=0.25)
    assert loaded.role == "evaluative"
    assert len(loaded.fields["audience_angle"]) == 1
    assert loaded.fields["audience_angle"][0].author == "Nia"
    assert loaded.fields["audience_angle"][0].text == "Draft text"
