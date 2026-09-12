"""Unit test for AnalyticsStore.session_ids_with_buying_intent — the
join key app.py uses against LeadStore to detect missed opportunities
(a lead who showed real buying intent but never booked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from business_ai.analytics import AnalyticsStore


@pytest.fixture()
def store(tmp_path: Path) -> AnalyticsStore:
    return AnalyticsStore(tmp_path / "analytics.db")


def _log(store, *, tenant_id="t1", session_id, shows_buying_intent=False):
    store.log_turn(
        tenant_id=tenant_id, session_id=session_id, query="q", answer_status="answered",
        shows_buying_intent=shows_buying_intent, suggested_handoff=False, shows_dissatisfaction=False, channel="whatsapp",
    )


def test_session_ids_with_buying_intent_only_includes_flagged_sessions(store):
    _log(store, session_id="s1", shows_buying_intent=True)
    _log(store, session_id="s2", shows_buying_intent=False)
    assert store.session_ids_with_buying_intent("t1") == {"s1"}


def test_session_ids_with_buying_intent_is_tenant_scoped(store):
    _log(store, tenant_id="t1", session_id="s1", shows_buying_intent=True)
    _log(store, tenant_id="t2", session_id="s2", shows_buying_intent=True)
    assert store.session_ids_with_buying_intent("t1") == {"s1"}


def test_session_ids_with_buying_intent_dedupes_multiple_turns_same_session(store):
    _log(store, session_id="s1", shows_buying_intent=True)
    _log(store, session_id="s1", shows_buying_intent=True)
    assert store.session_ids_with_buying_intent("t1") == {"s1"}


def test_session_ids_with_buying_intent_windowed(store):
    _log(store, session_id="s1", shows_buying_intent=True)
    assert store.session_ids_with_buying_intent("t1", since_iso="2099-01-01T00:00:00Z") == set()
    assert store.session_ids_with_buying_intent("t1", since_iso="2000-01-01T00:00:00Z") == {"s1"}
