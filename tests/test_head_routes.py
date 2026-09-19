"""Tests for HEAD method support on public static pages and health check."""

def test_head_healthz(client):
    res = client.head("/healthz")
    assert res.status_code == 200


def test_head_static_routes(client):
    routes = [
        "/",
        "/login",
        "/login.html",
        "/forgot-password",
        "/forgot-password.html",
        "/reset-password",
        "/reset-password.html",
        "/chat",
        "/chat.html",
        "/dashboard",
        "/dashboard.html",
        "/onboarding",
        "/onboarding.html",
        "/contact",
        "/contact.html",
        "/shipping-policy",
        "/shipping-policy.html",
        "/terms",
        "/terms.html",
        "/privacy",
        "/privacy.html",
        "/cancellation-refunds",
        "/cancellation-refunds.html",
        "/404",
        "/404.html",
    ]
    for route in routes:
        res = client.head(route)
        assert res.status_code == 200, f"HEAD {route} failed with {res.status_code}"
