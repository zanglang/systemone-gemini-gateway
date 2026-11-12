"""Request translation (BRIEF section 11, items 1-11)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from s1gw.errors import TranslationError
from s1gw.models_systemone import SystemOneRequest
from s1gw.translate import BOX_MESSAGE, TranslatedRequest, serialize, translate_request
from tests.conftest import fixture_names, load_fixture


def req(
    state: Any = "hello", questions: dict[str, Any] | None = None, **extra: Any
) -> SystemOneRequest:
    qs = questions or {"a": {"type": "noul", "instructions": "x"}}
    return SystemOneRequest.model_validate(
        {"model": "jev-latest", "state": state, "questions": qs, **extra}
    )


def tr(request: SystemOneRequest, **kw: Any) -> TranslatedRequest:
    return translate_request(request, model="gemini-decision-1", **kw)


def one(question: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = tr(req(questions={"q": question})).payload["questions"]["q0"]
    return result


def test_string_state_passes_through_and_empty_is_400() -> None:
    assert tr(req("  keep  as is\n")).payload["input"] == "  keep  as is\n"
    with pytest.raises(TranslationError, match="state must not be empty") as exc:
        tr(req(""))
    assert exc.value.status_code == 400


def test_object_and_array_state_are_compact_json_in_order() -> None:
    assert tr(req({"b": 1, "a": "é☕"})).payload["input"] == '{"b":1,"a":"é☕"}'
    assert tr(req([3, {"z": None}, "ü"])).payload["input"] == '[3,{"z":null},"ü"]'
    assert serialize("raw") == "raw"
    assert serialize({"a": [1, 2]}) == '{"a":[1,2]}'


@pytest.mark.parametrize("client_id", ["has space", "a/b", "🎫", "x" * 200, "q1"])
def test_question_ids_are_remapped_in_order_and_reversible(client_id: str) -> None:
    qs = {
        "first": {"type": "noul", "instructions": "1"},
        client_id: {"type": "noul", "instructions": "2"},
        "last": {"type": "noul", "instructions": "3"},
    }
    result = tr(req(questions=qs))
    assert list(result.payload["questions"]) == ["q0", "q1", "q2"]
    assert result.id_map == {"q0": "first", "q1": client_id, "q2": "last"}
    assert result.payload["questions"]["q1"]["instructions"] == "2"


def test_noul_criteria() -> None:
    both = one({"type": "noul", "instructions": "i", "criteria": {"true": "T", "false": "F"}})
    assert both["criteria"] == {"true_description": "T", "false_description": "F"}
    only_false = one(
        {"type": "noul", "instructions": "i", "criteria": {"true": None, "false": {"a": 1}}}
    )
    assert only_false["criteria"] == {"false_description": '{"a":1}'}
    for criteria in (None, {}, {"true": None, "false": None}):
        assert "criteria" not in one({"type": "noul", "instructions": "i", "criteria": criteria})
    with pytest.raises(TranslationError, match="'q' criteria must be an object"):
        one({"type": "noul", "instructions": "i", "criteria": ["x"]})


def test_choice_criteria() -> None:
    out = one(
        {"type": "choice", "instructions": "i", "criteria": {"a": None, "b": {"k": "v"}, "c": "d"}}
    )
    assert out["criteria"] == {"a": "", "b": '{"k":"v"}', "c": "d"}


@pytest.mark.parametrize("criteria", [{"only": "x"}, {}])
def test_choice_needs_two_options(criteria: dict[str, Any]) -> None:
    with pytest.raises(TranslationError) as exc:
        tr(req(questions={"pick": {"type": "choice", "instructions": "i", "criteria": criteria}}))
    assert (
        exc.value.detail == "choice question 'pick' needs at least 2 options for gemini-decision-1"
    )


def test_choice_option_key_limits() -> None:
    ok = {"k" * 128: "a", "b": "b"}
    assert one({"type": "choice", "instructions": "i", "criteria": ok})
    too_long = {"k" * 129: "a", "b": "b"}
    with pytest.raises(TranslationError, match="'q'.*option key"):
        one({"type": "choice", "instructions": "i", "criteria": too_long})
    too_many_bytes = {"é" * 129: "a", "b": "b"}  # 129 chars but also > 128
    with pytest.raises(TranslationError, match="option key"):
        one({"type": "choice", "instructions": "i", "criteria": too_many_bytes})
    # 128 chars but 3 bytes each = 384 bytes: fine. 128 emoji = 512 bytes: fine. Over: not possible
    # within 128 chars with 4-byte code points, so the byte limit is covered by the check above.
    emoji = {"🎫" * 128: "a", "b": "b"}
    assert one({"type": "choice", "instructions": "i", "criteria": emoji})


def test_choice_criteria_shape_and_option_count_limits() -> None:
    with pytest.raises(TranslationError, match="must be an object"):
        one({"type": "choice", "instructions": "i", "criteria": ["a", "b"]})
    with pytest.raises(TranslationError, match="more than 255 options"):
        one({"type": "choice", "instructions": "i", "criteria": {f"o{i}": "" for i in range(256)}})
    assert one(
        {"type": "choice", "instructions": "i", "criteria": {f"o{i}": "" for i in range(255)}}
    )


def test_score_levels() -> None:
    assert one({"type": "score", "instructions": "i", "criteria": ["a", "b", "c"]})["criteria"] == [
        "a",
        "b",
        "c",
    ]
    assert one({"type": "score", "instructions": "i", "criteria": ["a", {"x": 1}]})["criteria"] == [
        "a",
        '{"x":1}',
    ]
    assert (
        len(one({"type": "score", "instructions": "i", "criteria": list("abcdefghij")})["criteria"])
        == 10
    )
    for bad in (["only"], list("abcdefghijk"), ["a", None], ["a", ""], []):
        with pytest.raises(TranslationError):
            one({"type": "score", "instructions": "i", "criteria": bad})
    with pytest.raises(TranslationError, match="must be an array"):
        one({"type": "score", "instructions": "i", "criteria": {"a": "b"}})


def test_non_string_instructions_are_serialized() -> None:
    assert one({"type": "noul", "instructions": {"a": 1}})["instructions"] == '{"a":1}'
    assert one({"type": "noul", "instructions": ["x", "y"]})["instructions"] == '["x","y"]'


def test_box_is_rejected() -> None:
    with pytest.raises(TranslationError) as exc:
        tr(req(questions={"b": {"type": "box", "instructions": "find it"}}))
    assert exc.value.detail == BOX_MESSAGE
    assert exc.value.status_code == 400


def test_question_count_limits() -> None:
    def many(n: int) -> dict[str, Any]:
        return {f"id{i}": {"type": "noul", "instructions": "x"} for i in range(n)}

    assert len(tr(req(questions=many(1000))).payload["questions"]) == 1000
    with pytest.raises(TranslationError, match="too many questions"):
        tr(req(questions=many(1001)))


def test_unknown_fields_are_ignored() -> None:
    request = req(
        questions={"a": {"type": "noul", "instructions": "x", "extra": 1}},
        surprise={"a": 1},
        images=[],
    )
    assert tr(request).payload["questions"]["q0"] == {"type": "noul", "instructions": "x"}


@pytest.mark.parametrize("name", fixture_names())
def test_golden_fixtures(name: str) -> None:
    request = SystemOneRequest.model_validate(load_fixture("systemone", name))
    expected = load_fixture("gemini", name)
    result = tr(request)
    assert result.payload == expected
    # Key order must also match (insertion order is part of the contract).
    assert json.dumps(result.payload) == json.dumps(expected)


def test_fixture_one_is_the_brief_example() -> None:
    assert fixture_names()[0] == "01_ticket_triage.request.json"
    request = load_fixture("systemone", "01_ticket_triage.request.json")
    assert request["state"] == {
        "ticket": "Checkout is blank after I click Pay",
        "tier": "enterprise",
    }
