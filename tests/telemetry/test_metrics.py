"""OpenTelemetry metrics (BRIEF section 11, items 33-36)."""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from prometheus_client import REGISTRY, generate_latest

from s1gw import telemetry
from s1gw.main import create_app
from tests.conftest import (
    SEEN_ATTRIBUTES,
    Gateway,
    GatewayFactory,
    collect_attributes,
    load_fixture,
    make_settings,
    png_b64,
)

SO = "/v1/systemone"
NAME = "01_ticket_triage.request.json"

ALLOWED_ATTRIBUTE_KEYS = {
    # gateway metrics
    "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.request.model", "error.type",
    "gen_ai.token.type", "outcome", "http.response.status_code", "question.type", "reason",
    # http.server.request.duration (stable HTTP semconv from the FastAPI instrumentation)
    "http.request.method", "http.route", "url.scheme", "network.protocol.version",
    "network.protocol.name", "server.address", "server.port", "http.target",
}  # fmt: skip

# Values used by the tests that must never leak into metric attributes.
FORBIDDEN_FRAGMENTS = [
    "is it a bug?", "severity", "team", "jev-latest", "typesafe/jev", "receipt", "logo", "photo",
    "Checkout is blank", "enterprise", "チケット", "café", "secret", "Bearer",
]  # fmt: skip


def ok(name: str = NAME) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("gemini", name.replace(".request", ".response")))


def point_map(gw: Gateway, name: str) -> list[Any]:
    return gw.metrics().get(name, [])


def attrs_of(points: list[Any]) -> list[dict[str, Any]]:
    return [dict(p.attributes) for p in points]


async def test_success_records_all_instruments(upstream: Any, gateway: Gateway) -> None:
    upstream.side_effect = [ok()]
    response = await gateway.client.post(SO, json=load_fixture("systemone", NAME))
    assert response.status_code == 200

    duration = point_map(gateway, "gen_ai.client.operation.duration")
    assert len(duration) == 1 and duration[0].count == 1
    assert dict(duration[0].attributes) == {
        "gen_ai.operation.name": "decide",
        "gen_ai.provider.name": "gcp.gemini",
        "gen_ai.request.model": "gemini-decision-1",
    }

    tokens = point_map(gateway, "gen_ai.client.token.usage")
    assert len(tokens) == 1 and tokens[0].sum == 412
    assert dict(tokens[0].attributes)["gen_ai.token.type"] == "input"

    questions = {
        p.attributes["question.type"]: p.value for p in point_map(gateway, "s1gw.questions")
    }
    assert questions == {"noul": 1, "choice": 1, "score": 1}

    per_request = point_map(gateway, "s1gw.questions_per_request")
    assert per_request[0].count == 1 and per_request[0].sum == 3
    assert list(per_request[0].explicit_bounds) == [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000]

    requests = point_map(gateway, "s1gw.requests")
    assert attrs_of(requests) == [{"outcome": "ok", "http.response.status_code": 200}]
    assert requests[0].value == 1
    assert point_map(gateway, "s1gw.images")[0].value == 0  # startup zero sample only
    assert "s1gw.upstream.retries" not in gateway.metrics()


async def test_http_server_duration_comes_from_instrumentation(
    upstream: Any, gateway: Gateway
) -> None:
    upstream.side_effect = [ok()]
    await gateway.client.post(SO, json=load_fixture("systemone", NAME))
    await gateway.client.get("/healthz")
    await gateway.client.get("/readyz")
    points = point_map(gateway, "http.server.request.duration")
    routes = {p.attributes.get("http.route") for p in points}
    assert routes == {SO}  # probes are excluded


async def test_images_counter(upstream: Any, gateway: Gateway) -> None:
    name = "02_image_assets.request.json"
    upstream.side_effect = [ok(name)]
    await gateway.client.post(SO, json=load_fixture("systemone", name))
    assert point_map(gateway, "s1gw.images")[0].value == 2


async def test_safety_block_is_recorded_as_blocked(upstream: Any, gateway: Gateway) -> None:
    upstream.side_effect = [httpx.Response(400, json={"error": {"code": "safety", "message": "m"}})]
    response = await gateway.client.post(SO, json=load_fixture("systemone", NAME))
    assert response.status_code == 400
    assert attrs_of(point_map(gateway, "s1gw.requests")) == [
        {"outcome": "blocked", "http.response.status_code": 400}
    ]
    duration = attrs_of(point_map(gateway, "gen_ai.client.operation.duration"))
    assert duration[0]["error.type"] == "safety"
    assert "gen_ai.client.token.usage" not in gateway.metrics()


