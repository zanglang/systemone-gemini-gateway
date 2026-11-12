"""Measure gateway overhead against an instant mocked upstream (not run in CI).

    uv run python scripts/bench.py [--requests 500] [--questions 10]

Reports p50/p95/p99 wall time per request for (a) a direct call to the mocked upstream and
(b) the same call through the gateway, plus the difference (the gateway's overhead). The budget in
docs/BRIEF.md section 12 is p50 overhead under 10 ms for a 10-question text request.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any

import httpx

from s1gw.config import Settings
from s1gw.main import create_app

UPSTREAM = "https://gemini.bench"


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def build_request(n: int) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    for i in range(n):
        kind = i % 3
        if kind == 0:
            questions[f"question {i}"] = {"type": "noul", "instructions": f"Is it {i}?"}
        elif kind == 1:
            questions[f"question {i}"] = {
                "type": "choice",
                "instructions": "Pick",
                "criteria": {"a": "first", "b": None},
            }
        else:
            questions[f"question {i}"] = {
                "type": "score",
                "instructions": "Rate",
                "criteria": ["low", "mid", "high"],
            }
    return {"model": "jev-latest", "state": {"ticket": "bench " * 50}, "questions": questions}


def build_response(n: int) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for i in range(n):
        kind = i % 3
        if kind == 0:
            answers[f"q{i}"] = {"type": "noul", "noul": 0.5, "confidence": 0.5}
        elif kind == 1:
            answers[f"q{i}"] = {
                "type": "choice", "choice": "a", "confidence": 0.7,
                "probabilities": {"a": 0.7, "b": 0.3},
            }  # fmt: skip
        else:
            answers[f"q{i}"] = {
                "type": "score", "score": 1.0, "confidence": 0.6,
                "legend": {"0": "low", "1": "mid", "2": "high"},
                "probabilities": {"0": 0.2, "1": 0.6, "2": 0.2},
            }  # fmt: skip
    return {"answers": answers, "usage": {"total_input_tokens": 100, "total_output_tokens": 0}}


async def timed(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], count: int
) -> list[float]:
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        response = await client.post(url, json=body)
        samples.append((time.perf_counter() - start) * 1000)
        response.raise_for_status()
    return samples


async def main(requests: int, questions: int) -> None:
    payload = build_response(questions)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    mock = httpx.MockTransport(upstream_handler)
    settings = Settings(
        gemini_api_key="bench-key", gemini_base_url=UPSTREAM, otel_metrics_exporter="none",
        log_level="WARNING",
    )  # fmt: skip
    app = create_app(settings, transport=mock)
    body = build_request(questions)
    async with app.router.lifespan_context(app):
        gateway = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
        direct = httpx.AsyncClient(transport=mock)
        async with gateway, direct:
            await timed(gateway, "/v1/systemone", body, 50)  # warm up
            await timed(direct, f"{UPSTREAM}/v1beta/decisions", {}, 50)
            via = await timed(gateway, "/v1/systemone", body, requests)
            base = await timed(direct, f"{UPSTREAM}/v1beta/decisions", {}, requests)
    print(f"{requests} requests, {questions} questions each")
    for label, samples in (("direct upstream", base), ("via gateway", via)):
        print(
            f"{label:>16}: p50={statistics.median(samples):.2f}ms "
            f"p95={percentile(samples, 95):.2f}ms p99={percentile(samples, 99):.2f}ms"
        )
    overhead = statistics.median(via) - statistics.median(base)
    verdict = "OK" if overhead < 10 else "OVER BUDGET"
    print(f"p50 gateway overhead: {overhead:.2f}ms (budget 10ms) {verdict}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--questions", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(main(args.requests, args.questions))
