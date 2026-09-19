from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from business_ai.app import Services, create_app
from business_ai.config import load_settings
from business_ai.generation import (
    EmployeeCommandIntent,
    FeedbackClassification,
    LLMResponseDraft,
    ReceiptExtraction,
    ToneAdjustmentDraft,
)
from business_ai.retrieval import HashEmbeddingProvider


class FakeGenerator:
    """Deterministic stand-in for the OpenAI generator: cites whatever
    evidence_passage id appears in the prompt, so tests never spend real
    API credits but still exercise the real validation/citation path."""

    def __init__(
        self, *, status: str = "answered", answer_text: str = "This is a grounded answer.",
        dissatisfied_queries: frozenset[str] = frozenset(), action_brief_items: list[str] = (),
        feedback_classifications: dict[str, FeedbackClassification] | None = None,
        employee_command_intents: dict[str, EmployeeCommandIntent] | None = None,
        tone_adjustment_draft: ToneAdjustmentDraft | Exception | None = None,
        voice_transcript: str | Exception | None = None,
        receipt_extraction: ReceiptExtraction | Exception | None = None,
        shows_buying_intent: bool = False,
    ) -> None:
        self.status = status
        self.answer_text = answer_text
        self.shows_buying_intent = shows_buying_intent
        # Queries (exact match) that classify_dissatisfaction() should
        # report as True — lets tests exercise that path deterministically
        # without a real API call.
        self.dissatisfied_queries = dissatisfied_queries
        self.action_brief_items = list(action_brief_items)
        # Exact-text -> classification overrides for classify_feedback_sentiment,
        # same "deterministic fake, real-API-shaped" idiom as the other fakes here.
        self.feedback_classifications = feedback_classifications or {}
        # Exact-text -> intent overrides for classify_employee_message;
        # default is "other" (help text), same fail-closed-to-safe shape
        # as the real provider's own fallback.
        self.employee_command_intents = employee_command_intents or {}
        # A ToneAdjustmentDraft to return, an Exception instance to raise
        # (exercising evolution.py's deterministic-fallback path), or None
        # for a deterministic default draft — same "override or sensible
        # default" idiom as the other fakes here.
        self.tone_adjustment_draft = tone_adjustment_draft
        # A transcript string to return from transcribe_voice_note(), an
        # Exception instance to raise (exercising admin_bot.py's
        # "couldn't transcribe" fallback), or None for a deterministic
        # default transcript.
        self.voice_transcript = voice_transcript
        # A ReceiptExtraction to return from extract_receipt_data(), an
        # Exception instance to raise, or None for a deterministic
        # default (a fully-readable, fully-populated receipt).
        self.receipt_extraction = receipt_extraction

    def generate(self, *, system_prompt: str, user_prompt: str) -> LLMResponseDraft:
        match = re.search(r'evidence_passage id="([^"]+)"', user_prompt)
        seg_id = match.group(1) if match else "seg_unknown"
        return LLMResponseDraft(
            status=self.status,
            answer_text=self.answer_text,
            cited_segment_ids=[seg_id] if self.status == "answered" else [],
            shows_buying_intent=self.shows_buying_intent,
        )

    def draft_faq_answer(self, *, business_name: str, assistant_name: str, question: str) -> str:
        return f"[Draft answer for {business_name} — fill in the details for: {question}]"

    def classify_dissatisfaction(self, *, query: str) -> bool:
        return query in self.dissatisfied_queries

    def generate_action_brief(self, **kwargs) -> list[str]:
        return self.action_brief_items

    def classify_feedback_sentiment(self, *, text: str) -> FeedbackClassification:
        if text in self.feedback_classifications:
            return self.feedback_classifications[text]
        return FeedbackClassification(
            sentiment="negative", theme="software_or_tools", urgency="medium",
            root_cause_hint="Deterministic test default.", suggested_action="Deterministic test default.",
        )

    def classify_employee_message(self, *, text: str, current_date_iso: str, employee_role: str) -> EmployeeCommandIntent:
        return self.employee_command_intents.get(text, EmployeeCommandIntent(intent="other"))

    def draft_sop_note(self, *, theme_label: str, recent_feedback_texts: list[str]) -> str:
        return f"[draft] Based on {len(recent_feedback_texts)} report(s) about {theme_label}, investigate and address the root cause."

    def draft_tone_adjustment(self, *, dissatisfied_queries: list[str]) -> ToneAdjustmentDraft:
        if isinstance(self.tone_adjustment_draft, Exception):
            raise self.tone_adjustment_draft
        if self.tone_adjustment_draft is not None:
            return self.tone_adjustment_draft
        return ToneAdjustmentDraft(
            theme="general dissatisfaction",
            tone_instructions=f"[draft] Acknowledge the customer's concern before answering ({len(dissatisfied_queries)} sample question(s)).",
        )

    def translate_to_english_for_retrieval(self, *, text: str) -> str:
        # Deterministic no-op in tests — the real translation call is
        # exercised only in the live (non-CI) validation script, never
        # against the real OpenAI API in the test suite.
        return text

    def transcribe_voice_note(self, *, audio_bytes: bytes, mime_type: str) -> str:
        if isinstance(self.voice_transcript, Exception):
            raise self.voice_transcript
        if self.voice_transcript is not None:
            return self.voice_transcript
        return "This is a deterministic test transcript."

    def extract_receipt_data(self, *, image_bytes: bytes, mime_type: str) -> ReceiptExtraction:
        if isinstance(self.receipt_extraction, Exception):
            raise self.receipt_extraction
        if self.receipt_extraction is not None:
            return self.receipt_extraction
        return ReceiptExtraction(
            ingredient_name="Chicken", quantity=10.0, unit="kg", amount_inr=4200.0,
            supplier_name="Ramesh Traders", readable=True,
        )


