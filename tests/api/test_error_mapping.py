"""Error mapping (BRIEF section 4; tests 27-29, 32)."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import httpx
import pytest

from s1gw.main import create_app
from tests.conftest import GEMINI_KEY, Gateway, GatewayFactory, load_fixture

SO = "/v1/systemone"
NAME = "01_ticket_triage.request.json"


def body() -> dict[str, Any]:
    result: dict[str, Any] = load_fixture("systemone", NAME)
    return result


@pytest.mark.parametrize(
    "code", ["safety", "prohibited_content", "blocklist", "image_safety", "content_blocked"]
)
async def test_upstream_safety_codes(upstream: Any, gateway: Gateway, code: str) -> None:
    upstream.side_effect = [
        httpx.Response(400, json={"error": {"code": code, "message": "Content was blocked"}})
    ]
    response = await gateway.client.post(SO, json=body())
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": code, "message": "Content was blocked"}}


async def test_upstream_other_400(upstream: Any, gateway: Gateway) -> None:
    upstream.side_effect = [
        httpx.Response(400, json={"error": {"code": 400, "message": "too many levels"}})
    ]
    response = await gateway.client.post(SO, json=body())
    assert response.status_code == 400
    assert response.json() == {"detail": "upstream rejected request: too many levels"}


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_upstream_config_errors_hide_key_and_body(
    upstream: Any, gateway: Gateway, status: int
) -> None:
    upstream.side_effect = [
        httpx.Response(status, text=f"secret upstream body mentioning {GEMINI_KEY}")
    ]
    response = await gateway.client.post(SO, json=body())
    assert response.status_code == 502
    assert response.json() == {"detail": "upstream configuration error"}
    assert GEMINI_KEY not in response.text
    assert "secret upstream body" not in response.text


@pytest.mark.parametrize(
    ("responses", "status", "detail"),
    [
        ([httpx.Response(429, headers={"Retry-After": "3"})] * 3, 429, "rate limited by upstream"),
        ([httpx.Response(503)] * 3, 529, "upstream overloaded"),
        ([httpx.Response(500)], 502, "upstream error"),
        ([httpx.Response(200, text="not json")], 502, "invalid upstream response"),
        ([httpx.Response(200, json={"nope": 1})], 502, "invalid upstream response"),
        ([httpx.TimeoutException("t")] * 3, 504, "upstream timeout"),
        ([httpx.ConnectError("c")] * 3, 504, "upstream timeout"),
    ],
)
async def test_upstream_failure_mapping(
    upstream: Any, gateway: Gateway, responses: list[Any], status: int, detail: str
) -> None:
    upstream.side_effect = responses
    response = await gateway.client.post(SO, json=body())
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "x-request-id" in response.headers
    if status == 429:
        assert response.headers["retry-after"] == "3"


async def test_missing_answer_is_502(upstream: Any, gateway: Gateway) -> None:
    resp = load_fixture("gemini", NAME.replace(".request", ".response"))
    del resp["answers"]["q2"]
    upstream.side_effect = [httpx.Response(200, json=resp)]
    response = await gateway.client.post(SO, json=body())
    assert response.status_code == 502
    assert response.json() == {"detail": "upstream returned no answer for 'team'"}


async def test_multipart_is_415(upstream: Any, gateway: Gateway) -> None:
    response = await gateway.client.post(
        SO,
        files={"request": ("request.json", b"{}", "application/json")},
    )
    assert response.status_code == 415
    assert response.json() == {
        "detail": "multipart requests are not supported; send assets as JSON"
    }
    assert upstream.call_count == 0


async def test_oversized_body_with_content_length_is_413(
    upstream: Any, make_gateway: GatewayFactory
) -> None:
    gw = await make_gateway(max_body_bytes=200)
    response = await gw.client.post(
        SO, content=b"x" * 300, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


async def test_oversized_chunked_body_is_413(upstream: Any, make_gateway: GatewayFactory) -> None:
    gw = await make_gateway(max_body_bytes=200)

    async def chunks() -> Any:
        for _ in range(3):
            yield b"x" * 100

    request = gw.client.build_request(
        "POST", SO, content=chunks(), headers={"content-type": "application/json"}
    )
    assert "content-length" not in request.headers  # genuinely chunked
    response = await gw.client.send(request)
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}
    assert upstream.call_count == 0


async def test_body_exactly_at_the_limit_is_accepted(
    upstream: Any, make_gateway: GatewayFactory
) -> None:
    payload = b'{"model":"m","state":"s","questions":{"a":{"type":"noul","instructions":"x"}}}'
    gw = await make_gateway(max_body_bytes=len(payload))
    upstream.side_effect = [
        httpx.Response(
            200, json={"answers": {"q0": {"type": "noul", "noul": 0.5, "confidence": 1}}}
        )
    ]
    response = await gw.client.post(
        SO, content=payload, headers={"content-type": "application/json"}
    )
    assert response.status_code == 200


async def test_startup_without_api_key_fails_clearly() -> None:
    app = create_app()
    with pytest.raises(RuntimeError, match="invalid configuration: GEMINI_API_KEY"):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover


async def test_startup_with_empty_api_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    app = create_app()
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover


async def test_startup_rejects_bad_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "statsd")
    app = create_app()
    with pytest.raises(RuntimeError, match="OTEL_METRICS_EXPORTER"):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover


def test_module_import_needs_no_environment() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import s1gw.main"], env={}, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
