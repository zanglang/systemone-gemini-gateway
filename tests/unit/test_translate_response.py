"""Response translation (BRIEF section 11, items 19-23)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from s1gw.errors import InvalidUpstreamResponseError
from s1gw.models_systemone import SystemOneRequest
from s1gw.translate import TranslatedRequest, translate_request, translate_response
from tests.conftest import fixture_names, load_fixture


def prepared(name: str = "01_ticket_triage.request.json") -> TranslatedRequest:
    request = SystemOneRequest.model_validate(load_fixture("systemone", name))
    return translate_request(request, model="gemini-decision-1")


def run(body: Any, name: str = "01_ticket_triage.request.json") -> dict[str, Any]:
    return translate_response(prepared(name), body, model="gemini-decision-1")


@pytest.mark.parametrize("name", fixture_names())
def test_golden_responses(name: str) -> None:
    body = load_fixture("gemini", name.replace(".request", ".response"))
    assert run(body, name) == load_fixture("systemone", name.replace(".request", ".response"))


def test_noul_confidence_dropped_and_others_unrounded() -> None:
    result = run(load_fixture("gemini", "01_ticket_triage.response.json"))
    assert result["answers"]["is it a bug?"] == {"type": "noul", "noul": 0.87}
    team = result["answers"]["team"]
    assert team["confidence"] == 0.9123456789
    assert team["probabilities"] == {"payments": 0.9123456789, "web": 0.0876543211}


def test_integer_keys_are_normalized_to_strings() -> None:
    body = load_fixture("gemini", "01_ticket_triage.response.json")
    score = body["answers"]["q1"]
    score["legend"] = {0: "Cosmetic", 1: "Degraded", 2: "Blocking"}
    score["probabilities"] = {0: 0.1, 1: 0.38, 2: 0.52}
    answer = run(body)["answers"]["severity"]
    assert answer["legend"] == {"0": "Cosmetic", "1": "Degraded", "2": "Blocking"}
    assert answer["probabilities"] == {"0": 0.1, "1": 0.38, "2": 0.52}


def test_answers_without_type_field_are_accepted() -> None:
    body = load_fixture("gemini", "01_ticket_triage.response.json")
    for answer in body["answers"].values():
        del answer["type"]
    assert run(body) == load_fixture("systemone", "01_ticket_triage.response.json")


def test_answers_use_client_ids_in_client_order() -> None:
    body = load_fixture("gemini", "03_unicode_structured.response.json")  # upstream order differs
    result = run(body, "03_unicode_structured.request.json")
    assert list(result["answers"]) == list(
        load_fixture("systemone", "03_unicode_structured.request.json")["questions"]
    )


def test_missing_answer_is_502_naming_client_id() -> None:
    body = load_fixture("gemini", "01_ticket_triage.response.json")
    del body["answers"]["q1"]
    with pytest.raises(InvalidUpstreamResponseError) as exc:
        run(body)
    assert exc.value.status_code == 502
    assert exc.value.detail == "upstream returned no answer for 'severity'"


def test_type_mismatch_is_502() -> None:
    body = load_fixture("gemini", "01_ticket_triage.response.json")
    body["answers"]["q0"]["type"] = "choice"
    with pytest.raises(InvalidUpstreamResponseError, match="wrong type"):
        run(body)


def test_mismatch_without_type_field_is_502() -> None:
    body = load_fixture("gemini", "01_ticket_triage.response.json")
    body["answers"]["q0"] = {"choice": "x", "confidence": 1, "probabilities": {"x": 1}}
    with pytest.raises(InvalidUpstreamResponseError, match="wrong type"):
        run(body)


@pytest.mark.parametrize(
    "body",
    [None, [], "text", {}, {"answers": []}, {"answers": {"q0": "nope"}}],
)
def test_malformed_bodies_are_502(body: Any) -> None:
    with pytest.raises(InvalidUpstreamResponseError) as exc:
        run(body)
    assert exc.value.detail == "invalid upstream response"


def test_malformed_answer_fields_are_502() -> None:
    body = load_fixture("gemini", "01_ticket_triage.response.json")
    body["answers"]["q0"]["noul"] = "high"
    with pytest.raises(InvalidUpstreamResponseError, match="invalid upstream response"):
        run(body)


def test_usage_mapping_and_missing_usage() -> None:
    body = load_fixture("gemini", "01_ticket_triage.response.json")
    assert run(body)["usage"] == {"input_tokens": 412, "output_tokens": 0}
    body = copy.deepcopy(body)
    del body["usage"]
    assert run(body)["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_model_is_the_upstream_model() -> None:
    body = load_fixture("gemini", "01_ticket_triage.response.json")
    out = translate_response(prepared(), body, model="gemini-decision-9")
    assert out["model"] == "gemini-decision-9"
