"""OpenTelemetry metrics setup and instruments (BRIEF section 6).

The MeterProvider is built explicitly (no ``opentelemetry-instrument``), so the configured
exporters are honoured and tests can inject an ``InMemoryMetricReader``.

Cardinality rule: attribute values come only from fixed enums or from configuration. Never put a
client question id, inbound ``model`` string, state content, asset key or error message here.
"""

from __future__ import annotations

import os
import threading
from importlib import metadata
from typing import Any

from opentelemetry.metrics import MeterProvider
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from s1gw.config import Settings

# Emit the stable HTTP semconv (http.server.request.duration, in seconds) from the FastAPI
# instrumentation. Must be set before the instrumentor first runs.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

QUESTIONS_PER_REQUEST_BUCKETS = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000]
HTTP_EXCLUDED_URLS = "healthz,readyz,metrics"

_prometheus_lock = threading.Lock()
_prometheus_server: Any = None


def service_version() -> str:
    try:
        return metadata.version("systemone-gemini-gateway")
    except metadata.PackageNotFoundError:  # pragma: no cover - running from a bare checkout
        return "0.0.0"


def build_resource(settings: Settings) -> Resource:
    attrs: dict[str, str] = {
        "service.name": settings.otel_service_name,
        "service.version": service_version(),
    }
    if settings.pod_name:
        attrs["k8s.pod.name"] = settings.pod_name
    if settings.pod_namespace:
        attrs["k8s.namespace.name"] = settings.pod_namespace
    return Resource.create(attrs)


def build_readers(settings: Settings) -> list[MetricReader]:
    readers: list[MetricReader] = []
    exporters = settings.metrics_exporters
    if "otlp" in exporters:
        if settings.otel_exporter_otlp_protocol != "http/protobuf":
            raise ValueError("only OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf is supported")
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        endpoint = settings.otel_exporter_otlp_endpoint
        exporter = OTLPMetricExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/metrics" if endpoint else None
        )
        readers.append(PeriodicExportingMetricReader(exporter))
    if "prometheus" in exporters:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        readers.append(PrometheusMetricReader())
    return readers


def build_meter_provider(settings: Settings) -> SdkMeterProvider:
    return SdkMeterProvider(
        resource=build_resource(settings), metric_readers=build_readers(settings)
    )


def start_prometheus_server(port: int) -> None:
    """Serve /metrics on its own port. Started at most once per process."""
    global _prometheus_server
    from prometheus_client import start_http_server

    with _prometheus_lock:
        if _prometheus_server is None:
            _prometheus_server, _ = start_http_server(port)


def stop_prometheus_server() -> None:
    global _prometheus_server
    with _prometheus_lock:
        if _prometheus_server is not None:
            _prometheus_server.shutdown()
            _prometheus_server.server_close()
            _prometheus_server = None


class Metrics:
    """The gateway's own instruments. HTTP server duration comes from the FastAPI instrumentor."""

    def __init__(self, provider: MeterProvider, *, model: str) -> None:
        self._model = model
        meter = provider.get_meter("s1gw", service_version())
        self._op_duration = meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="Duration of upstream Gemini Decisions calls, retries included",
        )
        self._tokens = meter.create_histogram(
            "gen_ai.client.token.usage", unit="{token}", description="Input tokens per request"
        )
        self._requests = meter.create_counter("s1gw.requests", description="Gateway requests")
        self._questions = meter.create_counter("s1gw.questions", description="Questions by type")
        self._questions_per_request = meter.create_histogram(
            "s1gw.questions_per_request",
            description="Questions per request",
            explicit_bucket_boundaries_advisory=QUESTIONS_PER_REQUEST_BUCKETS,
        )
        self._images = meter.create_counter("s1gw.images", description="Images forwarded")
        # Export a zero sample at startup so /metrics (and target_info) is non-empty at idle.
        self._images.add(0)
        self._retries = meter.create_counter(
            "s1gw.upstream.retries", description="Upstream retries"
        )

    def _base(self) -> dict[str, str]:
        return {
            "gen_ai.provider.name": "gcp.gemini",
            "gen_ai.request.model": self._model,
        }

    def record_upstream(self, seconds: float, error_type: str | None) -> None:
        attrs = {"gen_ai.operation.name": "decide", **self._base()}
        if error_type:
            attrs["error.type"] = error_type
        self._op_duration.record(seconds, attrs)

    def record_tokens(self, input_tokens: int) -> None:
        self._tokens.record(input_tokens, {"gen_ai.token.type": "input", **self._base()})

    def record_request(self, outcome: str, status: int) -> None:
        self._requests.add(1, {"outcome": outcome, "http.response.status_code": status})

    def record_questions(self, counts: dict[str, int]) -> None:
        for question_type, count in counts.items():
            self._questions.add(count, {"question.type": question_type})
        self._questions_per_request.record(sum(counts.values()))

    def record_images(self, count: int) -> None:
        if count:
            self._images.add(count)

    def record_retry(self, reason: str) -> None:
        self._retries.add(1, {"reason": reason})
