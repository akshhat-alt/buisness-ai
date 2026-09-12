"""Pure, dependency-free formatting/parsing helpers shared across route
modules (Phase 9 extraction from app.py). None of these need a `Services`
instance or a request context — they're plain functions of their own
arguments, which is exactly why they're safe to import directly rather
than threading through the shared RouteContext. Names keep their
original leading underscore so every existing call site in app.py's
former body works unchanged after a plain import.
"""

from __future__ import annotations

import re
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

# Single-timezone V1: every pilot business is in India, and there is no
# per-tenant timezone configuration anywhere in this app yet. A bare
# datetime typed into the dashboard's <input type="datetime-local"> is
# assumed to be the business's own local (IST) wall-clock time; real
# multi-timezone support is a stated future scope item, not silently
# guessed at here.
IST = timezone(timedelta(hours=5, minutes=30))

_CITATION_MARKER_RE = re.compile(r"\s*\(seg_[a-zA-Z0-9_]+\)")


def _slugify_tenant_id(business_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")[:32] or "biz"
    return f"{base}-{secrets.token_hex(3)}"


def _whatsapp_link(number: str | None, message: str) -> str | None:
    if not number:
        return None
    return f"https://wa.me/{number}?text={urllib.parse.quote(message)}"


def _parse_appointment_to_utc(raw: str) -> str:
    """Parses an owner-entered appointment datetime into the sortable UTC
    ISO string every other timestamp in this app already uses. Raises
    ValueError on anything unparseable — callers turn that into a 400."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_appointment_ist(iso_utc: str) -> str:
    """The reverse direction, for a human-readable line in a customer-
    facing reminder/win-back message — always shown in the business's
    own local time, never raw UTC."""
    dt_utc = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(IST).strftime("%A, %d %b at %I:%M %p")


def _strip_citation_markers(text: str) -> str:
    """The grounded-generation prompt instructs the model to inline
    "(seg_abc123)" citation markers after each claim (see
    generation.build_system_prompt) — the website widget keeps these
    invisible to a real customer by never rendering answer_text raw as
    the only signal (a separate "Sourced from X" chip carries the actual
    citation). WhatsApp has no such chip; sending literal segment IDs
    straight to a customer's phone would look broken, so the WhatsApp
    reply path strips them. The underlying grounding/citation validation
    in generation.validate_llm_draft is untouched — this is purely a
    presentation-layer cleanup for one channel's plain-text medium."""
    return _CITATION_MARKER_RE.sub("", text).strip()


def _short_task_id(task_id: str) -> str:
    return task_id[-6:]
