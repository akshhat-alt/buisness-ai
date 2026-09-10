"""Tests for the five growth-automation features built on top of the
existing WhatsApp/lead/analytics infrastructure:
  1. Missed-lead re-engagement
  2. Appointment reminders (+ no-show prevention)
  3. Deposit / payment links
  4. Hindi / Hinglish language support
  5. Customer win-back

Each scheduled job (reengagement/run, reminders/run, winback/run) mirrors
the existing digest/run endpoint's shape: platform-admin only, iterates
ACTIVE tenants, best-effort per lead. All WhatsApp/Razorpay calls are
faked — no real network calls, no OpenAI cost.
"""

from __future__ import annotations

import time

import pytest

from business_ai.app import Services, create_app
from business_ai.generation import build_system_prompt
from business_ai.payments import PaymentLinkError
from business_ai.retrieval import HashEmbeddingProvider
from tests.conftest import FakeGenerator


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_text(self, *, phone_number_id: str, access_token: str, to: str, body: str) -> dict:
        self.sent.append({"phone_number_id": phone_number_id, "access_token": access_token, "to": to, "body": body})
        return {"messages": [{"id": "wamid.fake"}]}

    def mark_read(self, **kwargs) -> None:
        pass


class FakeRazorpayClient:
    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.raise_error = raise_error

    def create_payment_link(self, **kwargs) -> str:
        if self.raise_error:
            raise self.raise_error
        self.calls.append(kwargs)
        return "https://rzp.io/i/fake-deposit-link"


@pytest.fixture()
def services_auto(tmp_path, settings):
    svc = Services(settings, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    fake_wa = FakeWhatsAppClient()
    svc.whatsapp_client = lambda: fake_wa
    svc.fake_whatsapp_client = fake_wa
    fake_rzp = FakeRazorpayClient()
    svc.razorpay_client = lambda: fake_rzp
    svc.fake_razorpay_client = fake_rzp
    return svc


@pytest.fixture()
def client_auto(services_auto):
    from fastapi.testclient import TestClient

    return TestClient(create_app(services_auto))


def _signup(client, business_name="Priya Salon", email="owner@example.com"):
    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "secret123", "name": "Priya", "business_name": business_name},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["tenant_id"]


def _activate(client, headers, tenant_id, admin_password):
    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text
    admin_login = client.post("/api/auth/login", json={"email": "admin", "password": admin_password})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 200, r.text
    return admin_headers


def _connect_whatsapp(client, headers, tenant_id, phone_number_id="PNID_1"):
    r = client.put(
        f"/api/tenant?tenant_id={tenant_id}",
        json={"whatsapp_phone_number_id": phone_number_id, "whatsapp_access_token": "tok"},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def _iso_hours_ago(hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))


def _backdate_lead(lead_store, tenant_id, lead_id, created_at_iso):
    with lead_store._db() as conn:
        conn.execute(
            "UPDATE leads SET created_at = ? WHERE tenant_id = ? AND lead_id = ?", (created_at_iso, tenant_id, lead_id)
        )
        conn.commit()


def _make_lead(services_auto, tenant_id, *, source="whatsapp", phone="919876543210", email=None, created_hours_ago=None):
    lead = services_auto.lead_store.create(
        tenant_id=tenant_id, session_id=f"wa_{phone}" if source == "whatsapp" else "sess_1",
        name="Asha", phone=phone, email=email, message="hi", source=source,
    )
    if created_hours_ago is not None:
        _backdate_lead(services_auto.lead_store, tenant_id, lead.lead_id, _iso_hours_ago(created_hours_ago))
    return lead


# ============================================================== Feature 1: missed-lead re-engagement


def test_reengagement_sends_to_stale_lead(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    _make_lead(services_auto, tenant_id, created_hours_ago=72)  # older than the 48h minimum

    r = client_auto.post("/api/v1/admin/reengagement/run", headers=headers)
    # not platform admin yet — owner token shouldn't work
    assert r.status_code == 403

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/reengagement/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["sent"]) == 1
    assert len(services_auto.fake_whatsapp_client.sent) == 1
    assert services_auto.fake_whatsapp_client.sent[0]["to"] == "919876543210"

    # idempotent: a second run doesn't re-send the same lead
    r2 = client_auto.post("/api/v1/admin/reengagement/run", headers=admin_headers)
    assert r2.json()["sent"] == []
    assert len(services_auto.fake_whatsapp_client.sent) == 1


