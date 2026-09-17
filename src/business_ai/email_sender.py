"""Transactional email sending via the Resend HTTP API.

Uses stdlib urllib instead of adding an SDK/httpx as a production
dependency — the same "smaller dependency footprint" instinct as
ingestion.py's stdlib HTML parser. One HTTP call, one JSON payload; no
client library needed.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RESEND_API_URL = "https://api.resend.com/emails"


class EmailSendError(Exception):
    """Raised when an email could not be sent."""


class EmailSender:
    def __init__(self, *, api_key: str, from_address: str, reply_to: str | None = None) -> None:
        if not api_key:
            raise ValueError("An email provider API key is required.")
        if not from_address:
            raise ValueError("A from-address is required.")
        self._api_key = api_key
        self._from_address = from_address
        self._reply_to = reply_to

    def send(self, *, to: str, subject: str, html_body: str, text_body: str | None = None) -> None:
        payload: dict = {
            "from": self._from_address,
            "to": [to],
            "subject": subject,
            "html": html_body,
        }
        if text_body:
            payload["text"] = text_body
        if self._reply_to:
            payload["reply_to"] = [self._reply_to]

        request = Request(
            RESEND_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Business-AI/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise EmailSendError(f"Resend API error {exc.code} sending to {to}: {detail}") from exc
        except URLError as exc:
            raise EmailSendError(f"Could not reach Resend sending to {to}: {exc}") from exc