async def test_retries_are_counted_by_reason(upstream: Any, gateway: Gateway) -> None:
    upstream.side_effect = [
        httpx.Response(429),
        httpx.Response(503),
        httpx.TimeoutException("t"),  # third attempt: out of retries, so not counted as a retry
    ]
    response = await gateway.client.post(SO, json=load_fixture("systemone", NAME))
    assert response.status_code == 504
    counts = {p.attributes["reason"]: p.value for p in point_map(gateway, "s1gw.upstream.retries")}
    assert counts == {"429": 1, "503": 1}
    assert attrs_of(point_map(gateway, "s1gw.requests")) == [
        {"outcome": "timeout", "http.response.status_code": 504}
    ]


async def test_connect_and_timeout_retry_reasons(upstream: Any, gateway: Gateway) -> None:
    upstream.side_effect = [httpx.ConnectError("x"), httpx.TimeoutException("t"), ok()]
    await gateway.client.post(SO, json=load_fixture("systemone", NAME))
    counts = {p.attributes["reason"]: p.value for p in point_map(gateway, "s1gw.upstream.retries")}
    assert counts == {"connect": 1, "timeout": 1}


@pytest.mark.parametrize(
    ("setup", "outcome", "status"),
    [
        ("upstream_500", "upstream_error", 502),
        ("upstream_503", "upstream_error", 529),
        ("bad_request", "client_error", 400),
        ("unauthorized", "client_error", 401),
        ("invalid_body", "client_error", 422),
    ],
)
async def test_outcome_classification(
    upstream: Any, make_gateway: GatewayFactory, setup: str, outcome: str, status: int
) -> None:
    gw = await make_gateway(inbound_api_keys="inbound-token-xyz" if setup == "unauthorized" else "")
    body: Any = load_fixture("systemone", NAME)
    if setup == "upstream_500":
        upstream.side_effect = [httpx.Response(500)]
    elif setup == "upstream_503":
        upstream.side_effect = [httpx.Response(503)] * 3
    elif setup == "bad_request":
        body = {
            "model": "m",
            "state": "",
            "questions": {"a": {"type": "noul", "instructions": "i"}},
        }
    elif setup == "invalid_body":
        body = {"model": "m"}
    response = await gw.client.post(SO, json=body)
    assert response.status_code == status
    assert attrs_of(point_map(gw, "s1gw.requests")) == [
        {"outcome": outcome, "http.response.status_code": status}
    ]


async def test_cardinality_guard(upstream: Any, make_gateway: GatewayFactory) -> None:
    """Metric attributes come only from fixed enums/config, never from client-controlled data."""
    gw = await make_gateway(inbound_api_keys="secret-inbound-token", gemini_image_resolution="high")
    auth = {"Authorization": "Bearer secret-inbound-token"}
    unicode_name = "03_unicode_structured.request.json"
    image_name = "02_image_assets.request.json"

    upstream.side_effect = [ok(), ok(image_name), ok(unicode_name)]
    for name in (NAME, image_name, unicode_name):
        assert (
            await gw.client.post(SO, json=load_fixture("systemone", name), headers=auth)
        ).status_code == 200
    upstream.side_effect = [
        httpx.Response(400, json={"error": {"code": "blocklist", "message": "photo"}})
    ]
    await gw.client.post(SO, json=load_fixture("systemone", NAME), headers=auth)
    upstream.side_effect = [httpx.Response(429), httpx.Response(200, json={"bad": "photo"})]
    await gw.client.post(SO, json=load_fixture("systemone", NAME), headers=auth)
    await gw.client.post(SO, json=load_fixture("systemone", NAME))  # 401
    await gw.client.post(
        SO, json={"model": "typesafe/jev-9", "state": "photo", "questions": {}}, headers=auth
    )
    await gw.client.post(
        SO,
        json={
            "model": "typesafe/jev-9",
            "state": "photo",
            "questions": {"severity": {"type": "box", "instructions": "x"}},
            "assets": {"receipt": {"format": "png", "data_base64": png_b64()}},
        },
        headers=auth,
    )

    # Attributes from this gateway plus every gateway created earlier in the whole test session.
    pairs = collect_attributes(gw.reader) + SEEN_ATTRIBUTES
    assert pairs
    keys = {k for k, _ in pairs}
    assert keys <= ALLOWED_ATTRIBUTE_KEYS, keys - ALLOWED_ATTRIBUTE_KEYS
    for key, value in pairs:
        assert not any(fragment in value for fragment in FORBIDDEN_FRAGMENTS), (key, value)
    # Exact enums for the gateway's own attributes.
    values: dict[str, set[str]] = {}
    for key, value in pairs:
        values.setdefault(key, set()).add(value)
    assert values["outcome"] <= {"ok", "client_error", "blocked", "upstream_error", "timeout"}
    assert values["question.type"] <= {"noul", "choice", "score"}
    assert values["reason"] <= {"429", "503", "timeout", "connect"}
    assert values["gen_ai.request.model"] <= {
        "gemini-decision-1",
        "gemini-decision-2",
    }  # config only


