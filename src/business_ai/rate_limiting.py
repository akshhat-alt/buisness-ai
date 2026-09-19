"""IP- and tenant-level rate limiting (Phase 9) — a broad abuse guard
that runs in front of EVERY route, layered on top of (never replacing)
the existing per-(tenant, session) question-quota enforcement in
usage_limiter.py.

Why a second layer: `UsageLimiter.check_and_reserve` only ever sees one
(tenant_id, session_id) pair at a time — it has no way to notice "one IP
address is minting a new session_id per request to dodge its own
per-session limit," and no way to cap a tenant's TOTAL request volume
across every session at once. Both of those are real gaps a per-session
counter structurally cannot close, which is exactly what "strengthen IP +
tenant rate limiting" (Phase 9) asks for.

Deliberately in-memory, not SQLite-backed like UsageLimiter's quota
counters: a rate-limit window resetting on a process restart is
correct/expected behavior (there is no "used-up quota" to lose, only a
sliding count of very recent requests), and avoids a disk round-trip on
every single request across the whole app. Fixed-window counters, not a
true sliding log — simpler, and precise enough for an abuse guard whose
job is "stop something clearly excessive," not enforce an exact quota.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


@dataclass
class _Window:
    window_start: float
    count: int
    summary_sent: bool = False


class FixedWindowRateLimiter:
    """A plain, thread-safe, per-key fixed-window counter. One instance
    is reused across both the IP and tenant dimensions (with different
    limits), keyed by whatever string the caller passes in."""

    def __init__(
        self,
        *,
        limit_per_minute: int | None = None,
        limit: int | None = None,
        window_seconds: float = 60.0,
    ) -> None:
        self.limit = limit if limit is not None else (limit_per_minute if limit_per_minute is not None else 0)
        self.limit_per_minute = self.limit
        self.window_seconds = window_seconds
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def check_and_increment(self, key: str, *, now: float | None = None) -> bool:
        """Returns True if this request is allowed (and counts it),
        False if `key` has already hit its limit for the active window."""
        if self.limit <= 0:
            return True  # 0/negative = disabled, never blocks
        now = now if now is not None else time.time()
        with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.window_start >= self.window_seconds:
                self._windows[key] = _Window(window_start=now, count=1, summary_sent=False)
                return True
            if window.count >= self.limit:
                return False
            window.count += 1
            return True

    def check_lead_alert_action(self, key: str, *, now: float | None = None) -> str:
        """Determines lead alert email action for key:
        - 'individual': under limit (count <= limit). Send individual lead email.
        - 'summary': cap hit for the first time in this window (11th lead). Send exactly 1 summary email.
        - 'suppress': cap already hit and summary already sent in this window (12th+ lead). Send no email.

        Window rollover after window_seconds resets count AND summary_sent.
        """
        if self.limit <= 0:
            return "individual"
        now = now if now is not None else time.time()
        with self._lock:
            window = self._windows.get(key)
            if window is None or now - window.window_start >= self.window_seconds:
                self._windows[key] = _Window(window_start=now, count=1, summary_sent=False)
                return "individual"
            if window.count < self.limit:
                window.count += 1
                return "individual"
            if not window.summary_sent:
                window.summary_sent = True
                window.count += 1
                return "summary"
            window.count += 1
            return "suppress"

    def sweep_stale(self, *, older_than_seconds: float | None = None, now: float | None = None) -> int:
        """Drops windows untouched for a while, so a long-running process
        doesn't accumulate one dict entry per IP/tenant ever seen. Not on
        a background timer (this app runs no background threads by
        design — see ARCHITECTURE.md) — called opportunistically by the
        middleware every so often instead."""
        threshold = older_than_seconds if older_than_seconds is not None else max(300.0, self.window_seconds * 2)
        now = now if now is not None else time.time()
        with self._lock:
            stale = [k for k, w in self._windows.items() if now - w.window_start > threshold]
            for k in stale:
                del self._windows[k]
            return len(stale)


def client_ip(request: Request) -> str:
    """Prefers the first hop of `X-Forwarded-For` (this app is meant to
    run behind a platform proxy — Railway — per ARCHITECTURE.md's own
    deployment notes), falling back to the direct connection's address
    for local/dev use where no proxy sits in front. Never trusted for
    anything security-authorizing (isolation/authz already never depends
    on IP anywhere in this app) — only used as a rate-limit bucket key,
    where a spoofed value at worst lets one abusive caller dodge its own
    throttling, not gain access to anything.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Applies BOTH an IP-wide and (when the request names one) a
    tenant-wide fixed-window limit, ahead of any route handler running —
    including the per-(tenant, session) quota check deeper in
    _process_question, which this never replaces."""

    def __init__(self, app, *, ip_limiter: FixedWindowRateLimiter, tenant_limiter: FixedWindowRateLimiter) -> None:
        super().__init__(app)
        self._ip_limiter = ip_limiter
        self._tenant_limiter = tenant_limiter
        self._sweep_counter = 0

    async def dispatch(self, request: Request, call_next):
        self._sweep_counter += 1
        if self._sweep_counter % 500 == 0:
            self._ip_limiter.sweep_stale()
            self._tenant_limiter.sweep_stale()

        ip = client_ip(request)
        if not self._ip_limiter.check_and_increment(ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests from this address. Please slow down and try again shortly."},
            )

        tenant_id = request.query_params.get("tenant_id")
        if tenant_id and not self._tenant_limiter.check_and_increment(tenant_id):
            return JSONResponse(
                status_code=429,
                content={"detail": "This business is receiving an unusually high volume of requests right now. Please try again shortly."},
            )

        return await call_next(request)
