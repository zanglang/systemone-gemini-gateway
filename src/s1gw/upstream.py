"""Gemini Decisions API client with retry policy (BRIEF sections 2.2 and 4)."""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

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

logger = logging.getLogger("s1gw.upstream")

SAFETY_CODES = frozenset(
    {"safety", "prohibited_content", "blocklist", "image_safety", "content_blocked"}
)
BACKOFF_BASE_SECONDS = 0.25
BACKOFF_CAP_SECONDS = 4.0

Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[], float]


@dataclass
class CallStats:
    """Filled in by ``decide`` so callers can log/measure even when the call fails."""

    upstream_status: int | None = None
    retries: int = 0
    retry_reasons: list[str] = field(default_factory=list)


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds form only; HTTP-date values fall back to normal backoff."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class GeminiDecisionsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str,
        max_retries: int,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.random,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/v1beta/decisions"
        self._max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return float(min(retry_after, BACKOFF_CAP_SECONDS))
        # Exponential backoff with full jitter.
        return float(self._jitter() * min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt))

    async def decide(self, payload: dict[str, Any], stats: CallStats) -> Any:
        """POST the payload, retrying on 429/503/timeouts/connect errors. Returns parsed JSON."""
        # Only x-goog-api-key goes out; inbound headers are never forwarded.
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        attempt = 0
        while True:
            retry_after: str | None = None
            reason: str
            failure: GatewayError
            try:
                response = await self._http.post(self._url, json=payload, headers=headers)
            except httpx.TimeoutException:
                reason, failure = "timeout", UpstreamTimeoutError()
            except httpx.TransportError:
                reason, failure = "connect", UpstreamTimeoutError()
            else:
                stats.upstream_status = response.status_code
                if response.status_code == 200:
                    return self._parse_success(response)
                retry_after = response.headers.get("retry-after")
                if response.status_code == 429:
                    reason, failure = "429", UpstreamRateLimitedError(retry_after)
                elif response.status_code == 503:
                    reason, failure = "503", UpstreamOverloadedError()
                else:
                    raise self._map_error(response)
            if attempt >= self._max_retries:
                raise failure
            stats.retries += 1
            stats.retry_reasons.append(reason)
            await self._sleep(self._delay(attempt, _parse_retry_after(retry_after)))
            attempt += 1

    @staticmethod
    def _parse_success(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidUpstreamResponseError() from exc

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "[REDACTED]")

    def _map_error(self, response: httpx.Response) -> GatewayError:
        status = response.status_code
        if status == 400:
            try:
                data = response.json()
            except ValueError:
                data = None
            error = data.get("error") if isinstance(data, dict) else None
            error = error if isinstance(error, dict) else {}
            code, message = error.get("code"), error.get("message")
            message = self._redact(message) if isinstance(message, str) else ""
            if isinstance(code, str) and code in SAFETY_CODES:
                return UpstreamSafetyError(code, message)
            return UpstreamRejectedError(message)
        logger.error("upstream returned unexpected status", extra={"fields": {"status": status}})
        if status in (401, 403, 404):
            return UpstreamConfigError()
        return UpstreamServerError()
