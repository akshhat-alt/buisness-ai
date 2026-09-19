"""The WhatsApp showcase is pure HTML/CSS illustration, so these guard the
honesty of what it shows: three phones, labelled illustrative, no images
(no fabricated screenshots), reduced-motion respected, and the one
Growth-gated claim (shifts) disclosed."""

from __future__ import annotations

from business_ai.constants import STATIC_DIR


def _section() -> str:
    src = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    start = src.index('id="whatsapp-demo"')
    return src[start : src.index('id="product"', start)]


def test_three_phone_mockups_labelled_illustrative_and_no_images():
    sec = _section()
    assert sec.count('class="bm-phone"') == 3
    assert "Illustrative conversations" in sec
    assert "<img" not in sec


def test_growth_gated_shift_claim_is_disclosed():
    assert "shift questions are part of the Pro plan" in _section()


def test_animation_respects_reduced_motion():
    css = (STATIC_DIR / "css" / "marketing.css").read_text(encoding="utf-8")
    block = css[css.index("prefers-reduced-motion: reduce) {\n  .bm-phone") :]
    assert ".bm-phone { animation: none; }" in block
    assert ".bm-wa-msg { animation: none; opacity: 1; }" in block
