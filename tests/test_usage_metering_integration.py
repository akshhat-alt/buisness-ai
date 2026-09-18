"""Integration tests for Phase 1's usage-metering increments: the shared
/api/ask (web) pipeline increments "ai_messages", and a successful
WhatsApp reply additionally increments "whatsapp_messages" — see the
two call sites in routers/admin_bot.py (_process_question and the
webhook handler's send_text success path)."""

from __future__ import annotations

from tests.test_admin_bot import _setup_tenant_with_owner_and_staff, _signed_post, _wa_payload
from tests.test_whatsapp import client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


def test_web_ask_increments_ai_messages(client, owner_session, activate_tenant, services):
    headers, tenant_id = owner_session
    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text
    activate_tenant(tenant_id)
    assert services.usage_meter_store.get_usage(tenant_id=tenant_id) == {}

    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "What are your hours?"})
    assert r.status_code == 200, r.text

    assert services.usage_meter_store.get_usage(tenant_id=tenant_id) == {"ai_messages": 1}

    client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "Do you deliver?"})
    assert services.usage_meter_store.get_usage(tenant_id=tenant_id) == {"ai_messages": 2}


def test_whatsapp_reply_increments_both_ai_and_whatsapp_messages(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    assert services_wa.usage_meter_store.get_usage(tenant_id=tenant_id) == {}

    # A real, non-roster customer number — a roster member's own number
    # (Ravi's, or OWNER_WA) is routed to the internal admin-bot command
    # parser instead of the customer-facing _process_question pipeline.
    payload = _wa_payload(phone_number_id="PNID_1", wa_id="917000011122", message_id="wamid.usage1", text="What are your hours?")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text

    usage = services_wa.usage_meter_store.get_usage(tenant_id=tenant_id)
    assert usage.get("ai_messages") == 1
    assert usage.get("whatsapp_messages") == 1
