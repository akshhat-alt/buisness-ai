"""Structured logging + request tracing (Phase 9).

Every log line emitted anywhere in this app — a route handler, a helper
deep in routers/admin_bot.py, a background cron job — is rendered as one
JSON object carrying the SAME `request_id` for the lifetime of a single
HTTP request, via a contextvar the request-tracing middleware sets once
per request. This is what makes "search the logs for one failing
request" possible: grep one request_id, get every line it produced,
across every module, with no manual plumbing at each call site.

Deliberately NOT a full OpenTelemetry/APM integration — that's real
additional infrastructure (a collector, an exporter, a vendor or
self-hosted backend) this phase doesn't need to justify yet. What's here
is the free, dependency-light half of "observability": structured,
correlatable logs and a request-id trail. Reaching for a tracing SDK is
a reasonable later step if/when multi-service tracing is actually needed.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Populated once per request by RequestContextMiddleware, read by
# JsonLogFormatter for every log line emitted while that request is in
# flight — including from code that has no idea it's running inside a
# request (a store method, a shared helper several calls deep).
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant_id", default=None)

# LogRecord attributes that already exist on every record (from the
# standard logging.LogRecord constructor) — never treated as "extra"
# structured fields when flattening a record into JSON.
_STANDARD_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {"message", "asctime"}


class JsonLogFormatter(logging.Formatter):
    """Renders one JSON object per log line: timestamp, level, logger
    name, message, the current request's id/tenant (if any), plus any
    caller-supplied structured fields passed via `extra={...}`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        tenant_id = tenant_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        if tenant_id:
            payload["tenant_id"] = tenant_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Replaces the root logger's handlers with one JSON stream handler.
    Idempotent — safe to call more than once (e.g. once from create_app
    and once from a test fixture) without accumulating duplicate handlers."""
    root = logging.getLogger()
    root.handlers = [_build_handler()]
    root.setLevel(level)


def _build_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    return handler


_access_logger = logging.getLogger("business_ai.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns every inbound request a trace id (reusing an inbound
    `X-Request-ID` if the caller already has one, e.g. a platform
    fronting proxy — never trusted for anything security-relevant, only
    log correlation), makes it available to every log line produced
    while handling that request via `request_id_var`, echoes it back on
    the response so a client can report "this exact request failed", and
    emits one structured access-log line per request with method, path,
    status, tenant_id (best-effort, from the `tenant_id` query param
    most routes already use), and duration."""

    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        tenant_id = request.query_params.get("tenant_id")
        req_token = request_id_var.set(req_id)
        tenant_token = tenant_id_var.set(tenant_id)
        start = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            _access_logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            try:
                response.headers["X-Request-ID"] = req_id
            except NameError:
                pass  # call_next raised before `response` was ever assigned
            request_id_var.reset(req_token)
            tenant_id_var.reset(tenant_token)
