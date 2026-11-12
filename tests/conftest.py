"""Shared fixtures and helpers."""

from __future__ import annotations

import base64
import io
import json
import struct
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from s1gw.config import Settings
from s1gw.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
GEMINI_URL = "https://gemini.test/v1beta/decisions"
GEMINI_KEY = "test-gemini-key-0123456789"

ENV_VARS = [
    "GEMINI_API_KEY", "GEMINI_BASE_URL", "GEMINI_MODEL", "GEMINI_IMAGE_RESOLUTION",
    "INBOUND_API_KEYS", "UPSTREAM_TIMEOUT_SECONDS", "UPSTREAM_MAX_RETRIES", "MAX_BODY_BYTES",
    "ADVERTISED_MODELS", "LOG_LEVEL", "LOG_FORMAT", "OTEL_SERVICE_NAME", "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_PROTOCOL", "METRICS_PORT", "PORT",
    "POD_NAME", "POD_NAMESPACE",
]  # fmt: skip

# Every metric attribute seen in any gateway created by the test suite (cardinality guard).
SEEN_ATTRIBUTES: list[tuple[str, str]] = []


def png_bytes(rgb: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """A real 1x1 PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb)))
        + chunk(b"IEND", b"")
    )


def png_b64(rgb: tuple[int, int, int] = (255, 0, 0)) -> str:
    return base64.b64encode(png_bytes(rgb)).decode()


def load_fixture(kind: str, name: str) -> Any:
    return json.loads((FIXTURES / kind / name).read_text())


def fixture_names() -> list[str]:
    return sorted(p.name for p in (FIXTURES / "systemone").glob("*.request.json"))


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "gemini_api_key": GEMINI_KEY,
        "gemini_base_url": "https://gemini.test",
        "otel_metrics_exporter": "none",
        "log_level": "INFO",
        "upstream_max_retries": 2,
    }
    values.update(overrides)
    return Settings(**values)


def collect_attributes(reader: InMemoryMetricReader) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    data = reader.get_metrics_data()
    for resource_metrics in data.resource_metrics if data else []:
        for scope in resource_metrics.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    for key, value in (point.attributes or {}).items():
                        found.append((key, str(value)))
    return found


@dataclass
class Gateway:
    app: Any
    client: httpx.AsyncClient
    reader: InMemoryMetricReader
    settings: Settings
    sleeps: list[float] = field(default_factory=list)

    def metrics(self) -> dict[str, list[Any]]:
        """metric name -> data points."""
        out: dict[str, list[Any]] = {}
        data = self.reader.get_metrics_data()
        for resource_metrics in data.resource_metrics if data else []:
            for scope in resource_metrics.scope_metrics:
                for metric in scope.metrics:
                    out.setdefault(metric.name, []).extend(metric.data.data_points)
        return out


GatewayFactory = Callable[..., Awaitable[Gateway]]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def upstream() -> Any:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(GEMINI_URL)
        yield route


@pytest_asyncio.fixture
async def make_gateway(upstream: Any) -> AsyncIterator[GatewayFactory]:
    stack = AsyncExitStack()
    created: list[Gateway] = []

    async def factory(**overrides: Any) -> Gateway:
        settings = make_settings(**overrides)
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        app = create_app(settings, provider, sleep=fake_sleep, jitter=lambda: 0.5)
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")
        )
        gw = Gateway(app, client, reader, settings, sleeps)
        created.append(gw)
        return gw

    yield factory
    for gw in created:
        SEEN_ATTRIBUTES.extend(collect_attributes(gw.reader))
    await stack.aclose()


@pytest_asyncio.fixture
async def gateway(make_gateway: GatewayFactory) -> Gateway:
    return await make_gateway()


def json_log_lines(text: str) -> list[dict[str, Any]]:
    lines = []
    for raw in io.StringIO(text):
        raw = raw.strip()
        if raw.startswith("{"):
            lines.append(json.loads(raw))
    return lines


@pytest.fixture(autouse=True)
def _restore_logging() -> Any:
    import logging

    root = logging.getLogger()
    level = root.level
    yield
    for handler in [h for h in root.handlers if getattr(h, "_s1gw_handler", False)]:
        root.removeHandler(handler)
    root.setLevel(level)


# --- 100% coverage gate for translate.py -----------------------------------------------------
# pytest-cov's --cov-fail-under (pyproject addopts) enforces >= 90% overall. translate.py must be
# at 100%, which cov-fail-under cannot express per file, so this hook checks it at session end and
# fails the run. It only applies when the translation test modules ran in full (no -k filter).
TRANSLATE_MODULES = ("test_translate_request", "test_translate_assets", "test_translate_response")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    plugin = config.pluginmanager.get_plugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    if controller is None or config.getoption("keyword"):
        return
    ran = {item.nodeid for item in session.items}
    if not all(any(module in nodeid for nodeid in ran) for module in TRANSLATE_MODULES):
        return
    translate_path = Path(__file__).parents[1] / "src" / "s1gw" / "translate.py"
    _, _, _, missing, _ = controller.cov.analysis2(str(translate_path))
    if missing:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        message = f"COVERAGE GATE: src/s1gw/translate.py must be 100%; missing lines {missing}"
        if reporter is not None:
            reporter.write_line(message, red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
