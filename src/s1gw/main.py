"""FastAPI application factory.

``s1gw.main:app`` imports cleanly with no environment variables set: settings are loaded and
validated in the lifespan, so a missing ``GEMINI_API_KEY`` makes *startup* fail with a clear error.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import metrics as otel_metrics
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import MeterProvider
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from s1gw import telemetry
from s1gw.auth import is_authorized
from s1gw.config import Settings
from s1gw.errors import (
    GatewayError,
    PayloadTooLargeError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
)
from s1gw.logging import setup_logging
from s1gw.models_systemone import SystemOneRequest
from s1gw.telemetry import Metrics
from s1gw.translate import translate_request, translate_response
from s1gw.upstream import CallStats, GeminiDecisionsClient, Jitter, Sleep

logger = logging.getLogger("s1gw")
access_logger = logging.getLogger("s1gw.access")

SYSTEMONE_PATH = "/v1/systemone"
QUIET_PATHS = {"/healthz", "/readyz"}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:/=+-]{1,128}$")


@dataclass
class RequestInfo:
    """Per-request facts shared between the endpoint and the middleware."""

    request_id: str
    question_count: int = 0
    image_count: int = 0
    upstream_status: int | None = None
    retries: int = 0
    outcome: str | None = None


def _outcome_for_status(status: int) -> str:
    if status < 400:
        return "ok"
    return "client_error" if status < 500 else "upstream_error"


class GatewayMiddleware:
    """Pure ASGI middleware: request id, auth, size/media-type limits, access log, metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _headers(scope: Scope) -> dict[str, str]:
        return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}

    def _precheck(
        self, scope: Scope, headers: dict[str, str], settings: Settings
    ) -> GatewayError | None:
        if scope["method"] == "POST" and scope["path"] == SYSTEMONE_PATH:
            if not is_authorized(headers.get("authorization"), settings.inbound_keys):
                return UnauthorizedError()
            if headers.get("content-type", "").lower().startswith("multipart/form-data"):
                return UnsupportedMediaTypeError()
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > settings.max_body_bytes:
            return PayloadTooLargeError()
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        app_state = scope["app"].state
        headers = self._headers(scope)
        inbound_id = headers.get("x-request-id", "")
        request_id = inbound_id if _REQUEST_ID_RE.match(inbound_id) else str(uuid.uuid4())
        info = RequestInfo(request_id)
        scope["s1gw"] = info
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message["headers"], (b"x-request-id", request_id.encode())]
            await send(message)

        try:
            ready = getattr(app_state, "ready", False)
            settings: Settings | None = getattr(app_state, "settings", None)
            if not ready or settings is None:
                if scope["path"] == "/healthz":
                    await self.app(scope, receive, send_wrapper)
                else:
                    await JSONResponse({"detail": "service not ready"}, 503)(
                        scope, receive, send_wrapper
                    )
                return
            rejection = self._precheck(scope, headers, settings)
            if rejection is not None:
                info.outcome = rejection.outcome
                response = JSONResponse(
                    {"detail": rejection.detail}, rejection.status_code, rejection.headers
                )
                await response(scope, receive, send_wrapper)
                return

            received = 0

            async def limited_receive() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > settings.max_body_bytes:
                        raise PayloadTooLargeError()
                return message

            await self.app(scope, limited_receive, send_wrapper)
        finally:
            self._finish(scope, app_state, info, status, time.perf_counter() - started)

    @staticmethod
    def _finish(
        scope: Scope, app_state: Any, info: RequestInfo, status: int, seconds: float
    ) -> None:
        outcome = info.outcome or _outcome_for_status(status)
        metrics: Metrics | None = getattr(app_state, "metrics", None)
        if metrics is not None and scope["method"] == "POST" and scope["path"] == SYSTEMONE_PATH:
            metrics.record_request(outcome, status)
        level = logging.DEBUG if scope["path"] in QUIET_PATHS else logging.INFO
        access_logger.log(
            level,
            "request",
            extra={
                "fields": {
                    "request_id": info.request_id,
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round(seconds * 1000, 2),
                    "question_count": info.question_count,
                    "image_count": info.image_count,
                    "upstream_status": info.upstream_status,
                    "retries": info.retries,
                }
            },
        )


