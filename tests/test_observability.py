"""Tests for Phase 9's structured logging + request tracing:
JsonLogFormatter's field shaping, and RequestContextMiddleware's
request-id propagation/echo/access-logging behavior."""

from __future__ import annotations

import json
import logging

from business_ai.observability import JsonLogFormatter, request_id_var, tenant_id_var


def _make_record(msg="hello", **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="business_ai.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_produces_valid_json_with_core_fields():
    formatter = JsonLogFormatter()
    line = formatter.format(_make_record("something happened"))
    data = json.loads(line)
    assert data["message"] == "something happened"
    assert data["level"] == "INFO"
    assert data["logger"] == "business_ai.test"
    assert "timestamp" in data


def test_json_formatter_includes_request_and_tenant_id_from_context():
    formatter = JsonLogFormatter()
    req_token = request_id_var.set("req-abc123")
    tenant_token = tenant_id_var.set("salon-a")
    try:
        data = json.loads(formatter.format(_make_record("in a request")))
        assert data["request_id"] == "req-abc123"
        assert data["tenant_id"] == "salon-a"
    finally:
        request_id_var.reset(req_token)
        tenant_id_var.reset(tenant_token)


def test_json_formatter_omits_request_id_outside_a_request():
    formatter = JsonLogFormatter()
    data = json.loads(formatter.format(_make_record("no request in flight")))
    assert "request_id" not in data
    assert "tenant_id" not in data


def test_json_formatter_includes_extra_structured_fields():
    formatter = JsonLogFormatter()
    record = _make_record("request", method="GET", path="/healthz", status_code=200, duration_ms=1.23)
    data = json.loads(formatter.format(record))
    assert data["method"] == "GET"
    assert data["path"] == "/healthz"
    assert data["status_code"] == 200
    assert data["duration_ms"] == 1.23


def test_response_echoes_a_request_id_header(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert "X-Request-ID" in r.headers
    assert len(r.headers["X-Request-ID"]) > 0


def test_response_reuses_an_inbound_request_id_header(client):
    r = client.get("/healthz", headers={"X-Request-ID": "caller-supplied-id-123"})
    assert r.headers["X-Request-ID"] == "caller-supplied-id-123"


def test_each_request_gets_a_distinct_request_id(client):
    r1 = client.get("/healthz")
    r2 = client.get("/healthz")
    assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]


def test_access_log_is_emitted_as_structured_json(client, caplog):
    caplog.set_level(logging.INFO, logger="business_ai.access")
    client.get("/healthz")
    records = [r for r in caplog.records if r.name == "business_ai.access"]
    assert len(records) >= 1
    last = records[-1]
    assert last.method == "GET"
    assert last.path == "/healthz"
    assert last.status_code == 200
    assert isinstance(last.duration_ms, float)
