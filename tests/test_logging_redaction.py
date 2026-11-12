"""Logging never leaks request content or secrets (BRIEF section 11, item 37)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import httpx
import pytest

from s1gw.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    TextFormatter,
    setup_logging,
)
from tests.conftest import GEMINI_KEY, GatewayFactory, json_log_lines, load_fixture, png_b64

SO = "/v1/systemone"
INBOUND = "inbound-token-abcdef123456"


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=load_fixture("gemini", name.replace(".request", ".response")))


async def test_no_sensitive_content_in_any_log_record(
    upstream: Any,
    make_gateway: GatewayFactory,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gw = await make_gateway(inbound_api_keys=INBOUND, log_level="DEBUG")
    headers = {"Authorization": f"Bearer {INBOUND}"}
    image_name = "02_image_assets.request.json"
    name = "01_ticket_triage.request.json"

    upstream.side_effect = [ok(name), ok(image_name)]
    assert (
        await gw.client.post(SO, json=load_fixture("systemone", name), headers=headers)
    ).status_code == 200
    assert (
        await gw.client.post(SO, json=load_fixture("systemone", image_name), headers=headers)
    ).status_code == 200
    upstream.side_effect = [
        httpx.Response(400, json={"error": {"code": "safety", "message": "blocked content"}})
    ]
    assert (
        await gw.client.post(SO, json=load_fixture("systemone", name), headers=headers)
    ).status_code == 400
    upstream.side_effect = [httpx.Response(403, text=f"denied for {GEMINI_KEY}")]
    assert (
        await gw.client.post(SO, json=load_fixture("systemone", name), headers=headers)
    ).status_code == 502
    assert (await gw.client.post(SO, json=load_fixture("systemone", name))).status_code == 401

    stdout = capsys.readouterr().out
    haystacks = [stdout, caplog.text]
    for record in caplog.records:
        haystacks.append(record.getMessage())
        haystacks.append(json.dumps(getattr(record, "fields", {}), default=str))
    forbidden = [
        "Checkout is blank after I click Pay",
        "Is this a bug report?",
        "Describes broken behaviour",
        "Check ",
        "tampering",
        "Fonts or alignment differ",
        png_b64(),
        png_b64((0, 0, 255)),
        INBOUND,
        GEMINI_KEY,
    ]
    for text in haystacks:
        for secret in forbidden:
            assert secret not in text, secret

    lines = json_log_lines(stdout)
    access = [line for line in lines if line["logger"] == "s1gw.access" and line["path"] == SO]
    assert len(access) == 5
    first = access[0]
    assert {
        "request_id", "status", "duration_ms", "question_count", "image_count",
        "upstream_status", "retries",
    } <= set(first)  # fmt: skip
    assert (
        first["status"] == 200 and first["question_count"] == 3 and first["upstream_status"] == 200
    )
    assert access[1]["image_count"] == 2
    assert access[2]["status"] == 400
    assert access[4]["status"] == 401


async def test_probe_requests_are_only_logged_at_debug(
    make_gateway: GatewayFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    gw = await make_gateway()
    await gw.client.get("/healthz")
    assert [x for x in json_log_lines(capsys.readouterr().out) if x.get("path") == "/healthz"] == []


def test_filter_redacts_message_args_fields_and_exceptions() -> None:
    flt = RedactionFilter(["super-secret-key", "abc"])  # "abc" is too short to be redacted
    try:
        raise ValueError("boom super-secret-key")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "key=%s abc", ("super-secret-key",), exc_info
    )
    record.fields = {"token": "super-secret-key", "n": 3}
    assert flt.filter(record)
    assert record.getMessage() == f"key={REDACTED} abc"
    assert record.fields == {"token": REDACTED, "n": 3}
    assert record.exc_text is not None and "super-secret-key" not in record.exc_text
    assert record.exc_info is None
    assert "super-secret-key" not in JsonFormatter().format(record)


def test_filter_without_fields_or_secrets_is_a_noop() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "plain", None, None)
    assert RedactionFilter().filter(record)
    assert record.getMessage() == "plain"


def test_formatters() -> None:
    record = logging.LogRecord("name", logging.WARNING, __file__, 1, "hello é", None, None)
    record.fields = {"a": 1}
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["message"] == "hello é" and parsed["a"] == 1 and parsed["level"] == "WARNING"
    assert TextFormatter().format(record) == "WARNING name: hello é a=1"
    bare = logging.LogRecord("name", logging.INFO, __file__, 1, "hi", None, None)
    assert TextFormatter().format(bare) == "INFO name: hi"
    bare.exc_text = "Traceback..."
    assert TextFormatter().format(bare).endswith("Traceback...")
    assert json.loads(JsonFormatter().format(bare))["exception"] == "Traceback..."


def test_setup_logging_is_idempotent_and_supports_text(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    before = len(root.handlers)
    setup_logging("INFO", "json", [])
    setup_logging("info", "text", ["topsecretvalue"])
    mine = [h for h in root.handlers if getattr(h, "_s1gw_handler", False)]
    assert len(mine) == 1 and len(root.handlers) == before + 1
    logging.getLogger("s1gw.test").info("hello topsecretvalue")
    assert capsys.readouterr().out.strip() == f"INFO s1gw.test: hello {REDACTED}"
    assert logging.getLogger("uvicorn").propagate is True
