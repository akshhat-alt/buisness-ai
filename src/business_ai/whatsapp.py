"""WhatsApp Business Cloud API integration: inbound webhook parsing +
signature verification, outbound message sending, and inbox idempotency.

Architecture (see ARCHITECTURE.md for the full writeup): Business AI runs
ONE Meta App with ONE webhook URL, shared by every tenant. A tenant's
`phone_number_id` + permanent `access_token` are stored on their own
TenantConfig either way; there are two ways they get there:

  1. Manual Cloud API setup — the owner creates their own Meta Developer
     App, generates a token, and pastes both values into the dashboard.
     Always available, zero external dependency, the fallback forever.
  2. WhatsApp Embedded Signup (MetaEmbeddedSignupClient below) — the
     owner clicks "Connect WhatsApp," authorizes inside a Meta-hosted
     popup, and never sees a raw token. Requires Business AI to be a
     registered Meta Tech Provider with the relevant App Review approved
     (WHATSAPP_APP_ID configured) — until then, this path reports itself
     unavailable and callers fall back to (1) automatically.

Either way, the platform-level Meta App holds the shared
`WHATSAPP_APP_SECRET` (webhook signature verification AND, for Embedded
Signup, the OAuth code exchange) and `WHATSAPP_VERIFY_TOKEN` (webhook
handshake) — it never needs its own WhatsApp number.

Uses stdlib urllib for the same reason email_sender.py does: one HTTP
call, one JSON payload, no SDK dependency justified.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GRAPH_API_HOST = "https://graph.facebook.com"


class WhatsAppSendError(Exception):
    """Raised when an outbound WhatsApp message could not be delivered."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


# Meta's error code for "the free-form 24-hour customer service window has
# closed" — the caller must use a pre-approved message template instead.
# Surfaced as a distinct, recognizable attribute so callers (e.g. the
# review-request follow-up) can give the owner an honest, specific reason
# rather than a generic "send failed".
REENGAGEMENT_WINDOW_CLOSED_CODE = 131047


@dataclass(frozen=True)
class IncomingWhatsAppMessage:
    phone_number_id: str  # the BUSINESS's WhatsApp number Meta routed this to
    wa_id: str  # the CUSTOMER's WhatsApp id (their phone number, digits only)
    message_id: str
    text: str  # empty string for a media message with no caption
    contact_name: str | None
    timestamp: str
    # Phase 25 — Perception & Input Expansion: media_type is None for a
    # plain text message, or "audio"/"image"/"document" for a media
    # message. media_id is Meta's opaque handle — pass it to
    # WhatsAppClient.get_media_url() then download_media() to retrieve
    # the actual bytes (Meta's two-step retrieval; the URL itself is
    # short-lived and requires the same access token to fetch).
    media_type: str | None = None
    media_id: str | None = None
    mime_type: str | None = None