async def test_prometheus_exposition_names() -> None:
    reader = telemetry.build_readers(make_settings(otel_metrics_exporter="prometheus"))[0]
    provider = MeterProvider(metric_readers=[reader])
    app = create_app(make_settings(), provider)
    try:
        with __import__("respx").mock(assert_all_called=False) as router:
            router.post("https://gemini.test/v1beta/decisions").side_effect = [ok()]
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                    response = await client.post(SO, json=load_fixture("systemone", NAME))
                    assert response.status_code == 200
        text = generate_latest().decode()
    finally:
        provider.shutdown()  # also unregisters the collector from the global registry
    for name in (
        "s1gw_requests_total",
        "s1gw_questions_total",
        "s1gw_questions_per_request",
        "gen_ai_client_operation_duration_seconds",
        "gen_ai_client_token_usage",
        "http_server_request_duration_seconds",
        "target_info",
    ):
        assert name in text, name
    assert 'outcome="ok"' in text


def test_resource_attributes() -> None:
    settings = make_settings(pod_name="pod-1", pod_namespace="ns-1", otel_service_name="svc")
    attrs = telemetry.build_resource(settings).attributes
    assert attrs["service.name"] == "svc"
    assert attrs["service.version"] == telemetry.service_version() == "0.1.0"
    assert attrs["k8s.pod.name"] == "pod-1" and attrs["k8s.namespace.name"] == "ns-1"
    bare = telemetry.build_resource(make_settings()).attributes
    assert "k8s.pod.name" not in bare and "k8s.namespace.name" not in bare


def test_reader_selection() -> None:
    assert telemetry.build_readers(make_settings(otel_metrics_exporter="none")) == []
    otlp = telemetry.build_readers(
        make_settings(
            otel_metrics_exporter="otlp", otel_exporter_otlp_endpoint="http://collector:4318/"
        )
    )
    assert len(otlp) == 1 and isinstance(otlp[0], PeriodicExportingMetricReader)
    assert otlp[0]._exporter._endpoint == "http://collector:4318/v1/metrics"  # type: ignore[attr-defined]
    default_endpoint = telemetry.build_readers(make_settings(otel_metrics_exporter="otlp"))
    assert len(default_endpoint) == 1
    both = telemetry.build_readers(make_settings(otel_metrics_exporter="otlp,prometheus"))
    assert len(both) == 2
    for reader in both:
        if hasattr(reader, "_collector"):
            REGISTRY.unregister(reader._collector)  # never attached to a provider here
    with pytest.raises(ValueError, match="http/protobuf"):
        telemetry.build_readers(
            make_settings(otel_metrics_exporter="otlp", otel_exporter_otlp_protocol="grpc")
        )


async def test_lifespan_starts_prometheus_server_once_and_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[int] = []
    stopped: list[bool] = []

    class FakeServer:
        def shutdown(self) -> None:
            stopped.append(True)

        def server_close(self) -> None:
            pass

    def fake_start(port: int) -> tuple[FakeServer, object]:
        started.append(port)
        return FakeServer(), object()

    monkeypatch.setattr("prometheus_client.start_http_server", fake_start)
    monkeypatch.setattr("s1gw.main.otel_metrics.set_meter_provider", lambda provider: None)
    readers: list[Any] = []
    original = telemetry.build_readers

    def spy(settings: Any) -> Any:
        built = original(settings)
        readers.extend(built)
        return built

    monkeypatch.setattr(telemetry, "build_readers", spy)
    settings = make_settings(otel_metrics_exporter="prometheus", metrics_port=19464)
    try:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            telemetry.start_prometheus_server(19464)  # a second start is a no-op
            assert started == [19464]
        assert stopped == [True]
        telemetry.stop_prometheus_server()  # already stopped: no-op
    finally:  # the provider's shutdown already unregistered them; this is belt and braces
        for reader in readers:
            with contextlib.suppress(KeyError):
                REGISTRY.unregister(reader._collector)
