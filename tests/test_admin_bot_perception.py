"""Tests for Phase 25's Perception & Input Expansion admin-bot
integration: voice-note transcription feeding the exact same command
dispatcher as typed text, receipt-photo OCR replying with a suggested
(never auto-applied) "log purchase ..." command, and the "log review"/
"reviews" WhatsApp commands.
"""

from __future__ import annotations

from business_ai.generation import ReceiptExtraction
from tests.conftest import FakeGenerator
from tests.test_admin_bot import OWNER_WA, RAVI_WA, _add_employee, _send, _setup_tenant_with_owner_and_staff
from tests.test_whatsapp import _signed_post, client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


def _media_payload(*, phone_number_id: str, wa_id: str, message_id: str, media_type: str, media_id: str, mime_type: str, caption: str | None = None):
    media_field = {"id": media_id, "mime_type": mime_type}
    if caption is not None:
        media_field["caption"] = caption
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_TEST",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550001111", "phone_number_id": phone_number_id},
                    "contacts": [{"profile": {"name": "Owner"}, "wa_id": wa_id}],
                    "messages": [{"from": wa_id, "id": message_id, "timestamp": "1700000000", "type": media_type, media_type: media_field}],
                },
                "field": "messages",
            }],
        }],
    }


def _send_media(client_wa, *, wa_id, media_type, media_id, mime_type, message_id, caption=None):
    payload = _media_payload(
        phone_number_id="PNID_1", wa_id=wa_id, message_id=message_id, media_type=media_type,
        media_id=media_id, mime_type=mime_type, caption=caption,
    )
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ voice notes


def test_voice_note_is_transcribed_and_handled_like_typed_text(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"voice_notes_enabled": True}, headers=headers)
    services_wa.generator = lambda: FakeGenerator(voice_transcript="tasks")

    _send_media(client_wa, wa_id=OWNER_WA, media_type="audio", media_id="AUDIO_1", mime_type="audio/ogg", message_id="wamid.voice1")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "No open tasks" in reply or "task" in reply.lower()


def test_voice_note_disabled_by_default_asks_to_type_instead(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    # voice_notes_enabled defaults to False — never send this without the tenant opting in.

    _send_media(client_wa, wa_id=OWNER_WA, media_type="audio", media_id="AUDIO_1", mime_type="audio/ogg", message_id="wamid.voice2")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "aren't enabled" in reply
    assert "type" in reply.lower()


def test_voice_note_transcription_failure_asks_to_type_instead(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"voice_notes_enabled": True}, headers=headers)
    services_wa.generator = lambda: FakeGenerator(voice_transcript=RuntimeError("Whisper API unavailable"))

    _send_media(client_wa, wa_id=OWNER_WA, media_type="audio", media_id="AUDIO_1", mime_type="audio/ogg", message_id="wamid.voice3")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Couldn't transcribe" in reply


def test_voice_note_from_a_customer_is_ignored_not_routed_to_rag(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"voice_notes_enabled": True}, headers=headers)
    customer_wa = "918800000000"

    _send_media(client_wa, wa_id=customer_wa, media_type="audio", media_id="AUDIO_1", mime_type="audio/ogg", message_id="wamid.voice4")

    # No lead created, no reply sent — this app has no customer voice-note capability.
    assert services_wa.lead_store.list_for_tenant(tenant_id) == []
    assert not any(m["to"] == customer_wa for m in services_wa.fake_whatsapp_client.sent)


# ------------------------------------------------------------------ receipt OCR


def test_receipt_photo_replies_with_a_suggested_log_purchase_command(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.generator = lambda: FakeGenerator(receipt_extraction=ReceiptExtraction(
        ingredient_name="Chicken", quantity=10.0, unit="kg", amount_inr=4200.0, supplier_name="Ramesh Traders", readable=True,
    ))

    _send_media(client_wa, wa_id=OWNER_WA, media_type="image", media_id="IMG_1", mime_type="image/jpeg", message_id="wamid.receipt1")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "log purchase 10 kg Chicken ₹4200" in reply
    assert "from Ramesh Traders" in reply
    assert "nothing has been logged yet" in reply.lower()
    # Never auto-logged:
    assert services_wa.purchase_store.list_for_tenant(tenant_id) == []


def test_receipt_photo_that_is_unreadable_asks_for_the_command_directly(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.generator = lambda: FakeGenerator(receipt_extraction=ReceiptExtraction(readable=False))

    _send_media(client_wa, wa_id=OWNER_WA, media_type="image", media_id="IMG_2", mime_type="image/jpeg", message_id="wamid.receipt2")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Couldn't read enough" in reply


def test_receipt_photo_ocr_failure_asks_for_the_command_directly(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.generator = lambda: FakeGenerator(receipt_extraction=RuntimeError("vision API unavailable"))

    _send_media(client_wa, wa_id=OWNER_WA, media_type="image", media_id="IMG_3", mime_type="image/jpeg", message_id="wamid.receipt3")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Couldn't read that photo" in reply


def test_receipt_photo_from_a_customer_is_ignored(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    customer_wa = "918800000000"

    _send_media(client_wa, wa_id=customer_wa, media_type="image", media_id="IMG_4", mime_type="image/jpeg", message_id="wamid.receipt4")

    assert services_wa.lead_store.list_for_tenant(tenant_id) == []
    assert not any(m["to"] == customer_wa for m in services_wa.fake_whatsapp_client.sent)


# ------------------------------------------------------------------ log review / reviews commands


def test_log_review_command_records_a_manual_snapshot(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(client_wa, wa_id=OWNER_WA, text="log review zomato 4.3 128", message_id="wamid.rev1")

    latest = services_wa.review_store.latest_by_platform(tenant_id)
    assert latest["zomato"].rating == 4.3
    assert latest["zomato"].review_count == 128
    assert latest["zomato"].source == "manual"
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Zomato" in reply and "4.3" in reply


def test_log_review_command_rejects_an_unknown_platform(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(client_wa, wa_id=OWNER_WA, text="log review yelp 4.3", message_id="wamid.rev2")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Unknown review platform" in reply
    assert services_wa.review_store.list_for_tenant(tenant_id) == []


def test_log_review_command_needs_no_special_permission(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(client_wa, wa_id=RAVI_WA, text="log review swiggy 4.0", message_id="wamid.rev3")

    assert services_wa.review_store.latest_by_platform(tenant_id)["swiggy"].rating == 4.0


def test_reviews_command_shows_the_latest_snapshot(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.review_store.record(tenant_id=tenant_id, platform="google", rating=4.6, review_count=213, source="google_places")

    _send(client_wa, wa_id=OWNER_WA, text="reviews", message_id="wamid.rev4")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Google" in reply and "4.6" in reply


def test_reviews_command_is_owner_manager_gated(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(client_wa, wa_id=RAVI_WA, text="reviews", message_id="wamid.rev5")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA][-1]["body"]
    assert "Only an owner or manager" in reply