class WhatsAppClient:
    """Thin wrapper over the Cloud API's /messages endpoint. Each call is
    scoped to one tenant's own phone_number_id + access token — this
    client is deliberately stateless/tenant-agnostic so it can't
    accidentally reuse one tenant's credentials for another's send."""

    def __init__(self, *, api_version: str = "v21.0") -> None:
        self._api_version = api_version

    def _post(self, *, phone_number_id: str, access_token: str, payload: dict) -> dict:
        import json

        url = f"{GRAPH_API_HOST}/{self._api_version}/{phone_number_id}/messages"
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            error_code = None
            try:
                error_code = json.loads(body).get("error", {}).get("code")
            except Exception:  # noqa: BLE001 - best-effort error-code extraction only
                pass
            raise WhatsAppSendError(f"WhatsApp API error {exc.code}: {body}", error_code=error_code) from exc
        except URLError as exc:
            raise WhatsAppSendError(f"Could not reach the WhatsApp API: {exc}") from exc

    def send_text(self, *, phone_number_id: str, access_token: str, to: str, body: str) -> dict:
        if not phone_number_id or not access_token:
            raise WhatsAppSendError("This business has not connected a WhatsApp number yet.")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": body, "preview_url": False},
        }
        return self._post(phone_number_id=phone_number_id, access_token=access_token, payload=payload)

    def send_template(
        self, *, phone_number_id: str, access_token: str, to: str, template_name: str, language_code: str = "en"
    ) -> dict:
        """Sends a pre-approved WhatsApp message template — the ONLY way
        to reach someone outside Meta's 24h customer-service window
        (see REENGAGEMENT_WINDOW_CLOSED_CODE). Deliberately minimal: no
        component/variable support yet, so this can only send a static
        template with no dynamic body text — a tenant's approved
        template must be created accordingly (e.g. a fixed "you have a
        new update from Business AI, check WhatsApp or your dashboard"
        notice, not one that embeds the actual summary). Templating with
        variables is real additional Meta-side setup complexity, deferred
        until there's a concrete need for it."""
        if not phone_number_id or not access_token:
            raise WhatsAppSendError("This business has not connected a WhatsApp number yet.")
        if not template_name:
            raise WhatsAppSendError("No approved WhatsApp template is configured for this business.")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {"name": template_name, "language": {"code": language_code}},
        }
        return self._post(phone_number_id=phone_number_id, access_token=access_token, payload=payload)

    def mark_read(self, *, phone_number_id: str, access_token: str, message_id: str) -> None:
        """Best-effort read receipt — never worth failing the whole
        webhook turn over, so callers should swallow errors from this."""
        payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        self._post(phone_number_id=phone_number_id, access_token=access_token, payload=payload)

    def get_media_url(self, *, media_id: str, access_token: str) -> str:
        """Step 1 of Meta's two-step media retrieval: exchange an opaque
        media_id (from an inbound message) for a short-lived download
        URL. That URL itself still requires the SAME access token as a
        Bearer header to actually fetch — see download_media below."""
        import json

        url = f"{GRAPH_API_HOST}/{self._api_version}/{media_id}"
        request = Request(url, headers={"Authorization": f"Bearer {access_token}"}, method="GET")
        try:
            with urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise WhatsAppSendError(f"WhatsApp media lookup failed ({exc.code}): {detail}") from exc
        except URLError as exc:
            raise WhatsAppSendError(f"Could not reach the WhatsApp API: {exc}") from exc
        media_url = body.get("url")
        if not media_url:
            raise WhatsAppSendError("WhatsApp did not return a media download URL.")
        return media_url

    def download_media(self, *, media_url: str, access_token: str) -> bytes:
        """Step 2: fetches the actual media bytes from the short-lived
        URL get_media_url() returned. Kept as a separate call (not
        folded into get_media_url) because a caller may want to check
        the media's reported mime_type/size (from the same lookup
        response) before deciding whether to download it at all."""
        request = Request(media_url, headers={"Authorization": f"Bearer {access_token}"}, method="GET")
        try:
            with urlopen(request, timeout=30) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise WhatsAppSendError(f"WhatsApp media download failed ({exc.code}): {detail}") from exc
        except URLError as exc:
            raise WhatsAppSendError(f"Could not reach the WhatsApp API: {exc}") from exc


class MetaEmbeddedSignupError(Exception):
    """Raised when Embedded Signup's OAuth code exchange fails."""


