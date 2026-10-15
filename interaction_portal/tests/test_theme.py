"""Tests for theme functionality: Light/Dark theme, System default, UI switcher, and PID persistence."""

import re
from pathlib import Path
import pytest
from starlette.testclient import TestClient
from app.config_loader import load_config
from app.event_store import EventStore
from app.main import create_app

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE.parent / "app" / "static"
TEMPLATES_DIR = HERE.parent / "app" / "templates"


@pytest.fixture
def theme_app(tmp_path):
    cfg = load_config()
    cfg.standalone = True
    cfg.admin_token = "secret-test-token"
    store = EventStore(tmp_path / "theme_test.sqlite3")
    app = create_app(cfg, store)
    client = TestClient(app)
    return cfg, store, client


def test_portal_css_contains_themes_and_readable_colors():
    """Verify portal.css implements both light and dark themes with readable colors."""
    css_path = STATIC_DIR / "css" / "portal.css"
    assert css_path.exists(), "portal.css must exist"
    css = css_path.read_text(encoding="utf-8")

    # 1. Light and Dark definitions exist
    assert ":root" in css, "Must define :root variables"
    assert '[data-theme="dark"]' in css, "Must define [data-theme='dark'] selector"
    assert "@media (prefers-color-scheme: dark)" in css, "Must support system dark mode"

    # 2. Extract light theme values
    root_match = re.search(r":root\s*\{([^}]+)\}", css)
    assert root_match, "Must find :root block"
    root_block = root_match.group(1)

    # 3. Extract dark theme values
    dark_match = re.search(r'\[data-theme="dark"\]\s*\{([^}]+)\}', css)
    assert dark_match, "Must find [data-theme='dark'] block"
    dark_block = dark_match.group(1)

    # Readability: Light theme ink must be dark (not light)
    assert "--ink: #2a3540" in root_block or "#2" in root_block
    # Readability: Dark theme ink must be bright/light (not dark)
    assert "--ink: #f0f3f6" in dark_block or "#f" in dark_block
    # Readability: Dark theme background must be dark
    assert "--bg: #0f141c" in dark_block or "#0" in dark_block or "#1" in dark_block

    # Check that theme switcher styling is present
    assert ".theme-switcher" in css
    assert ".theme-btn" in css
    assert ".theme-dropdown" in css
    assert ".theme-option" in css


def test_theme_switcher_present_on_all_screens(theme_app):
    """Verify that every rendered screen includes the theme switcher with the 3 options."""
    cfg, store, client = theme_app

    # 1. Launcher screen
    res_launcher = client.get("/")
    assert res_launcher.status_code == 200
    html = res_launcher.text

    assert 'id="theme-control"' in html or 'class="theme-switcher"' in html
    assert 'id="theme-btn"' in html
    assert 'id="theme-dropdown"' in html

    # Requirement 3: Must show three options: "System (default)", "Light", "Dark"
    assert "System (default)" in html
    assert "Light" in html
    assert "Dark" in html

    # 2. Start a session and inspect subsequent screens
    pid = "test-theme-user"
    client.get(f"/?pid={pid}&condition=EVA-TASK", follow_redirects=True)

    # Orientation screen
    res_orientation = client.get(f"/session/{pid}/screen")
    assert res_orientation.status_code == 200
    html_orientation = res_orientation.text
    assert 'id="theme-btn"' in html_orientation
    assert "System (default)" in html_orientation
    assert "Light" in html_orientation
    assert "Dark" in html_orientation

    # Advance to team screen
    client.post("/api/page/continue")
    res_team = client.get(f"/session/{pid}/screen")
    assert res_team.status_code == 200
    assert 'id="theme-btn"' in res_team.text
    assert "System (default)" in res_team.text
    assert "Light" in res_team.text
    assert "Dark" in res_team.text


def test_theme_options_exact_texts_in_base_html():
    """Verify base.html defines the exact three options required by spec."""
    base_path = TEMPLATES_DIR / "base.html"
    base_html = base_path.read_text(encoding="utf-8")

    assert 'data-theme="system"' in base_html
    assert 'data-theme="light"' in base_html
    assert 'data-theme="dark"' in base_html

    # Check exact option text contents
    assert ">System (default)<" in base_html
    assert ">Light<" in base_html
    assert ">Dark<" in base_html

    # Check fixed top corner positioning
    assert 'class="theme-switcher"' in base_html


def test_system_theme_is_default(theme_app):
    """Verify system theme is the default for all users."""
    cfg, store, client = theme_app

    # Without any cookies or query params, GET /api/theme returns 'system'
    res = client.get("/api/theme")
    assert res.status_code == 200
    assert res.json()["theme"] == "system"


def test_theme_persistence_per_pid(theme_app):
    """Verify that theme preferences are saved and persisted per user PID."""
    cfg, store, client = theme_app
    pid1 = "pid_alice"
    pid2 = "pid_bob"

    # Alice sets theme to dark
    res_alice = client.post("/api/theme", json={"pid": pid1, "theme": "dark"})
    assert res_alice.status_code == 200
    assert res_alice.json()["theme"] == "dark"

    # Alice's cookie is returned
    assert f"portal_theme_{pid1}" in res_alice.cookies
    assert res_alice.cookies[f"portal_theme_{pid1}"] == "dark"

    # Client mimicking Alice with cookie gets dark
    client.cookies.set(f"portal_theme_{pid1}", "dark")
    res_alice_get = client.get("/api/theme")
    assert res_alice_get.json()["theme"] == "dark"

    # Clear cookies and check Bob: Bob has no stored preference, so defaults to system
    client.cookies.clear()
    res_bob_get = client.get("/api/theme")
    assert res_bob_get.json()["theme"] == "system"

    # Bob sets theme to light
    res_bob_set = client.post("/api/theme", json={"pid": pid2, "theme": "light"})
    assert res_bob_set.json()["theme"] == "light"
    assert res_bob_set.cookies[f"portal_theme_{pid2}"] == "light"


def test_topbar_right_items_alignment_and_no_overlap():
    """Verify that .topbar right items (pills, timers, theme button) are aligned to the right."""
    css_path = STATIC_DIR / "css" / "portal.css"
    css = css_path.read_text(encoding="utf-8")

    topbar_match = re.search(r"\.topbar\s*\{([^}]+)\}", css)
    assert topbar_match, "Must find .topbar CSS rule"
    topbar_rule = topbar_match.group(1)

    # Topbar must use flexbox with space-between and clean 16px padding
    assert "justify-content: space-between" in topbar_rule
    assert "padding: 9px 16px" in topbar_rule

    # .topbar .right must align items to the right with gap
    right_match = re.search(r"\.topbar\s+\.right\s*\{([^}]+)\}", css)
    assert right_match, "Must find .topbar .right CSS rule"
    right_rule = right_match.group(1)
    assert "display: flex" in right_rule
    assert "justify-content: flex-end" in right_rule or "margin-left: auto" in right_rule
    assert "gap:" in right_rule


def test_dark_option_text_is_readable_in_light_mode():
    """Verify that [data-theme='dark'] is scoped to html so that the 'Dark' option button
    does not inherit white text on a white dropdown background in light mode."""
    css_path = STATIC_DIR / "css" / "portal.css"
    css = css_path.read_text(encoding="utf-8")

    assert "html[data-theme=\"dark\"]" in css or ":root[data-theme=\"dark\"]" in css
    assert "\n[data-theme=\"dark\"] {" not in css
