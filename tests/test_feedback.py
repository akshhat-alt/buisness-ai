"""Unit tests for FeedbackStore: tenant-scoped storage and theme
aggregation for classified employee feedback (see generation.py's
classify_feedback_sentiment and app.py's "feedback <text>" command)."""

from __future__ import annotations

from pathlib import Path

import pytest

from business_ai.feedback import FeedbackStore


@pytest.fixture()
def store(tmp_path: Path) -> FeedbackStore:
    return FeedbackStore(tmp_path / "feedback.db")


def _record(store, tenant_id="t1", **overrides):
    defaults = dict(
        tenant_id=tenant_id, employee_id="emp_1", raw_text="the software keeps logging me out",
        sentiment="negative", theme="software_or_tools", urgency="medium",
        root_cause_hint="session timeout", suggested_action="check session config",
    )
    defaults.update(overrides)
    return store.record(**defaults)


def test_record_and_get(store):
    item = _record(store)
    fetched = store.get("t1", item.feedback_id)
    assert fetched.raw_text == "the software keeps logging me out"
    assert fetched.resolved is False


def test_get_is_tenant_scoped(store):
    item = _record(store, tenant_id="t1")
    assert store.get("t2", item.feedback_id) is None


def test_invalid_sentiment_or_urgency_rejected(store):
    with pytest.raises(ValueError):
        _record(store, sentiment="furious")
    with pytest.raises(ValueError):
        _record(store, urgency="asap")


def test_empty_text_rejected(store):
    with pytest.raises(ValueError):
        _record(store, raw_text="   ")


def test_list_for_tenant_filters(store):
    _record(store, tenant_id="t1", theme="software_or_tools", sentiment="negative")
    _record(store, tenant_id="t1", theme="scheduling_or_shifts", sentiment="neutral")
    _record(store, tenant_id="t2", theme="software_or_tools", sentiment="negative")

    all_t1 = store.list_for_tenant("t1")
    assert len(all_t1) == 2

    software_only = store.list_for_tenant("t1", theme="software_or_tools")
    assert len(software_only) == 1

    negative_only = store.list_for_tenant("t1", sentiment="negative")
    assert len(negative_only) == 1


def test_summarize_by_theme_counts_and_negative_count(store):
    _record(store, tenant_id="t1", theme="software_or_tools", sentiment="negative")
    _record(store, tenant_id="t1", theme="software_or_tools", sentiment="negative")
    _record(store, tenant_id="t1", theme="software_or_tools", sentiment="positive")
    _record(store, tenant_id="t1", theme="scheduling_or_shifts", sentiment="neutral")

    summaries = {s.theme: s for s in store.summarize_by_theme("t1")}
    assert summaries["software_or_tools"].count == 3
    assert summaries["software_or_tools"].negative_count == 2
    assert summaries["scheduling_or_shifts"].count == 1
    assert summaries["scheduling_or_shifts"].negative_count == 0
    # Sorted by count descending — the most-reported theme comes first.
    ordered = store.summarize_by_theme("t1")
    assert ordered[0].theme == "software_or_tools"


def test_summarize_by_theme_is_tenant_scoped(store):
    _record(store, tenant_id="t1", theme="software_or_tools")
    _record(store, tenant_id="t2", theme="software_or_tools")
    assert store.summarize_by_theme("t1")[0].count == 1


def test_list_unresolved_negative(store):
    negative = _record(store, tenant_id="t1", sentiment="negative")
    _record(store, tenant_id="t1", sentiment="positive")
    unresolved = store.list_unresolved_negative("t1")
    assert len(unresolved) == 1
    assert unresolved[0].feedback_id == negative.feedback_id

    store.mark_resolved("t1", negative.feedback_id)
    assert store.list_unresolved_negative("t1") == []


def test_mark_resolved_is_tenant_scoped(store):
    item = _record(store, tenant_id="t1")
    assert store.mark_resolved("t2", item.feedback_id) is None
    assert store.get("t1", item.feedback_id).resolved is False
