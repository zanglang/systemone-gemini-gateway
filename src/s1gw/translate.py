"""Pure translation functions between System One and Gemini Decisions (BRIEF section 3).

No I/O happens here. This module must stay at 100% line coverage.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from s1gw.errors import InvalidUpstreamResponseError, TranslationError
from s1gw.models_gemini import (
    ChoiceAnswer,
    GeminiResponse,
    NoulAnswer,
    ScoreAnswer,
)
from s1gw.models_systemone import Asset, Question, SystemOneRequest

MAX_QUESTIONS = 1000
MAX_CHOICE_OPTIONS = 255
MAX_OPTION_KEY_CHARS = 128
MAX_OPTION_KEY_BYTES = 512
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10  # System One maximum (Gemini allows 255)

BOX_MESSAGE = (
    "box questions are not supported by this gateway (gemini-decision-1 has no bounding-box type)"
)
MIME_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}

_ANSWER_MODELS: dict[str, type[BaseModel]] = {
    "noul": NoulAnswer,
    "choice": ChoiceAnswer,
    "score": ScoreAnswer,
}


@dataclass(frozen=True)
class TranslatedRequest:
    payload: dict[str, Any]
    id_map: dict[str, str]  # gemini key (q0..qN) -> client id, in client order
    question_types: dict[str, str]  # gemini key -> question type
    image_count: int


def serialize(value: Any) -> str:
    """Strings pass through unchanged; everything else becomes compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class _Relabeler:
    """Replaces asset references with ``<image N>`` labels, longest key first."""

    def __init__(self, assets: dict[str, Asset]) -> None:
        self.labels = {key: f"<image {n}>" for n, key in enumerate(assets, start=1)}
        self.used: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        if self.labels:
            keys = "|".join(re.escape(k) for k in sorted(self.labels, key=len, reverse=True))
            # Backticked form first so the backticks are consumed with the key.
            self._pattern = re.compile(f"`({keys})`|({keys})")

    def __call__(self, text: str) -> str:
        if self._pattern is None:
            return text

        def replace(match: re.Match[str]) -> str:
            key = match.group(1) or match.group(2)
            self.used.add(key)
            return self.labels[key]

        return self._pattern.sub(replace, text)


def _validate_assets(assets: dict[str, Asset]) -> dict[str, tuple[str, str]]:
    """Return asset key -> (mime type, base64 data), validating the data."""
    result: dict[str, tuple[str, str]] = {}
    for key, asset in assets.items():
        if not key:
            raise TranslationError("asset keys must not be empty")
        try:
            base64.b64decode(asset.data_base64, validate=True)
        except ValueError as exc:
            raise TranslationError(f"asset '{key}' has invalid base64 data") from exc
        result[key] = (MIME_TYPES[asset.format], asset.data_base64)
    return result


def _translate_noul(qid: str, q: Question, relabel: _Relabeler) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "noul", "instructions": relabel(serialize(q.instructions))}
    criteria = q.criteria
    if criteria is None:
        return out
    if not isinstance(criteria, dict):
        raise TranslationError(f"noul question '{qid}' criteria must be an object")
    described: dict[str, str] = {}
    for side in ("true", "false"):
        if criteria.get(side) is not None:
            described[f"{side}_description"] = relabel(serialize(criteria[side]))
    if described:
        out["criteria"] = described
    return out


def _translate_choice(qid: str, q: Question, relabel: _Relabeler) -> dict[str, Any]:
    criteria = q.criteria
    if not isinstance(criteria, dict):
        raise TranslationError(f"choice question '{qid}' criteria must be an object")
    if len(criteria) < 2:
        raise TranslationError(
            f"choice question '{qid}' needs at least 2 options for gemini-decision-1"
        )
    if len(criteria) > MAX_CHOICE_OPTIONS:
        raise TranslationError(
            f"choice question '{qid}' has more than {MAX_CHOICE_OPTIONS} options"
        )
    options: dict[str, str] = {}
    for key, description in criteria.items():
        if len(key) > MAX_OPTION_KEY_CHARS or len(key.encode()) > MAX_OPTION_KEY_BYTES:
            raise TranslationError(
                f"choice question '{qid}' has an option key longer than "
                f"{MAX_OPTION_KEY_CHARS} characters or {MAX_OPTION_KEY_BYTES} bytes"
            )
        options[key] = "" if description is None else relabel(serialize(description))
    return {
        "type": "choice",
        "instructions": relabel(serialize(q.instructions)),
        "criteria": options,
    }