def test_reengagement_skips_too_recent_lead(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    _make_lead(services_auto, tenant_id, created_hours_ago=2)  # under the 48h minimum

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/reengagement/run", headers=admin_headers)
    assert r.json()["sent"] == []
    assert services_auto.fake_whatsapp_client.sent == []


def test_reengagement_skips_stale_beyond_max_window(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    _make_lead(services_auto, tenant_id, created_hours_ago=24 * 20)  # older than the 14-day ceiling

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/reengagement/run", headers=admin_headers)
    assert r.json()["sent"] == []


def test_reengagement_skips_lead_that_already_booked(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id, created_hours_ago=72)
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(-24))  # booked, in the future

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/reengagement/run", headers=admin_headers)
    assert r.json()["sent"] == []  # already converted — don't nudge them


def test_reengagement_reports_skip_reason_without_whatsapp(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    # deliberately NOT connecting WhatsApp
    _make_lead(services_auto, tenant_id, created_hours_ago=72)

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/reengagement/run", headers=admin_headers)
    data = r.json()
    assert data["sent"] == []
    assert len(data["skipped"]) == 1
    assert data["skipped"][0]["reason"] == "no WhatsApp channel available"


# ============================================================== Feature 2: appointment reminders


def test_set_appointment_and_reminder_fires_in_window(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id)

    # ~24h from now in UTC, expressed as the naive IST wall-clock string a
    # dashboard <input type="datetime-local"> would send (no offset) —
    # _parse_appointment_to_utc treats a naive input as IST and subtracts
    # 5:30 to store UTC, so the naive string must be UTC target + 5:30.
    import datetime as dt

    target_utc = dt.datetime.utcnow() + dt.timedelta(hours=24)
    naive_ist_input = target_utc + dt.timedelta(hours=5, minutes=30)
    r = client_auto.put(
        f"/api/leads/{lead.lead_id}/appointment?tenant_id={tenant_id}",
        json={"appointment_at": naive_ist_input.strftime("%Y-%m-%dT%H:%M:%S")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["appointment_at"] is not None

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/reminders/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["sent"]) == 1
    assert "Reminder" in services_auto.fake_whatsapp_client.sent[0]["body"]

    # idempotent
    r2 = client_auto.post("/api/v1/admin/reminders/run", headers=admin_headers)
    assert r2.json()["sent"] == []


def test_reminder_skips_appointment_far_in_future(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id)
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(-24 * 5))  # 5 days out

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/reminders/run", headers=admin_headers)
    assert r.json()["sent"] == []


def test_reschedule_resets_reminder_flag(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id)
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(-24))
    services_auto.lead_store.mark_reminder_sent(tenant_id, lead.lead_id)

    updated = services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(-48))
    assert updated.reminder_sent_at is None  # rescheduled — the old reminder marker no longer applies


