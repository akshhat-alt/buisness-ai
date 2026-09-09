"""Unit tests for the SSRF guard and tenant-id validation — the boundary
between a business owner's "add my website" request and this server
making an arbitrary outbound HTTP call."""

from __future__ import annotations

import pytest

from business_ai.security import (
    InvalidTenantIdError,
    UnsafeUrlError,
    assert_public_http_url,
    validate_tenant_id,
)


def test_public_https_url_is_allowed():
    assert_public_http_url("https://example.com/about")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://metadata.google.internal/",
        "ftp://example.com/",
        "file:///etc/passwd",
    ],
)
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(UnsafeUrlError):
        assert_public_http_url(url)


def test_url_without_hostname_is_rejected():
    with pytest.raises(UnsafeUrlError):
        assert_public_http_url("https:///no-host")


def test_valid_tenant_id_normalizes_to_lowercase():
    assert validate_tenant_id("Priya-Salon") == "priya-salon"


@pytest.mark.parametrize("bad_id", ["", "a", "Has Spaces", "semi;colon", "../etc"])
def test_invalid_tenant_ids_are_rejected(bad_id):
    with pytest.raises(InvalidTenantIdError):
        validate_tenant_id(bad_id)
