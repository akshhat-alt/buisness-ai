"""Guards the 5-persona showcase and the polish-motion pass: every persona
tab has matching data (a tab with no data would throw on click), and all new
animation is switched off under prefers-reduced-motion."""

from __future__ import annotations

import re

from business_ai.constants import STATIC_DIR


def _index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_five_persona_tabs_each_have_data():
    src = _index()
    tabs = re.findall(r'data-assistant="([a-z]+)"', src)
    assert tabs == ["restaurant", "salon", "retail", "fitness", "services"]
    data_start = src.index("const assistantPersonas")
    data = src[data_start : src.index("function renderAssistantPanel", data_start)]
    for key in tabs:
        assert re.search(rf"\n      {key}: \{{", data), key


def test_services_persona_no_longer_replies_with_a_complaint():
    src = _index()
    services = src[src.index("      services: {") : src.index("function renderAssistantPanel")]
    assert "nobody's called me back" not in services
    assert "Kal shaam ko appointment" in services


def test_new_motion_disabled_for_reduced_motion():
    css = (STATIC_DIR / "css" / "marketing.css").read_text(encoding="utf-8")
    block = css[css.rindex("@media (prefers-reduced-motion: reduce)") :]
    assert ".bm-hero-glow { animation: none; }" in block
    assert ".bm-reveal, .bm-reveal-stagger > * { opacity: 1; transform: none; transition: none; }" in block


def test_nav_scroll_shadow_wired():
    assert "classList.toggle('scrolled'" in _index()
def test_persona_examples_are_labelled_as_examples():
    assert 'prices, timings and offers shown are made up' in _index()
