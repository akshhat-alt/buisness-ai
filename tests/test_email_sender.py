"""Tests for EmailSender (email_sender.py): Reply-To support, and the
Resend HTTPError body-extraction/User-Agent fix from the production
403 incident. Pure unit tests, urlopen mocked — never a real network call."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

import pytest

from business_ai.email_sender import EmailSendError, EmailSender


def test_send_omits_reply_to_when_not_configured(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=15):
        captured["payload"] = json.loads(request.data.decode("utf-8"))

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Resp()

    monkeypatch.setattr("business_ai.email_sender.urlopen", fake_urlopen)

    sender = EmailSender(api_key="re_test", from_address="noreply@send.bizistic.com")
    sender.send(to="user@example.com", subject="Hi", html_body="<p>Hi</p>")

    assert "reply_to" not in captured["payload"]
    assert captured["payload"]["from"] == "noreply@send.bizistic.com"


def test_send_includes_reply_to_when_configured(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=15):
        captured["payload"] = json.loads(request.data.decode("utf-8"))

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Resp()

    monkeypatch.setattr("business_ai.email_sender.urlopen", fake_urlopen)

    sender = EmailSender(
        api_key="re_test",
        from_address="noreply@send.bizistic.com",
        reply_to="support@bizistic.com",
    )
    sender.send(to="user@example.com", subject="Hi", html_body="<p>Hi</p>")

    assert captured["payload"]["reply_to"] == ["support@bizistic.com"]


def test_send_sets_explicit_user_agent(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=15):
        captured["headers"] = request.headers

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Resp()

    monkeypatch.setattr("business_ai.email_sender.urlopen", fake_urlopen)

    sender = EmailSender(api_key="re_test", from_address="noreply@send.bizistic.com")
    sender.send(to="user@example.com", subject="Hi", html_body="<p>Hi</p>")

    assert captured["headers"].get("User-agent") == "Business-AI/1.0"


def test_send_raises_with_resend_error_body_on_http_error(monkeypatch):
    error_body = json.dumps(
        {"statusCode": 403, "name": "validation_error", "message": "The gmail.com domain is not verified."}
    ).encode("utf-8")

    def fake_urlopen(request, timeout=15):
        raise HTTPError(
            url="https://api.resend.com/emails",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=__import__("io").BytesIO(error_body),
        )

    monkeypatch.setattr("business_ai.email_sender.urlopen", fake_urlopen)

    sender = EmailSender(api_key="re_test", from_address="akshhatwankhade@gmail.com")

    with pytest.raises(EmailSendError) as exc_info:
        sender.send(to="user@example.com", subject="Hi", html_body="<p>Hi</p>")

    assert "403" in str(exc_info.value)
    assert "validation_error" in str(exc_info.value)
    assert "not verified" in str(exc_info.value)


def test_send_raises_on_unreachable_host(monkeypatch):
    def fake_urlopen(request, timeout=15):
        raise URLError("no route to host")

    monkeypatch.setattr("business_ai.email_sender.urlopen", fake_urlopen)

    sender = EmailSender(api_key="re_test", from_address="noreply@send.bizistic.com")

    with pytest.raises(EmailSendError) as exc_info:
        sender.send(to="user@example.com", subject="Hi", html_body="<p>Hi</p>")

    assert "Resend" in str(exc_info.value)
