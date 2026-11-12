"""Opt-in smoke test against the real Gemini Decisions API (BRIEF section 11, item 38).

Runs only with RUN_LIVE=1 and GEMINI_API_KEY set (see .github/workflows/live.yml). With
RECORD_FIXTURES=1 the raw upstream response is written to tests/fixtures/gemini/live_*.json.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from s1gw.main import create_app
from tests.conftest import FIXTURES, load_fixture, make_settings, png_b64

# Captured at import: the autouse env-cleaning fixture removes it from os.environ per test.
LIVE_KEY = os.environ.get("GEMINI_API_KEY")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (LIVE_KEY and os.environ.get("RUN_LIVE") == "1"),
        reason="set RUN_LIVE=1 and GEMINI_API_KEY to run live tests",
    ),
]


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.bodies: list[Any] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        await response.aread()
        self.bodies.append(json.loads(response.content))
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )


async def test_live_fixture_one_with_an_image() -> None:
    assert LIVE_KEY
    body = load_fixture("systemone", "01_ticket_triage.request.json")
    body["state"]["screenshot"] = "`screenshot`"
    body["assets"] = {"screenshot": {"type": "image", "format": "png", "data_base64": png_b64()}}
    recorder = RecordingTransport()
    settings = make_settings(
        gemini_api_key=LIVE_KEY, gemini_base_url="https://generativelanguage.googleapis.com"
    )
    app = create_app(settings, transport=recorder)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=60
        ) as client,
    ):
        response = await client.post("/v1/systemone", json=body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["model"] == "gemini-decision-1"
    assert list(out["answers"]) == ["is it a bug?", "severity", "team"]
    assert out["answers"]["is it a bug?"]["type"] == "noul"
    team = out["answers"]["team"]
    assert team["choice"] in {"payments", "web"}
    assert abs(sum(team["probabilities"].values()) - 1) < 1e-6
    score = out["answers"]["severity"]
    assert set(score["legend"]) == {"0", "1", "2"} and set(score["probabilities"]) == {
        "0",
        "1",
        "2",
    }
    assert out["usage"]["input_tokens"] > 0 and out["usage"]["output_tokens"] == 0

    if os.environ.get("RECORD_FIXTURES") == "1":
        path = FIXTURES / "gemini" / "live_01_ticket_triage_with_image.response.json"
        path.write_text(json.dumps(recorder.bodies[-1], indent=2) + "\n")
