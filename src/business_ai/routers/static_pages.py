"""Static HTML page routes + the /static file mount (Phase 9 extraction
from app.py).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from business_ai.constants import STATIC_DIR


def register_static_pages(app: FastAPI, svc, ctx) -> None:

    # -------------------------------------------------------------- static pages
    for route, filename in (
        ("/", "index.html"), ("/login", "login.html"), ("/login.html", "login.html"),
        ("/forgot-password", "forgot-password.html"), ("/forgot-password.html", "forgot-password.html"),
        ("/reset-password", "reset-password.html"), ("/reset-password.html", "reset-password.html"),
        ("/chat", "chat.html"), ("/chat.html", "chat.html"),
        ("/dashboard", "dashboard.html"), ("/dashboard.html", "dashboard.html"),
        ("/onboarding", "onboarding.html"), ("/onboarding.html", "onboarding.html"),
        ("/contact", "contact.html"), ("/contact.html", "contact.html"),
        ("/shipping-policy", "shipping-policy.html"), ("/shipping-policy.html", "shipping-policy.html"),
        ("/terms", "terms.html"), ("/terms.html", "terms.html"),
        ("/privacy", "privacy.html"), ("/privacy.html", "privacy.html"),
        ("/cancellation-refunds", "cancellation-refunds.html"), ("/cancellation-refunds.html", "cancellation-refunds.html"),
        ("/404", "404.html"), ("/404.html", "404.html"),
    ):
        def _make_handler(fname: str):
            def _handler() -> FileResponse:
                return FileResponse(STATIC_DIR / fname)

            return _handler

        app.api_route(route, methods=["GET", "HEAD"])(_make_handler(filename))

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
