"""Tests for Phase 3's menu -> knowledge-base sync: live_menu_evidence_items
(menu.py) and its wiring into the shared _process_question pipeline
(admin_bot.py) — closes the "stale menu price in the RAG index" gap
without new LLM tool-calling infrastructure."""

from __future__ import annotations

from business_ai.menu import live_menu_evidence_items


def _add_menu_item(services, tenant_id: str, *, name: str, price_inr: int):
    return services.menu_store.create_item(tenant_id=tenant_id, name=name, price_inr=price_inr, category="mains")


def test_live_menu_evidence_matches_item_named_in_query(services):
    _add_menu_item(services, "t1", name="Paneer Tikka", price_inr=250)
    items = live_menu_evidence_items(tenant_id="t1", query="How much is the Paneer Tikka?", menu_store=services.menu_store)
    assert len(items) == 1
    assert items[0].confidence == 1.0
    assert items[0].citation.label == "Your current menu"
    assert "Paneer Tikka" in items[0].text
    assert "250" in items[0].text


def test_live_menu_evidence_is_case_insensitive(services):
    _add_menu_item(services, "t1", name="Paneer Tikka", price_inr=250)
    items = live_menu_evidence_items(tenant_id="t1", query="how much is paneer tikka", menu_store=services.menu_store)
    assert len(items) == 1


def test_live_menu_evidence_empty_for_no_menu_items(services):
    items = live_menu_evidence_items(tenant_id="t1", query="What are your hours?", menu_store=services.menu_store)
    assert items == []


def test_live_menu_evidence_empty_when_query_does_not_mention_a_menu_item(services):
    _add_menu_item(services, "t1", name="Paneer Tikka", price_inr=250)
    items = live_menu_evidence_items(tenant_id="t1", query="What are your hours?", menu_store=services.menu_store)
    assert items == []


def test_live_menu_evidence_ignores_inactive_items(services):
    item = _add_menu_item(services, "t1", name="Discontinued Dish", price_inr=100)
    services.menu_store.update_item("t1", item.menu_item_id, active=False)
    items = live_menu_evidence_items(tenant_id="t1", query="how much is discontinued dish", menu_store=services.menu_store)
    assert items == []


def test_live_menu_evidence_is_tenant_scoped(services):
    _add_menu_item(services, "t1", name="Paneer Tikka", price_inr=250)
    items = live_menu_evidence_items(tenant_id="t2", query="how much is paneer tikka", menu_store=services.menu_store)
    assert items == []


def test_ask_route_answers_menu_price_question_with_no_knowledge_ingested(client, owner_session, activate_tenant, services):
    """The real end-to-end case: a tenant with ZERO knowledge sources
    (so RAG alone would abstain) but a menu item logged, asked about
    that exact item's price, gets a real grounded answer, not an
    abstention — because the live menu evidence clears the gate."""
    headers, tenant_id = owner_session
    _add_menu_item(services, tenant_id, name="Paneer Tikka", price_inr=250)
    # Activate without any knowledge source — MenuStore items alone are
    # enough evidence, so the activation blocker is the only thing that
    # would otherwise require a knowledge source; bypass it directly
    # here since we're testing the evidence pipeline, not activation.
    from business_ai.tenant import TenantStatus

    services.tenant_registry.update_status(tenant_id, TenantStatus.ACTIVE)

    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "How much is the Paneer Tikka?"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "answered"
    assert any(c["label"] == "Your current menu" for c in data["citations_used"])


def test_ask_route_still_abstains_for_unrelated_question_with_no_knowledge(client, owner_session, services):
    """Confirms the injection is targeted, not a blanket bypass of
    abstention for menu-having tenants."""
    headers, tenant_id = owner_session
    _add_menu_item(services, tenant_id, name="Paneer Tikka", price_inr=250)
    from business_ai.tenant import TenantStatus

    services.tenant_registry.update_status(tenant_id, TenantStatus.ACTIVE)

    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "Do you offer home delivery to another city?"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "insufficient_evidence"
