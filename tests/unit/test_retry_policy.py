"""Upstream client retry policy (BRIEF section 11, items 24-25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from s1gw.errors import (
    GatewayError,
    InvalidUpstreamResponseError,
    UpstreamConfigError,
    UpstreamOverloadedError,
    UpstreamRateLimitedError,
    UpstreamRejectedError,
    UpstreamSafetyError,
    UpstreamServerError,
    UpstreamTimeoutError,
)
from s1gw.upstream import CallStats, GeminiDecisionsClient
from tests.conftest import GEMINI_KEY, GEMINI_URL

OK_BODY = {"answers": {}, "usage": {"total_input_tokens": 1}}


class Harness:
    def __init__(self, client: GeminiDecisionsClient, sleeps: list[float]) -> None:
        self.client = client
        self.sleeps = sleeps

    async def call(self) -> tuple[Any, CallStats]:
        stats = CallStats()
        result = await self.client.decide({"model": "m"}, stats)
        return result, stats


@pytest_asyncio.fixture
async def harness(upstream: Any) -> AsyncIterator[Harness]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient() as http:
        client = GeminiDecisionsClient(
            http,
            api_key=GEMINI_KEY,
            base_url="https://gemini.test/",
            max_retries=2,
            sleep=fake_sleep,
            jitter=lambda: 0.5,
        )
        yield Harness(client, sleeps)


async def test_429_429_200_succeeds_with_backoff(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [
        httpx.Response(429),
        httpx.Response(429),
        httpx.Response(200, json=OK_BODY),
    ]
    body, stats = await harness.call()
    assert body == OK_BODY
    assert stats.retries == 2
    assert stats.retry_reasons == ["429", "429"]
    assert stats.upstream_status == 200
    # Full jitter (0.5) of base 0.25 * 2**attempt.
    assert harness.sleeps == [0.5 * 0.25, 0.5 * 0.5]


async def test_retry_after_is_honoured_up_to_the_cap(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(503, headers={"Retry-After": "120"}),
        httpx.Response(200, json=OK_BODY),
    ]
    await harness.call()
    assert harness.sleeps == [1.0, 4.0]


async def test_unparseable_retry_after_falls_back_to_backoff(
    upstream: Any, harness: Harness
) -> None:
    upstream.side_effect = [
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        httpx.Response(429, headers={"Retry-After": "-3"}),
        httpx.Response(200, json=OK_BODY),
    ]
    await harness.call()
    assert harness.sleeps == [0.125, 0.25]


async def test_503_three_times_is_529(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.Response(503)] * 3
    with pytest.raises(UpstreamOverloadedError) as exc:
        await harness.call()
    assert exc.value.status_code == 529
    assert upstream.call_count == 3
    assert len(harness.sleeps) == 2


async def test_429_exhausted_passes_retry_after_through(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.Response(429, headers={"Retry-After": "7"})] * 3
    with pytest.raises(UpstreamRateLimitedError) as exc:
        await harness.call()
    assert exc.value.status_code == 429
    assert exc.value.headers == {"Retry-After": "7"}
    assert exc.value.detail == "rate limited by upstream"


async def test_429_without_retry_after_has_no_header(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.Response(429)] * 3
    with pytest.raises(UpstreamRateLimitedError) as exc:
        await harness.call()
    assert not exc.value.headers


async def test_timeouts_three_times_is_504(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = httpx.ReadTimeout("slow")
    stats = CallStats()
    with pytest.raises(UpstreamTimeoutError) as exc:
        await harness.client.decide({}, stats)
    assert exc.value.status_code == 504
    assert stats.retry_reasons == ["timeout", "timeout"]
    assert stats.upstream_status is None


async def test_connect_errors_are_retried_then_504(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [
        httpx.ConnectError("refused"),
        httpx.ConnectError("refused"),
        httpx.ConnectError("x"),
    ]
    stats = CallStats()
    with pytest.raises(UpstreamTimeoutError):
        await harness.client.decide({}, stats)
    assert stats.retry_reasons == ["connect", "connect"]


async def test_connect_error_then_success(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.ConnectError("refused"), httpx.Response(200, json=OK_BODY)]
    body, stats = await harness.call()
    assert body == OK_BODY and stats.retry_reasons == ["connect"]


async def test_400_is_never_retried(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.Response(400, json={"error": {"code": 400, "message": "bad"}})]
    with pytest.raises(UpstreamRejectedError) as exc:
        await harness.call()
    assert upstream.call_count == 1
    assert harness.sleeps == []
    assert exc.value.detail == "upstream rejected request: bad"


async def test_500_is_502_without_retry(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.Response(500, text="boom")]
    with pytest.raises(UpstreamServerError) as exc:
        await harness.call()
    assert exc.value.status_code == 502
    assert upstream.call_count == 1


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_auth_and_not_found_are_config_errors(
    upstream: Any, harness: Harness, status: int
) -> None:
    upstream.side_effect = [httpx.Response(status, json={"error": {"message": GEMINI_KEY}})]
    with pytest.raises(UpstreamConfigError) as exc:
        await harness.call()
    assert exc.value.status_code == 502
    assert exc.value.detail == "upstream configuration error"
    assert upstream.call_count == 1


async def test_other_4xx_is_502(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.Response(418)]
    with pytest.raises(UpstreamServerError):
        await harness.call()


@pytest.mark.parametrize(
    "code", ["safety", "prohibited_content", "blocklist", "image_safety", "content_blocked"]
)
async def test_safety_codes(upstream: Any, harness: Harness, code: str) -> None:
    upstream.side_effect = [
        httpx.Response(400, json={"error": {"code": code, "message": "blocked"}})
    ]
    with pytest.raises(UpstreamSafetyError) as exc:
        await harness.call()
    assert exc.value.detail == {"code": code, "message": "blocked"}
    assert exc.value.outcome == "blocked"
    assert exc.value.error_type == code


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, text="not json"),
        httpx.Response(400, json=["x"]),
        httpx.Response(400, json={"error": "str"}),
        httpx.Response(400, json={"error": {"code": 400, "message": 5}}),
    ],
)
async def test_400_with_odd_bodies_is_a_plain_rejection(
    upstream: Any, harness: Harness, response: httpx.Response
) -> None:
    upstream.side_effect = [response]
    with pytest.raises(UpstreamRejectedError) as exc:
        await harness.call()
    assert exc.value.detail == "upstream rejected request: "


async def test_400_message_never_contains_the_api_key(upstream: Any, harness: Harness) -> None:
    body = {"error": {"code": "x", "message": f"key {GEMINI_KEY} is bad"}}
    upstream.side_effect = [httpx.Response(400, json=body)]
    with pytest.raises(GatewayError) as exc:
        await harness.call()
    assert GEMINI_KEY not in str(exc.value.detail)


async def test_200_with_invalid_json_is_502(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.Response(200, text="<html>")]
    with pytest.raises(InvalidUpstreamResponseError):
        await harness.call()


async def test_request_shape_and_headers(upstream: Any, harness: Harness) -> None:
    upstream.side_effect = [httpx.Response(200, json=OK_BODY)]
    await harness.call()
    request = upstream.calls.last.request
    assert str(request.url) == GEMINI_URL
    assert request.headers["x-goog-api-key"] == GEMINI_KEY
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers


async def test_zero_retries_means_one_attempt(upstream: Any) -> None:
    upstream.side_effect = [httpx.Response(503)]
    async with httpx.AsyncClient() as http:
        client = GeminiDecisionsClient(
            http, api_key="k", base_url="https://gemini.test", max_retries=0
        )
        with pytest.raises(UpstreamOverloadedError):
            await client.decide({}, CallStats())
    assert upstream.call_count == 1
