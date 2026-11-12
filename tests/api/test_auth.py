"""Inbound bearer auth (BRIEF section 11, item 30)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from s1gw.auth import extract_bearer, is_authorized
from tests.conftest import GatewayFactory, load_fixture

SO = "/v1/systemone"
NAME = "01_ticket_triage.request.json"


def upstream_ok() -> httpx.Response:
    return httpx.Response(200, json=load_fixture("gemini", NAME.replace(".request", ".response")))


async def test_auth_enabled(upstream: Any, make_gateway: GatewayFactory) -> None:
    gw = await make_gateway(inbound_api_keys="a,b")
    body = load_fixture("systemone", NAME)
    upstream.side_effect = [upstream_ok()]

    missing = await gw.client.post(SO, json=body)
    assert missing.status_code == 401
    assert missing.json() == {"detail": "invalid or missing bearer token"}
    assert missing.headers["www-authenticate"] == "Bearer"
    assert "x-request-id" in missing.headers

    wrong = await gw.client.post(SO, json=body, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    wrong_scheme = await gw.client.post(SO, json=body, headers={"Authorization": "Basic YTph"})
    assert wrong_scheme.status_code == 401
    assert upstream.call_count == 0

    good = await gw.client.post(SO, json=body, headers={"Authorization": "Bearer b"})
    assert good.status_code == 200
    assert upstream.call_count == 1
    assert "authorization" not in upstream.calls.last.request.headers


async def test_auth_checked_before_validation(make_gateway: GatewayFactory) -> None:
    gw = await make_gateway(inbound_api_keys="secret-key")
    response = await gw.client.post(SO, json={"garbage": True})
    assert response.status_code == 401


async def test_auth_disabled_allows_anonymous(upstream: Any, make_gateway: GatewayFactory) -> None:
    gw = await make_gateway()
    upstream.side_effect = [upstream_ok()]
    assert (await gw.client.post(SO, json=load_fixture("systemone", NAME))).status_code == 200


async def test_public_endpoints_need_no_token(make_gateway: GatewayFactory) -> None:
    gw = await make_gateway(inbound_api_keys="secret-key")
    for path in ("/healthz", "/readyz", "/v1/models"):
        assert (await gw.client.get(path)).status_code == 200


@pytest.mark.parametrize(
    ("header", "keys", "expected"),
    [
        (None, [], True),
        (None, ["k"], False),
        ("", ["k"], False),
        ("Bearer k", ["k"], True),
        ("bearer k", ["k"], True),
        ("Bearer  k ", ["k"], True),
        ("Bearer ", ["k"], False),
        ("Bearer", ["k"], False),
        ("Token k", ["k"], False),
        ("Bearer k2", ["k", "k2"], True),
        ("Bearer K", ["k"], False),
    ],
)
def test_is_authorized(header: str | None, keys: list[str], expected: bool) -> None:
    assert is_authorized(header, keys) is expected


def test_extract_bearer() -> None:
    assert extract_bearer("Bearer abc") == "abc"
    assert extract_bearer(None) is None
