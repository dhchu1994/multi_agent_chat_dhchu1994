"""Tests for config loading and condition definitions."""

import pytest
from app.config_loader import load_config, PortalConfig


def test_load_config_valid():
    cfg = load_config()
    assert isinstance(cfg, PortalConfig)
    assert cfg.config_version == "2.0.0"
    assert len(cfg.specialists) == 4
    assert set(cfg.specialist_names) == {"Nia", "Theo", "Rhys", "Mira"}
    assert cfg.orchestrator_name == "Orchestrator"


def test_ten_conditions():
    cfg = load_config()
    expected_conditions = {
        "PAS-COORD": ("passive", "coordination", True),
        "PAS-TASK": ("passive", "task_focused", True),
        "PAS-DEV": ("passive", "developmental", True),
        "EVA-COORD": ("evaluative", "coordination", True),
        "EVA-TASK": ("evaluative", "task_focused", True),
        "EVA-DEV": ("evaluative", "developmental", True),
        "GEN-COORD": ("generative", "coordination", True),
        "GEN-TASK": ("generative", "task_focused", True),
        "GEN-DEV": ("generative", "developmental", True),
        "NOAI": ("none", "none", False),
    }
    assert len(cfg.conditions) == 10
    for code, (role, panel_level, ai) in expected_conditions.items():
        assert code in cfg.conditions, f"Missing condition {code}"
        cond = cfg.conditions[code]
        assert cond.role == role, f"{code} role expected {role}, got {cond.role}"
        assert cond.panel_level == panel_level, f"{code} panel_level expected {panel_level}, got {cond.panel_level}"
        assert cond.ai is ai, f"{code} ai expected {ai}, got {cond.ai}"


def test_clients_and_private_materials():
    cfg = load_config()
    assert len(cfg.clients) == 7
    for cid, client in cfg.clients.items():
        assert len(client.requirements) == 3
        assert client.body.strip()
        # Each client has 4 private files (one per specialist)
        assert len(client.private) == 4
        for sname in cfg.specialist_names:
            pm = client.private[sname]
            assert len(pm.hidden_items) == 1, f"Expected 1 hidden item for {sname} in {cid}"
            hid = pm.hidden_items[0]
            assert hid in pm.hidden_passages, f"Hidden passage missing for {hid}"
            assert pm.text_plain.strip()


def test_timer_scaling():
    cfg = load_config()
    cfg.timer_scale = 1.0
    assert cfg.scaled(600) == 600.0
    cfg.timer_scale = 2.0
    assert cfg.scaled(600) == 300.0
    cfg.timer_scale = 10.0
    assert cfg.scaled(600) == 60.0


def test_text_lookup():
    cfg = load_config()
    text = cfg.text("common.continue")
    assert text == "Continue"
    with pytest.raises(KeyError):
        cfg.text("nonexistent.key.path")