def _translate_score(qid: str, q: Question, relabel: _Relabeler) -> dict[str, Any]:
    levels = q.criteria
    if not isinstance(levels, list):
        raise TranslationError(f"score question '{qid}' criteria must be an array")
    if len(levels) < MIN_SCORE_LEVELS:
        raise TranslationError(
            f"score question '{qid}' needs at least {MIN_SCORE_LEVELS} levels for gemini-decision-1"
        )
    if len(levels) > MAX_SCORE_LEVELS:
        raise TranslationError(f"score question '{qid}' has more than {MAX_SCORE_LEVELS} levels")
    out_levels: list[str] = []
    for index, level in enumerate(levels):
        if level is None or level == "":
            raise TranslationError(f"score question '{qid}' level {index} must not be empty")
        out_levels.append(relabel(serialize(level)))
    return {
        "type": "score",
        "instructions": relabel(serialize(q.instructions)),
        "criteria": out_levels,
    }


_QUESTION_TRANSLATORS = {
    "noul": _translate_noul,
    "choice": _translate_choice,
    "score": _translate_score,
}


def translate_request(
    req: SystemOneRequest, *, model: str, image_resolution: str | None = None
) -> TranslatedRequest:
    """System One request -> Gemini request payload plus the data needed to reverse it."""
    if req.images:
        raise TranslationError("use assets for images")
    if len(req.questions) > MAX_QUESTIONS:
        raise TranslationError(f"too many questions: the maximum is {MAX_QUESTIONS}")
    if isinstance(req.state, str) and req.state == "":
        raise TranslationError("state must not be empty")

    assets = _validate_assets(req.assets or {})
    relabel = _Relabeler(req.assets or {})

    state_text = relabel(serialize(req.state))
    questions: dict[str, Any] = {}
    id_map: dict[str, str] = {}
    question_types: dict[str, str] = {}
    for index, (client_id, question) in enumerate(req.questions.items()):
        if question.type == "box":
            raise TranslationError(BOX_MESSAGE)
        key = f"q{index}"
        questions[key] = _QUESTION_TRANSLATORS[question.type](client_id, question, relabel)
        id_map[key] = client_id
        question_types[key] = question.type

    for asset_key in assets:
        if asset_key not in relabel.used:
            raise TranslationError(
                f"asset '{asset_key}' is not referenced in state, instructions, or criteria"
            )

    input_value: str | list[dict[str, Any]] = state_text
    if assets:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": state_text}]
        for number, (mime_type, data) in enumerate(assets.values(), start=1):
            blocks.append({"type": "text", "text": f"\n\n<image {number}>:\n"})
            image: dict[str, Any] = {"type": "image", "data": data, "mime_type": mime_type}
            if image_resolution:
                image["resolution"] = image_resolution
            blocks.append(image)
        input_value = blocks

    return TranslatedRequest(
        payload={"model": model, "input": input_value, "questions": questions},
        id_map=id_map,
        question_types=question_types,
        image_count=len(assets),
    )


def _string_keys(values: dict[str | int, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in values.items()}


def _translate_answer(client_id: str, expected: str, raw: dict[str, Any]) -> dict[str, Any]:
    declared = raw.get("type")
    if (declared is not None and declared != expected) or expected not in raw:
        raise InvalidUpstreamResponseError(f"upstream answer for '{client_id}' has the wrong type")
    try:
        parsed = _ANSWER_MODELS[expected].model_validate(raw)
    except ValidationError as exc:
        raise InvalidUpstreamResponseError() from exc
    if isinstance(parsed, NoulAnswer):
        # System One noul answers carry no confidence.
        return {"type": "noul", "noul": parsed.noul}
    if isinstance(parsed, ChoiceAnswer):
        return {
            "type": "choice",
            "choice": parsed.choice,
            "confidence": parsed.confidence,
            "probabilities": parsed.probabilities,
        }
    assert isinstance(parsed, ScoreAnswer)
    return {
        "type": "score",
        "score": parsed.score,
        "confidence": parsed.confidence,
        "legend": _string_keys(parsed.legend),
        "probabilities": _string_keys(parsed.probabilities),
    }


def translate_response(tr: TranslatedRequest, body: Any, *, model: str) -> dict[str, Any]:
    """Gemini response body -> System One response body."""
    try:
        upstream = GeminiResponse.model_validate(body)
    except ValidationError as exc:
        raise InvalidUpstreamResponseError() from exc
    answers: dict[str, Any] = {}
    for key, client_id in tr.id_map.items():
        if key not in upstream.answers:
            raise InvalidUpstreamResponseError(f"upstream returned no answer for '{client_id}'")
        answers[client_id] = _translate_answer(
            client_id, tr.question_types[key], upstream.answers[key]
        )
    return {
        "model": model,
        "answers": answers,
        "usage": {"input_tokens": upstream.usage.total_input_tokens, "output_tokens": 0},
    }
