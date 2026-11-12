"""POST /v1/systemone happy paths and validation (BRIEF section 11, items 10, 26)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from tests.conftest import (
    GEMINI_KEY,
    Gateway,
    GatewayFactory,
    fixture_names,
    load_fixture,
    png_b64,
)

SO = "/v1/systemone"


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("gemini", name.replace(".request", ".response")))


async def test_happy_path_end_to_end(upstream: Any, gateway: Gateway) -> None:
    name = "01_ticket_triage.request.json"
    upstream.side_effect = [ok(name)]
    response = await gateway.client.post(
        SO,
        json=load_fixture("systemone", name),
        headers={
            "Authorization": "Bearer inbound-secret",
            "X-Request-Id": "req-abc-123",
            "TypeSafe-Danger-Zone-Images": "true",
            "TypeSafe-Danger-Zone-Long-Context": "true",
        },
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-abc-123"
    assert response.json() == load_fixture("systemone", name.replace(".request", ".response"))

    sent = upstream.calls.last.request
    assert json.loads(sent.content) == load_fixture("gemini", name)
    assert sent.headers["x-goog-api-key"] == GEMINI_KEY
    assert "authorization" not in sent.headers
    assert "x-request-id" not in sent.headers
    assert upstream.call_count == 1


@pytest.mark.parametrize("name", fixture_names())
async def test_all_golden_fixtures_through_the_api(
    upstream: Any, gateway: Gateway, name: str
) -> None:
    upstream.side_effect = [ok(name)]
    response = await gateway.client.post(SO, json=load_fixture("systemone", name))
    assert response.status_code == 200
    assert response.json() == load_fixture("systemone", name.replace(".request", ".response"))
    assert json.loads(upstream.calls.last.request.content) == load_fixture("gemini", name)


async def test_request_id_is_generated_when_absent_or_unusable(
    upstream: Any, gateway: Gateway
) -> None:
    name = "01_ticket_triage.request.json"
    upstream.side_effect = [ok(name), ok(name)]
    body = load_fixture("systemone", name)
    first = await gateway.client.post(SO, json=body)
    assert re.fullmatch(r"[0-9a-f-]{36}", first.headers["x-request-id"])
    second = await gateway.client.post(
        SO, json=body, headers={"X-Request-Id": "bad id with spaces"}
    )
    assert re.fullmatch(r"[0-9a-f-]{36}", second.headers["x-request-id"])


async def test_image_resolution_setting_reaches_upstream(
    upstream: Any, make_gateway: GatewayFactory
) -> None:
    gw = await make_gateway(gemini_image_resolution="low", gemini_model="gemini-decision-2")
    name = "02_image_assets.request.json"
    upstream.side_effect = [ok(name)]
    response = await gw.client.post(SO, json=load_fixture("systemone", name))
    assert response.status_code == 200
    sent = json.loads(upstream.calls.last.request.content)
    assert sent["model"] == "gemini-decision-2"
    assert [b["resolution"] for b in sent["input"] if b["type"] == "image"] == ["low", "low"]
    assert response.json()["model"] == "gemini-decision-2"


async def test_translation_errors_are_400_with_detail_string(
    upstream: Any, gateway: Gateway
) -> None:
    body = {"model": "m", "state": "", "questions": {"a": {"type": "noul", "instructions": "x"}}}
    response = await gateway.client.post(SO, json=body)
    assert response.status_code == 400
    assert response.json() == {"detail": "state must not be empty"}
    assert upstream.call_count == 0


async def test_box_is_400(upstream: Any, gateway: Gateway) -> None:
    body = {"model": "m", "state": "s", "questions": {"a": {"type": "box", "instructions": "x"}}}
    response = await gateway.client.post(SO, json=body)
    assert response.status_code == 400
    assert "box questions are not supported" in response.json()["detail"]
    assert upstream.call_count == 0


async def test_asset_problems_are_400(upstream: Any, gateway: Gateway) -> None:
    body = {
        "model": "m",
        "state": "nothing",
        "questions": {"a": {"type": "noul", "instructions": "x"}},
        "assets": {"photo": {"type": "image", "format": "png", "data_base64": png_b64()}},
    }
    response = await gateway.client.post(SO, json=body)
    assert response.status_code == 400
    assert "asset 'photo' is not referenced" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"model": "m", "state": "s", "questions": {}},  # zero questions
        {"model": "m", "state": "s"},
        {"state": "s", "questions": {"a": {"type": "noul", "instructions": "x"}}},
        {"model": "m", "state": None, "questions": {"a": {"type": "noul", "instructions": "x"}}},
        {"model": "m", "state": "s", "questions": {"a": {"type": "wat", "instructions": "x"}}},
        {
            "model": "m",
            "state": "photo",
            "questions": {"a": {"type": "noul", "instructions": "x"}},
            "assets": {"photo": {"type": "image", "format": "gif", "data_base64": "AAAA"}},
        },
    ],
)
async def test_schema_violations_are_default_422(
    upstream: Any, gateway: Gateway, body: Any
) -> None:
    response = await gateway.client.post(SO, json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and {"loc", "msg", "type"} <= set(detail[0])
    assert upstream.call_count == 0


async def test_invalid_json_is_422(gateway: Gateway) -> None:
    response = await gateway.client.post(
        SO, content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


async def test_1000_questions_pass_and_1001_fail(upstream: Any, gateway: Gateway) -> None:
    def build(n: int) -> dict[str, Any]:
        qs = {f"id {i}": {"type": "noul", "instructions": "x"} for i in range(n)}
        return {"model": "m", "state": "s", "questions": qs}

    answers = {f"q{i}": {"type": "noul", "noul": 0.5, "confidence": 0.5} for i in range(1000)}
    upstream.side_effect = [httpx.Response(200, json={"answers": answers, "usage": {}})]
    assert (await gateway.client.post(SO, json=build(1000))).status_code == 200
    too_many = await gateway.client.post(SO, json=build(1001))
    assert too_many.status_code == 400
    assert upstream.call_count == 1