class FakeEmailSender:
    """Captures every send() call instead of hitting a real network
    endpoint — mirrors FakeGenerator/HashEmbeddingProvider's role."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, to: str, subject: str, html_body: str, text_body: str | None = None) -> None:
        self.sent.append({"to": to, "subject": subject, "html_body": html_body, "text_body": text_body})


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-local-dev-only-32bytes")
    monkeypatch.setenv("ADMIN_SECRET", "test-admin-secret-abcdef")
    return load_settings()


@pytest.fixture()
def services(tmp_path: Path, settings) -> Services:
    svc = Services(settings, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    yield svc
    svc.vector_store.close()


@pytest.fixture()
def app(services):
    return create_app(services)


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture()
def owner_session(client):
    """Sign up a fresh business and return (headers, tenant_id)."""
    r = client.post(
        "/api/auth/signup",
        json={"email": "owner@example.com", "password": "secret123", "name": "Priya", "business_name": "Priya Salon"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["tenant_id"]


@pytest.fixture()
def admin_headers(client, settings):
    r = client.post("/api/auth/login", json={"email": "admin", "password": settings.admin_secret})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def activate_tenant(client, admin_headers):
    def _activate(tenant_id: str) -> None:
        r = client.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
        assert r.status_code == 200, r.text

    return _activate


@pytest.fixture()
def services_with_email(tmp_path: Path, settings) -> Services:
    """Same as `services`, but with digest-email config present and a
    FakeEmailSender capturing sends — for tests of the digest-run route.
    A separate fixture chain (not a mutation of `services`) because the
    email-not-configured path also needs its own test coverage using the
    plain `services` fixture."""
    settings_with_email = dataclasses.replace(
        settings,
        resend_api_key="re_test_key",
        digest_from_email="digest@business-ai.example",
        public_base_url="https://app.example.com",
    )
    svc = Services(settings_with_email, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    fake_sender = FakeEmailSender()
    svc.email_sender = lambda: fake_sender
    svc.fake_email_sender = fake_sender  # test-only handle to inspect .sent
    yield svc
    svc.vector_store.close()


@pytest.fixture()
def app_with_email(services_with_email):
    return create_app(services_with_email)


@pytest.fixture()
def client_with_email(app_with_email):
    from fastapi.testclient import TestClient

    return TestClient(app_with_email)
