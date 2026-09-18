"""Tests for the public compliance/legal static pages (Contact Us, Shipping
Policy) required for Razorpay live-mode activation. Terms & Conditions,
Privacy Policy, and Cancellation & Refunds are intentionally not covered
here yet — they depend on business/legal facts not yet established."""

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


def test_footer_links_to_compliance_pages(client):
    r = client.get("/")
    assert r.status_code == 200
    assert '/contact' in r.text
    assert '/shipping-policy' in r.text
