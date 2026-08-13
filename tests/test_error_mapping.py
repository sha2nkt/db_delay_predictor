"""Upstream failure taxonomy -> HTTP status mapping, per class:
client over its own budget gets 429 elsewhere; bahn.de trouble is 503 with
Retry-After; only an unusable upstream response is a 502."""

from app import bahn_api
from app.main import _upstream_http_error


def test_upstream_rate_limit_maps_to_503_with_retry_after():
    exc = _upstream_http_error(bahn_api.UpstreamRateLimited("x", retry_after=42))
    assert exc.status_code == 503
    assert exc.headers["Retry-After"] == "42"


def test_circuit_open_maps_to_503_with_remaining_cooldown():
    exc = _upstream_http_error(bahn_api.UpstreamUnavailable("x", retry_after=17.3))
    assert exc.status_code == 503
    assert exc.headers["Retry-After"] == "18"


def test_unavailable_without_hint_gets_default_retry_after():
    exc = _upstream_http_error(bahn_api.UpstreamUnavailable("x"))
    assert exc.status_code == 503
    assert int(exc.headers["Retry-After"]) >= 1


def test_blocked_maps_to_503():
    exc = _upstream_http_error(bahn_api.UpstreamBlocked("x"))
    assert exc.status_code == 503


def test_protocol_error_maps_to_502():
    exc = _upstream_http_error(bahn_api.UpstreamProtocolError("x"))
    assert exc.status_code == 502


def test_details_expose_no_internals():
    for e in (bahn_api.UpstreamRateLimited("secret-cookie=1"),
              bahn_api.UpstreamUnavailable("https://internal"),
              bahn_api.UpstreamProtocolError("akamai says no")):
        detail = _upstream_http_error(e).detail
        assert "cookie" not in detail
        assert "http" not in detail.lower().replace("bahn.de", "")
        assert "akamai" not in detail.lower()
