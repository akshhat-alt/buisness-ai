"""Tests for the public compliance/legal static pages (Terms & Conditions,
Privacy Policy, Cancellation & Refund Policy, Contact Us, Shipping Policy)
required for Razorpay live-mode activation."""

from __future__ import annotations


def test_contact_page_serves(client):
    r = client.get("/contact")
    assert r.status_code == 200
    assert "support@bizistic.com" in r.text

    # .html alias
    r_alias = client.get("/contact.html")
    assert r_alias.status_code == 200


def test_shipping_policy_page_serves(client):
    r = client.get("/shipping-policy")
    assert r.status_code == 200
    assert "no physical goods" in r.text.lower() or "nothing is shipped" in r.text.lower()

    r_alias = client.get("/shipping-policy.html")
    assert r_alias.status_code == 200


def test_terms_page_serves(client):
    r = client.get("/terms")
    assert r.status_code == 200
    assert "Akshat Wankhade" in r.text
    assert "Indore" in r.text

    r_alias = client.get("/terms.html")
    assert r_alias.status_code == 200


def test_privacy_page_serves(client):
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "Akshat Wankhade" in r.text
    assert "Razorpay" in r.text

    r_alias = client.get("/privacy.html")
    assert r_alias.status_code == 200


def test_cancellation_refunds_page_serves(client):
    r = client.get("/cancellation-refunds")
    assert r.status_code == 200
    assert "No refunds" in r.text

    r_alias = client.get("/cancellation-refunds.html")
    assert r_alias.status_code == 200


def test_footer_links_to_compliance_pages(client):
    r = client.get("/")
    assert r.status_code == 200
    for path in ("/terms", "/privacy", "/cancellation-refunds", "/shipping-policy", "/contact"):
        assert f'href="{path}"' in r.text
