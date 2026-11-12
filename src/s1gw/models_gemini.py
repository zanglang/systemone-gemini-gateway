"""Pydantic v2 schemas for parsing Gemini Decisions API responses.

Parsing is deliberately tolerant (see docs/UPSTREAM_NOTES.md): legend and probability keys may
be strings or integers, and answers may or may not carry a ``type`` field.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class GeminiUsage(BaseModel):
    total_input_tokens: int = 0


class GeminiResponse(BaseModel):
    answers: dict[str, dict[str, Any]]
    usage: GeminiUsage = GeminiUsage()


class NoulAnswer(BaseModel):
    noul: float


class ChoiceAnswer(BaseModel):
    choice: str
    confidence: float
    probabilities: dict[str, float]


class ScoreAnswer(BaseModel):
    score: float
    confidence: float
    legend: dict[str | int, str]
    probabilities: dict[str | int, float]
