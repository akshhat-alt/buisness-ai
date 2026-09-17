"""Tests for the self-service password reset flow: PasswordResetStore,
alert email rendering, forgot-password and reset-password API routes,
and static page serving."""

from __future__ import annotations

from unittest.mock import patch

from business_ai.auth import PasswordResetStore
from business_ai.email_sender import EmailSendError


def test_password_reset_store_lifecycle(tmp_path):
    db_path = tmp_path / "test_resets.db"
    store = PasswordResetStore(db_path)

    token = store.create("user-123", lifetime_seconds=1800)
    assert token
    assert len(token) >= 32

    record = store.get(token)
    assert record is not None
    assert record.user_id == "user-123"
    assert record.used_at is None

    store.mark_used(token)
    updated = store.get(token)
    assert updated is not None
    assert updated.used_at is not None

    assert store.get("non-existent-token") is None


def test_user_store_update_password(services):
    user_store = services.user_store
    user = user_store.create_user(
        email="testuser@example.com",
        password="initial_password_123",
        name="Test User",
        business_name="Test Business",
        tenant_id="tenant_test",
    )

    # Verify initial password
    user_record = user_store.get_user_by_id(user.user_id)
    assert user_record is not None
    assert user_store.verify_password("initial_password_123", user_record.password_hash, user_record.salt)

    # Update to new password
    user_store.update_password(user.user_id, "new_secure_password_456")

    # Old password no longer verifies
    updated_record = user_store.get_user_by_id(user.user_id)
    assert updated_record is not None
    assert not user_store.verify_password("initial_password_123", updated_record.password_hash, updated_record.salt)

    # New password verifies
    assert user_store.verify_password("new_secure_password_456", updated_record.password_hash, updated_record.salt)


def test_forgot_password_sends_email_with_valid_token(client_with_email, services_with_email):
    # Sign up user
    r = client_with_email.post(
        "/api/auth/signup",
        json={"email": "resetme@example.com", "password": "originalpass123", "name": "Reset User", "business_name": "Reset Corp"},
    )
    assert r.status_code == 200, r.text

    # Request password reset
    r = client_with_email.post("/api/auth/forgot-password", json={"email": "resetme@example.com"})
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "If an account with that email exists, we have sent a password reset link."}

    # Verify email was captured by FakeEmailSender
    sent_emails = services_with_email.fake_email_sender.sent
    assert len(sent_emails) == 1
    email = sent_emails[0]
    assert email["to"] == "resetme@example.com"
    assert "Reset your Bizistic password" in email["subject"]
    assert "https://app.example.com/reset-password?token=" in email["html_body"]


def test_forgot_password_nonexistent_email_prevents_enumeration(client_with_email, services_with_email):
    # Request reset for unknown email
    r = client_with_email.post("/api/auth/forgot-password", json={"email": "doesnotexist@example.com"})
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "If an account with that email exists, we have sent a password reset link."}

    # No email should be sent
    assert len(services_with_email.fake_email_sender.sent) == 0


def test_forgot_password_case_insensitive(client_with_email, services_with_email):
    r = client_with_email.post(
        "/api/auth/signup",
        json={"email": "caseuser@example.com", "password": "originalpass123", "name": "Case User", "business_name": "Case Corp"},
    )
    assert r.status_code == 200, r.text

    r = client_with_email.post("/api/auth/forgot-password", json={"email": "CASEUSER@EXAMPLE.COM"})
    assert r.status_code == 200, r.text
    assert len(services_with_email.fake_email_sender.sent) == 1
    assert services_with_email.fake_email_sender.sent[0]["to"] == "caseuser@example.com"


def test_forgot_password_email_send_error_handled_gracefully(client_with_email, services_with_email):
    r = client_with_email.post(
        "/api/auth/signup",
        json={"email": "errorcase@example.com", "password": "originalpass123", "name": "Error User", "business_name": "Error Corp"},
    )
    assert r.status_code == 200, r.text

    def failing_send(*args, **kwargs):
        raise EmailSendError("Upstream SMTP failure")

    with patch.object(services_with_email.fake_email_sender, "send", side_effect=failing_send):
        r = client_with_email.post("/api/auth/forgot-password", json={"email": "errorcase@example.com"})
        assert r.status_code == 200
        assert r.json() == {"message": "If an account with that email exists, we have sent a password reset link."}


def test_reset_password_success_flow(client_with_email, services_with_email):
    # Sign up
    r = client_with_email.post(
        "/api/auth/signup",
        json={"email": "flowuser@example.com", "password": "old_password_123", "name": "Flow User", "business_name": "Flow Corp"},
    )
    assert r.status_code == 200, r.text

    # Request reset
    r = client_with_email.post("/api/auth/forgot-password", json={"email": "flowuser@example.com"})
    assert r.status_code == 200

    # Extract token from store
    user = services_with_email.user_store.get_user_by_email("flowuser@example.com")
    assert user is not None

    sent_body = services_with_email.fake_email_sender.sent[-1]["html_body"]
    token = sent_body.split("token=")[1].split('"')[0]

    # Reset password with valid token
    r = client_with_email.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "brand_new_password_789"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Password has been reset successfully. You can now sign in with your new password."}

    # Old password no longer works
    r_old = client_with_email.post(
        "/api/auth/login",
        json={"email": "flowuser@example.com", "password": "old_password_123"},
    )
    assert r_old.status_code == 401

    # New password works and logs in
    r_new = client_with_email.post(
        "/api/auth/login",
        json={"email": "flowuser@example.com", "password": "brand_new_password_789"},
    )
    assert r_new.status_code == 200
    assert "access_token" in r_new.json()

    # Reusing the same token should fail
    r_reuse = client_with_email.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "another_new_password_111"},
    )
    assert r_reuse.status_code == 400
    assert "already been used" in r_reuse.json()["detail"]


def test_reset_password_invalid_token(client):
    r = client.post(
        "/api/auth/reset-password",
        json={"token": "totally-bogus-token-xyz", "new_password": "newpassword123"},
    )
    assert r.status_code == 400
    assert "Invalid or expired" in r.json()["detail"]


def test_reset_password_expired_token(client, services):
    user = services.user_store.create_user(
        email="expired@example.com",
        password="oldpassword123",
        name="Expired User",
        business_name="Expired Business",
        tenant_id="tenant_expired",
    )
    token = services.password_reset_store.create(user.user_id, lifetime_seconds=-10)  # already expired

    r = client.post(
        "/api/auth/reset-password",
        json={"token": token, "new_password": "newpassword123"},
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()


def test_reset_password_short_password_rejected(client):
    r = client.post(
        "/api/auth/reset-password",
        json={"token": "some-token", "new_password": "123"},
    )
    # Pydantic validation rejects min_length < 6 with 422
    assert r.status_code == 422


def test_static_password_reset_pages(client):
    r_forgot = client.get("/forgot-password")
    assert r_forgot.status_code == 200
    assert "Reset your password" in r_forgot.text
    assert "Account Email" in r_forgot.text

    r_reset = client.get("/reset-password")
    assert r_reset.status_code == 200
    assert "Create new password" in r_reset.text
    assert "New Password" in r_reset.text