def _config_error(exc: ValidationError) -> RuntimeError:
    # Field names only: error "input" values may contain secrets.
    problems = "; ".join(
        f"{str(err['loc'][0]).upper()}: {err['msg']}" if err["loc"] else err["msg"]
        for err in exc.errors()
    )
    return RuntimeError(f"invalid configuration: {problems}")


def create_app(
    settings: Settings | None = None,
    meter_provider: MeterProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    sleep: Sleep | None = None,
    jitter: Jitter | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            cfg = settings or Settings()
        except ValidationError as exc:
            raise _config_error(exc) from exc
        setup_logging(cfg.log_level, cfg.log_format, cfg.secrets)
        if not cfg.inbound_keys:
            logger.warning("INBOUND_API_KEYS is empty: inbound authentication is DISABLED")

        owns_provider = meter_provider is None
        provider: MeterProvider
        if meter_provider is None:
            sdk_provider = telemetry.build_meter_provider(cfg)
            otel_metrics.set_meter_provider(sdk_provider)
            provider = sdk_provider
            if "prometheus" in cfg.metrics_exporters:
                telemetry.start_prometheus_server(cfg.metrics_port)
        else:
            provider = meter_provider

        http = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(cfg.upstream_timeout_seconds)
        )
        extra: dict[str, Any] = {}
        if sleep is not None:
            extra["sleep"] = sleep
        if jitter is not None:
            extra["jitter"] = jitter
        app.state.settings = cfg
        app.state.metrics = Metrics(provider, model=cfg.gemini_model)
        app.state.client = GeminiDecisionsClient(
            http,
            api_key=cfg.gemini_api_key,
            base_url=cfg.gemini_base_url,
            max_retries=cfg.upstream_max_retries,
            **extra,
        )
        app.state.ready = True
        logger.info(
            "gateway started",
            extra={"fields": {"model": cfg.gemini_model, "auth": bool(cfg.inbound_keys)}},
        )
        try:
            yield
        finally:
            app.state.ready = False
            await http.aclose()
            if owns_provider:
                telemetry.stop_prometheus_server()
                getattr(provider, "shutdown", lambda: None)()

    app = FastAPI(title="systemone-gemini-gateway", lifespan=lifespan)
    app.add_middleware(GatewayMiddleware)
    FastAPIInstrumentor.instrument_app(
        app, meter_provider=meter_provider, excluded_urls=telemetry.HTTP_EXCLUDED_URLS
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        # Never calls Gemini: probes must not burn quota.
        return {"status": "ready"}

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "owned_by": "systemone-gemini-gateway"}
                for name in request.app.state.settings.models
            ],
        }

    @app.post(SYSTEMONE_PATH)
    async def systemone(body: SystemOneRequest, request: Request) -> JSONResponse:
        state = request.app.state
        cfg: Settings = state.settings
        metrics: Metrics = state.metrics
        info: RequestInfo = request.scope["s1gw"]
        logger.debug("inbound model %s", body.model)
        info.question_count = len(body.questions)

        try:
            translated = translate_request(
                body, model=cfg.gemini_model, image_resolution=cfg.gemini_image_resolution
            )
        except GatewayError as exc:
            info.outcome = exc.outcome
            raise
        info.image_count = translated.image_count
        metrics.record_questions(dict(Counter(translated.question_types.values())))
        metrics.record_images(translated.image_count)

        stats = CallStats()
        started = time.perf_counter()
        try:
            raw = await state.client.decide(translated.payload, stats)
            result = translate_response(translated, raw, model=cfg.gemini_model)
        except GatewayError as exc:
            info.outcome = exc.outcome
            metrics.record_upstream(time.perf_counter() - started, exc.error_type)
            raise
        else:
            metrics.record_upstream(time.perf_counter() - started, None)
            metrics.record_tokens(result["usage"]["input_tokens"])
        finally:
            info.upstream_status = stats.upstream_status
            info.retries = stats.retries
            for reason in stats.retry_reasons:
                metrics.record_retry(reason)
        return JSONResponse(result)

    return app


app = create_app()