def test_set_appointment_rejects_invalid_datetime(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    lead = _make_lead(services_auto, tenant_id)

    r = client_auto.put(
        f"/api/leads/{lead.lead_id}/appointment?tenant_id={tenant_id}", json={"appointment_at": "not-a-date"}, headers=headers
    )
    assert r.status_code == 400


def test_set_appointment_is_tenant_isolated(client_auto, services_auto):
    headers_a, tenant_a = _signup(client_auto, business_name="Salon A", email="a@example.com")
    headers_b, tenant_b = _signup(client_auto, business_name="Salon B", email="b@example.com")
    _activate(client_auto, headers_a, tenant_a, services_auto.settings.admin_secret)
    lead = _make_lead(services_auto, tenant_a)

    r = client_auto.put(
        f"/api/leads/{lead.lead_id}/appointment?tenant_id={tenant_b}",
        json={"appointment_at": "2026-12-01T10:00:00"},
        headers=headers_b,
    )
    assert r.status_code == 404


# ============================================================== Feature 3: deposit / payment links


def test_deposit_link_sent_over_whatsapp(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    client_auto.put(
        f"/api/tenant?tenant_id={tenant_id}",
        json={"deposit_amount_inr": 200, "razorpay_key_id": "rzp_test_key", "razorpay_key_secret": "rzp_test_secret"},
        headers=headers,
    )
    lead = _make_lead(services_auto, tenant_id)

    r = client_auto.post(f"/api/leads/{lead.lead_id}/deposit-link?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["channel"] == "whatsapp"
    assert data["payment_url"] == "https://rzp.io/i/fake-deposit-link"
    assert len(services_auto.fake_razorpay_client.calls) == 1
    assert services_auto.fake_razorpay_client.calls[0]["amount_inr"] == 200
    assert "fake-deposit-link" in services_auto.fake_whatsapp_client.sent[0]["body"]

    leads = client_auto.get(f"/api/leads?tenant_id={tenant_id}", headers=headers).json()["leads"]
    assert leads[0]["deposit_link_sent_at"] is not None


def test_deposit_link_requires_amount_configured(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id)

    r = client_auto.post(f"/api/leads/{lead.lead_id}/deposit-link?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "deposit amount" in r.json()["detail"].lower()


def test_deposit_link_requires_whatsapp_when_lead_is_whatsapp_sourced(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    # deliberately not connecting WhatsApp
    client_auto.put(f"/api/tenant?tenant_id={tenant_id}", json={"deposit_amount_inr": 200}, headers=headers)
    lead = _make_lead(services_auto, tenant_id)

    r = client_auto.post(f"/api/leads/{lead.lead_id}/deposit-link?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert services_auto.fake_razorpay_client.calls == []  # never even attempted to create a link


def test_deposit_link_surfaces_razorpay_error(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    client_auto.put(
        f"/api/tenant?tenant_id={tenant_id}",
        json={"deposit_amount_inr": 200, "razorpay_key_id": "bad", "razorpay_key_secret": "bad"},
        headers=headers,
    )
    lead = _make_lead(services_auto, tenant_id)
    services_auto.fake_razorpay_client.raise_error = PaymentLinkError("Razorpay API error 401: invalid credentials")

    r = client_auto.post(f"/api/leads/{lead.lead_id}/deposit-link?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "invalid credentials" in r.json()["detail"]
    assert services_auto.fake_whatsapp_client.sent == []


# ============================================================== Feature 4: Hindi / Hinglish support


def test_system_prompt_instructs_language_mirroring():
    prompt = build_system_prompt("Priya Salon", "Priya's Assistant")
    lowered = prompt.lower()
    assert "hindi" in lowered
    assert "hinglish" in lowered
    assert "same language" in lowered


def test_whatsapp_channel_gets_a_brevity_instruction_web_does_not():
    wa_prompt = build_system_prompt("Priya Salon", "Priya's Assistant", channel="whatsapp").lower()
    web_prompt = build_system_prompt("Priya Salon", "Priya's Assistant", channel="web").lower()
    default_prompt = build_system_prompt("Priya Salon", "Priya's Assistant").lower()
    assert "whatsapp style" in wa_prompt
    assert "short" in wa_prompt
    assert "whatsapp style" not in web_prompt
    assert "whatsapp style" not in default_prompt  # "web" stays the default, unchanged behavior


# ============================================================== Feature 5: customer win-back


def test_winback_sent_to_lapsed_whatsapp_customer(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id, source="whatsapp")
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(24 * 60))  # 60 days ago

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/winback/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["sent"]) == 1
    assert "miss you" in services_auto.fake_whatsapp_client.sent[0]["body"].lower()

    r2 = client_auto.post("/api/v1/admin/winback/run", headers=admin_headers)
    assert r2.json()["sent"] == []  # already nudged for this gap


def test_winback_skips_recent_visit(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id, source="whatsapp")
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(24 * 10))  # 10 days ago

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/winback/run", headers=admin_headers)
    assert r.json()["sent"] == []  # under the 45-day platform default


def test_winback_ignores_non_whatsapp_leads(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id, source="chat", phone="919876500000")
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(24 * 60))

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/winback/run", headers=admin_headers)
    assert r.json()["sent"] == []


def test_winback_respects_per_tenant_threshold(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    client_auto.put(f"/api/tenant?tenant_id={tenant_id}", json={"winback_after_days": 10}, headers=headers)
    lead = _make_lead(services_auto, tenant_id, source="whatsapp")
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(24 * 15))  # 15 days — past this tenant's 10-day threshold

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_auto.post("/api/v1/admin/winback/run", headers=admin_headers)
    assert len(r.json()["sent"]) == 1  # would NOT have fired under the 45-day platform default


def test_winback_refires_after_a_new_lapse(client_auto, services_auto):
    headers, tenant_id = _signup(client_auto)
    _activate(client_auto, headers, tenant_id, services_auto.settings.admin_secret)
    _connect_whatsapp(client_auto, headers, tenant_id)
    lead = _make_lead(services_auto, tenant_id, source="whatsapp")
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(24 * 60))

    admin_login = client_auto.post("/api/auth/login", json={"email": "admin", "password": services_auto.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    client_auto.post("/api/v1/admin/winback/run", headers=admin_headers)  # first nudge

    # customer came back and rebooked, then lapsed again
    services_auto.lead_store.set_appointment(tenant_id, lead.lead_id, _iso_hours_ago(24 * 50))
    r = client_auto.post("/api/v1/admin/winback/run", headers=admin_headers)
    assert len(r.json()["sent"]) == 1  # eligible again for the NEW lapse
