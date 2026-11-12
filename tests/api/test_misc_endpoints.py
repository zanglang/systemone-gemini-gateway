"""Models, health and readiness (BRIEF section 11, item 31)."""

from __future__ import annotations

from typing import Any

import httpx

from s1gw.main import create_app
from tests.conftest import Gateway, GatewayFactory


async def test_models_lists_advertised_models(make_gateway: GatewayFactory) -> None:
    gw = await make_gateway(advertised_models="jev-latest, gemini-decision-1 ,extra")
    response = await gw.client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["jev-latest", "gemini-decision-1", "extra"]
    assert all(m["object"] == "model" for m in body["data"])


async def test_models_default(gateway: Gateway) -> None:
    ids = [m["id"] for m in (await gateway.client.get("/v1/models")).json()["data"]]
    assert ids == ["jev-latest", "gemini-decision-1"]


async def test_health_and_ready_make_no_upstream_calls(upstream: Any, gateway: Gateway) -> None:
    for path in ("/healthz", "/readyz"):
        response = await gateway.client.get(path)
        assert response.status_code == 200
        assert "x-request-id" in response.headers
    assert upstream.call_count == 0


async def test_not_ready_before_startup() -> None:
    app = create_app()  # lifespan has not run
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 503
        assert (await c.post("/v1/systemone", json={})).status_code == 503


async def test_not_ready_after_shutdown(upstream: Any) -> None:
    from tests.conftest import make_settings

    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        assert app.state.ready is True
    assert app.state.ready is False


async def test_middleware_passes_non_http_scopes_through() -> None:
    from s1gw.main import GatewayMiddleware

    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    await GatewayMiddleware(inner)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]
