"""Regression and UI guards for the verified launch fixes and dashboard redesign.

Asserts on static/dashboard.html, static/onboarding.html, and static/index.html:
- All 12 tables in dashboard.html are wrapped in ba-table-responsive.
- Setup checklist has 4 items, progress indicator, and copy-link tip (no fake tick).
- Mobile table cards data-label attributes are rendered on leads, tasks, automation tables.
- Forbidden legacy strings replaced with plain language.
- WhatsApp help request and paste text ingestion elements are present in both dashboard and onboarding.
"""

from __future__ import annotations

import re
from business_ai.constants import STATIC_DIR


def _read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_all_12_dashboard_tables_are_wrapped_in_responsive_containers():
    src = _read_static("dashboard.html")
    expected_tables = [
        "sources-table",
        "leads-table",
        "employees-table",
        "tasks-table",
        "shifts-table",
        "bm-processes-table",
        "automation-rules-table",
        "automation-runs-table",
        "evolution-versions-table",
        "bi-scorecard-table",
        "ri-menu-table",
        "ri-supplier-table",
    ]
    for tbl_id in expected_tables:
        assert f'id="{tbl_id}"' in src, f"Missing table #{tbl_id}"
        # Assert table is preceded by ba-table-responsive wrapper
        pattern = rf'<div class="ba-table-responsive"[^>]*>[\s\S]*?<table[^>]*id="{tbl_id}"'
        assert re.search(pattern, src), f"Table #{tbl_id} is not wrapped in a .ba-table-responsive div"


def test_setup_checklist_elements_and_copy_tip():
    src = _read_static("dashboard.html")
    assert 'id="setup-checklist-card"' in src
    assert 'id="setup-checklist-progress"' in src
    assert 'id="chk-item-activate"' in src
    assert 'id="chk-item-knowledge"' in src
    assert 'id="chk-item-whatsapp"' in src
    assert 'id="chk-item-automation"' in src
    assert 'id="chk-open-chat"' in src
    # Check that "Turn on automation — start with 3 ready-made recipes" is present
    assert "Turn on automation — start with 3 ready-made recipes" in src
    # Check that "Share your chat link" is a tip and not a checklist item
    assert "setup-checklist-tip" in src
    assert "copyWebChatLink()" in src


def test_mobile_table_card_data_labels():
    src = _read_static("dashboard.html")
    # Leads table td labels
    assert 'data-label="Channel"' in src
    assert 'data-label="Name"' in src
    assert 'data-label="Stage"' in src
    assert 'data-label="Phone"' in src
    assert 'data-label="Email"' in src
    assert 'data-label="Message"' in src
    assert 'data-label="Appointment"' in src
    assert 'data-label="Actions"' in src

    # Tasks table td labels
    assert 'data-label="Task"' in src
    assert 'data-label="Assigned to"' in src
    assert 'data-label="Due"' in src
    assert 'data-label="Status"' in src

    # Automation rules table td labels
    assert 'data-label="Trigger"' in src
    assert 'data-label="Action"' in src


def test_plain_language_replacements_in_dashboard():
    src = _read_static("dashboard.html")
    # "Knowledge Gaps" heading replaced with "Questions to Answer"
    assert "<h2>Questions to Answer</h2>" in src
    # "Self-Evolution" nav item replaced with "Assistant Improvements"
    assert "Assistant Improvements" in src
    assert "Assistant Improvements Engine" in src
    # "Business Info" nav item instead of bare Knowledge
    assert "Business Info" in src
    # "Needs you" calm executive section
    assert "Needs you" in src
    assert "Bizistic handled (last 7 days)" in src
    assert "✓ All clear — nothing needs your attention right now." in src


def test_whatsapp_concierge_and_paste_text_elements():
    dash = _read_static("dashboard.html")
    onboard = _read_static("onboarding.html")

    # Dashboard WhatsApp help request & paste text
    assert 'id="dash-wa-help-phone"' in dash
    assert "requestDashWhatsappHelp()" in dash
    assert 'id="paste-text-title"' in dash
    assert 'id="paste-text-content"' in dash
    assert "ingestPastedText()" in dash

    # Onboarding WhatsApp help request & paste text
    assert 'id="wiz-help-phone"' in onboard
    assert "requestWizWhatsappHelp()" in onboard
    assert 'id="wiz-paste-title"' in onboard
    assert 'id="wiz-paste-text"' in onboard
    assert "wizIngestText()" in onboard


def test_index_plain_language_updates():
    idx = _read_static("index.html")
    assert "3 customer questions to answer" in idx
    assert "Questions I couldn't answer" in idx
    assert "Assistant improvements" in idx
    assert "Every unanswered question becomes a one-click business info fix" in idx

