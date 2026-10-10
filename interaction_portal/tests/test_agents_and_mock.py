"""Tests for SpecialistAgent, OrchestratorAgent, and MockLLM."""

import pytest
from app.config_loader import load_config
from app.agents.mock_llm import MockClient
from app.agents.specialist import SpecialistAgent, Trigger
from app.agents.orchestrator import OrchestratorAgent


@pytest.fixture
def agent_env():
    cfg = load_config()
    client = cfg.clients["C1"]
    llm = MockClient(cfg)
    return cfg, client, llm


def test_specialist_reply_structured_output(agent_env):
    cfg, client, llm = agent_env
    nia_cfg = cfg.specialist("Nia")
    nia = SpecialistAgent(cfg, nia_cfg, client, llm)

    # In-domain general question
    res = nia.reply([], Trigger(kind="mention", sender="Participant", text="Who is the target audience?"))
    assert res.output.reply_text != ""
    assert res.output.disclosed_hidden_items == []
    assert res.output.declined is False

    # Hidden item explicit question
    res_hid = nia.reply([], Trigger(kind="mention", sender="Participant", text="What unstated priority does the client care about?"))
    assert "C1-H1" in res_hid.output.disclosed_hidden_items
    assert res_hid.asked_for is True


def test_specialist_cross_domain_redirect(agent_env):
    cfg, client, llm = agent_env
    nia_cfg = cfg.specialist("Nia")
    nia = SpecialistAgent(cfg, nia_cfg, client, llm)

    # Asking Nia about Mira's domain (budget)
    res = nia.reply([], Trigger(kind="mention", sender="Participant", text="What is the production budget and deadline?"))
    assert res.output.declined is True
    assert res.output.redirect_to == "Mira"


def test_orchestrator_planning_burst(agent_env):
    cfg, client, llm = agent_env
    orch = OrchestratorAgent(cfg, client, llm)

    # Opening burst
    out, res = orch.plan([], kind="opening")
    assert out.draft_card is True
    assert len(out.assignments) >= 3
    targets = {a.to for a in out.assignments}
    assert "Nia" in targets
    assert "Theo" in targets
    assert "Mira" in targets


def test_orchestrator_panel_generation_and_word_budget(agent_env):
    cfg, client, llm = agent_env
    orch = OrchestratorAgent(cfg, client, llm)
    lo, hi = cfg.panel.word_budget

    levels = ["coordination", "task_focused", "developmental"]
    for level in levels:
        panel_res = orch.generate_panel(
            level=level,
            transcript=[],
            status="No specialist has contributed yet.",
            card_status="0 of 5 card fields have content.",
        )
        assert lo <= panel_res.word_count <= hi, f"Level {level} word count {panel_res.word_count} not in [{lo}, {hi}]"
        # Verify required sections are present
        req = cfg.panel.sections[level]
        for sec in req:
            assert sec in panel_res.sections_present
        # Clean plain words: no bullet points or markdown headings
        assert not panel_res.text.startswith("#")
        assert "- " not in panel_res.text