class MetaEmbeddedSignupClient:
    """Server-side half of WhatsApp Embedded Signup.

    The frontend flow (not implemented here — it needs a real
    WHATSAPP_APP_ID and an approved Meta Tech Provider registration,
    neither of which exist yet) opens Meta's own hosted popup via their
    JS SDK. When the owner finishes connecting their WhatsApp number
    inside that popup, Meta's callback hands the frontend two things
    directly: a short-lived authorization `code`, and the new
    `phone_number_id` itself (Embedded Signup returns it inline — no
    separate discovery call needed). This client exists only to turn
    that `code` into a long-lived access token, server-side, using
    Business AI's OWN app credentials — the entire point of Embedded
    Signup is that the business never sees or handles a raw token.
    """

    def __init__(self, *, api_version: str = "v21.0") -> None:
        self._api_version = api_version

    def exchange_code_for_token(self, *, app_id: str, app_secret: str, code: str) -> str:
        import json
        from urllib.parse import urlencode

        query = urlencode({"client_id": app_id, "client_secret": app_secret, "code": code})
        url = f"{GRAPH_API_HOST}/{self._api_version}/oauth/access_token?{query}"
        try:
            with urlopen(Request(url, method="GET"), timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise MetaEmbeddedSignupError(f"Meta token exchange failed ({exc.code}): {detail}") from exc
        except URLError as exc:
            raise MetaEmbeddedSignupError(f"Could not reach Meta: {exc}") from exc

        token = body.get("access_token")
        if not token:
            raise MetaEmbeddedSignupError("Meta did not return an access token.")
        return token


def verify_webhook_signature(*, raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Verifies Meta's `X-Hub-Signature-256: sha256=<hex>` header — HMAC-
    SHA256 of the exact raw request bytes, keyed by the Meta App's secret.
    Must run against the RAW body, before any JSON parsing/re-serialization
    could change a single byte and silently break the comparison."""
    if not signature_header or not app_secret:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len(prefix):])


_MEDIA_MESSAGE_TYPES = frozenset({"audio", "image", "document"})


def parse_webhook_payload(payload: dict) -> list[IncomingWhatsAppMessage]:
    """Extracts every inbound text OR media message from a Meta webhook
    POST body.

    Phase 25 added audio/image/document extraction (voice notes, receipt
    photos) alongside the original text-only handling — each yields a
    message with media_type/media_id/mime_type set and text="" (or the
    media's caption, if one was attached). Everything else this app
    doesn't handle (delivery/read status callbacks, video/sticker/
    location/contact messages) is still silently skipped, not an error.
    A malformed/unexpected payload shape yields an empty list rather
    than raising, so one bad entry never breaks processing of the
    others in the same delivery.
    """
    messages: list[IncomingWhatsAppMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            if not phone_number_id:
                continue
            contacts = {c.get("wa_id"): c for c in (value.get("contacts") or [])}
            for msg in value.get("messages", []) or []:
                msg_type = msg.get("type")
                wa_id = msg.get("from")
                message_id = msg.get("id")
                if not (wa_id and message_id):
                    continue
                contact = contacts.get(wa_id)
                contact_name = (contact.get("profile") or {}).get("name") if contact else None
                timestamp = str(msg.get("timestamp") or "")

                if msg_type == "text":
                    text_body = (msg.get("text") or {}).get("body")
                    if not text_body:
                        continue
                    messages.append(
                        IncomingWhatsAppMessage(
                            phone_number_id=phone_number_id, wa_id=wa_id, message_id=message_id,
                            text=text_body, contact_name=contact_name, timestamp=timestamp,
                        )
                    )
                elif msg_type in _MEDIA_MESSAGE_TYPES:
                    media = msg.get(msg_type) or {}
                    media_id = media.get("id")
                    if not media_id:
                        continue
                    messages.append(
                        IncomingWhatsAppMessage(
                            phone_number_id=phone_number_id, wa_id=wa_id, message_id=message_id,
                            text=media.get("caption") or "", contact_name=contact_name, timestamp=timestamp,
                            media_type=msg_type, media_id=media_id, mime_type=media.get("mime_type"),
                        )
                    )
    return messages


from business_ai.storage import SqliteStore


class WhatsAppInboxStore(SqliteStore):
    """Idempotency guard: Meta redelivers webhook events on a non-200
    response and can occasionally deliver the same event more than once
    even on success. Without this, a redelivery would answer (and bill
    quota for) the same customer message twice. One row per Meta message
    id, first-write-wins."""

    def __init__(self, db_path: Path | str = "data/whatsapp_inbox.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def claim(self, message_id: str, tenant_id: str) -> bool:
        """Returns True if this message_id was not seen before (caller
        should process it), False if it's a duplicate (caller should
        skip). Atomic: the INSERT itself is the claim."""
        with self._lock, self._db() as conn:
            try:
                conn.execute(
                    "INSERT INTO processed_messages (message_id, tenant_id, created_at) VALUES (?, ?, ?)",
                    (message_id, tenant_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def delete_for_tenant(self, tenant_id: str) -> int:
        """Phase 9 tenant data deletion: removes every row for this
        tenant. Returns the number of rows deleted, for the export/
        deletion endpoint's summary report."""
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM processed_messages WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
