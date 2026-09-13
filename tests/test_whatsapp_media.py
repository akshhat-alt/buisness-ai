"""Tests for Phase 25's WhatsApp media receiving: parse_webhook_payload's
audio/image extraction and WhatsAppClient.get_media_url/download_media
(Meta's two-step retrieval). Pure unit tests, urlopen mocked — never a
real network call.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError

from business_ai.whatsapp import WhatsAppClient, WhatsAppSendError, parse_webhook_payload


def _payload_with_message(message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_TEST",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550001111", "phone_number_id": "PNID_1"},
                    "contacts": [{"profile": {"name": "Ravi"}, "wa_id": "919876500001"}],
                    "messages": [message],
                },
                "field": "messages",
            }],
        }],
    }


# ------------------------------------------------------------------ parse_webhook_payload


def test_parses_an_audio_message():
    payload = _payload_with_message({
        "from": "919876500001", "id": "wamid.audio1", "timestamp": "1700000000", "type": "audio",
        "audio": {"id": "MEDIA_ID_1", "mime_type": "audio/ogg; codecs=opus"},
    })
    messages = parse_webhook_payload(payload)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.media_type == "audio"
    assert msg.media_id == "MEDIA_ID_1"
    assert msg.mime_type == "audio/ogg; codecs=opus"
    assert msg.text == ""


def test_parses_an_image_message_with_a_caption():
    payload = _payload_with_message({
        "from": "919876500001", "id": "wamid.image1", "timestamp": "1700000000", "type": "image",
        "image": {"id": "MEDIA_ID_2", "mime_type": "image/jpeg", "caption": "today's receipt"},
    })
    messages = parse_webhook_payload(payload)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.media_type == "image"
    assert msg.media_id == "MEDIA_ID_2"
    assert msg.text == "today's receipt"


def test_skips_a_media_message_with_no_media_id():
    payload = _payload_with_message({
        "from": "919876500001", "id": "wamid.image2", "timestamp": "1700000000", "type": "image",
        "image": {"mime_type": "image/jpeg"},
    })
    assert parse_webhook_payload(payload) == []


def test_text_messages_still_parse_with_no_media_fields_set():
    payload = _payload_with_message({
        "from": "919876500001", "id": "wamid.text1", "timestamp": "1700000000", "type": "text",
        "text": {"body": "hello"},
    })
    messages = parse_webhook_payload(payload)
    assert len(messages) == 1
    msg = messages[0]
    assert msg.media_type is None
    assert msg.media_id is None
    assert msg.text == "hello"


def test_unsupported_message_types_are_still_silently_skipped():
    payload = _payload_with_message({
        "from": "919876500001", "id": "wamid.sticker1", "timestamp": "1700000000", "type": "sticker",
        "sticker": {"id": "STICKER_1"},
    })
    assert parse_webhook_payload(payload) == []


# ------------------------------------------------------------------ WhatsAppClient media download


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_get_media_url_returns_the_download_url(monkeypatch):
    client = WhatsAppClient()

    def fake_urlopen(request, timeout=15):
        return _FakeHTTPResponse(json.dumps({"url": "https://lookaside.fbsbx.com/media/xyz", "mime_type": "audio/ogg"}).encode())

    monkeypatch.setattr("business_ai.whatsapp.urlopen", fake_urlopen)
    url = client.get_media_url(media_id="MEDIA_ID_1", access_token="test-token")
    assert url == "https://lookaside.fbsbx.com/media/xyz"


def test_get_media_url_raises_when_no_url_returned(monkeypatch):
    client = WhatsAppClient()

    def fake_urlopen(request, timeout=15):
        return _FakeHTTPResponse(json.dumps({}).encode())

    monkeypatch.setattr("business_ai.whatsapp.urlopen", fake_urlopen)
    try:
        client.get_media_url(media_id="MEDIA_ID_1", access_token="test-token")
        assert False, "expected WhatsAppSendError"
    except WhatsAppSendError as exc:
        assert "download URL" in str(exc)


def test_get_media_url_wraps_http_error(monkeypatch):
    client = WhatsAppClient()

    def fake_urlopen(request, timeout=15):
        raise HTTPError(request.full_url, 401, "Unauthorized", None, None)

    monkeypatch.setattr("business_ai.whatsapp.urlopen", fake_urlopen)
    try:
        client.get_media_url(media_id="MEDIA_ID_1", access_token="bad-token")
        assert False, "expected WhatsAppSendError"
    except WhatsAppSendError as exc:
        assert "401" in str(exc)


def test_download_media_returns_raw_bytes(monkeypatch):
    client = WhatsAppClient()

    def fake_urlopen(request, timeout=30):
        return _FakeHTTPResponse(b"\xff\xd8\xff\xe0fake-jpeg-bytes")

    monkeypatch.setattr("business_ai.whatsapp.urlopen", fake_urlopen)
    data = client.download_media(media_url="https://lookaside.fbsbx.com/media/xyz", access_token="test-token")
    assert data == b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def test_download_media_wraps_http_error(monkeypatch):
    client = WhatsAppClient()

    def fake_urlopen(request, timeout=30):
        raise HTTPError(request.full_url, 410, "Gone", None, None)

    monkeypatch.setattr("business_ai.whatsapp.urlopen", fake_urlopen)
    try:
        client.download_media(media_url="https://lookaside.fbsbx.com/media/xyz", access_token="test-token")
        assert False, "expected WhatsAppSendError"
    except WhatsAppSendError as exc:
        assert "410" in str(exc)
