"""Asset (image) translation (BRIEF section 11, items 12-18)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from s1gw.errors import TranslationError
from s1gw.models_systemone import SystemOneRequest
from s1gw.translate import translate_request
from tests.conftest import png_b64


def build(
    state: Any,
    assets: dict[str, Any],
    questions: dict[str, Any] | None = None,
    **extra: Any,
) -> SystemOneRequest:
    qs = questions or {"q": {"type": "noul", "instructions": "plain"}}
    return SystemOneRequest.model_validate(
        {"model": "m", "state": state, "questions": qs, "assets": assets, **extra}
    )


def asset(fmt: str = "png", data: str | None = None) -> dict[str, str]:
    return {"type": "image", "format": fmt, "data_base64": data or png_b64()}


def run(request: SystemOneRequest, **kw: Any) -> dict[str, Any]:
    return translate_request(request, model="gemini-decision-1", **kw).payload


def test_reference_in_state_with_backticks() -> None:
    payload = run(build("Look at `photo` now", {"photo": asset()}))
    blocks = payload["input"]
    assert blocks[0] == {"type": "text", "text": "Look at <image 1> now"}
    assert blocks[2] == {"type": "image", "data": png_b64(), "mime_type": "image/png"}
    assert len(blocks) == 3


def test_bare_reference_as_whole_state() -> None:
    assert run(build("photo", {"photo": asset()}))["input"][0]["text"] == "<image 1>"


def test_reference_in_object_state() -> None:
    payload = run(build({"pic": "`photo`"}, {"photo": asset()}))
    assert payload["input"][0]["text"] == '{"pic":"<image 1>"}'


def test_reference_only_in_instruction_and_criteria() -> None:
    questions = {
        "a": {"type": "noul", "instructions": "Is `photo` blurry?"},
        "b": {
            "type": "choice",
            "instructions": "x",
            "criteria": {"photo": "matches shot", "other": None},
        },
    }
    payload = run(build("no refs here", {"shot": asset(), "photo": asset("jpg")}, questions))
    assert payload["questions"]["q0"]["instructions"] == "Is <image 2> blurry?"
    # Option keys are ids and are never rewritten; only descriptions are.
    assert payload["questions"]["q1"]["criteria"] == {"photo": "matches <image 1>", "other": ""}


def test_reference_in_score_level() -> None:
    questions = {"s": {"type": "score", "instructions": "x", "criteria": ["bad", "like `pic`"]}}
    payload = run(build("s", {"pic": asset(), "s": asset()}, questions))
    assert payload["questions"]["q0"]["criteria"] == ["bad", "like <image 1>"]


def test_longest_key_replaced_first() -> None:
    state = "compare img2 with `img` and `img2` and img"
    payload = run(build(state, {"img": asset(), "img2": asset("jpeg", png_b64((0, 0, 255)))}))
    assert (
        payload["input"][0]["text"]
        == "compare <image 2> with <image 1> and <image 2> and <image 1>"
    )


def test_label_text_is_not_rewritten_again() -> None:
    # A key that is a substring of the label text must not clobber labels.
    payload = run(build("see `image` and `x`", {"image": asset(), "x": asset()}))
    assert payload["input"][0]["text"] == "see <image 1> and <image 2>"


def test_unreferenced_asset_is_400() -> None:
    with pytest.raises(TranslationError) as exc:
        run(build("nothing", {"photo": asset()}))
    assert exc.value.detail == "asset 'photo' is not referenced in state, instructions, or criteria"
    assert exc.value.status_code == 400


def test_bad_format_is_rejected_by_schema_with_422() -> None:
    # Documented choice: unsupported formats fail request validation (FastAPI 422).
    with pytest.raises(ValidationError):
        build("photo", {"photo": asset("gif")})


def test_invalid_base64_is_400_naming_the_key() -> None:
    for bad in ("not base64!!", "é", "abc"):
        with pytest.raises(TranslationError, match="asset 'photo' has invalid base64"):
            run(build("photo", {"photo": asset(data=bad)}))


def test_empty_asset_key_is_400() -> None:
    with pytest.raises(TranslationError, match="must not be empty"):
        run(build("x", {"": asset()}))


def test_jpg_and_jpeg_map_to_image_jpeg() -> None:
    payload = run(build("a b", {"a": asset("jpg"), "b": asset("jpeg")}))
    assert [b["mime_type"] for b in payload["input"] if b["type"] == "image"] == ["image/jpeg"] * 2


def test_blocks_alternate_with_explicit_separators() -> None:
    payload = run(build("`a` `b` `c`", {"a": asset(), "b": asset("jpg"), "c": asset()}))
    blocks = payload["input"]
    assert [b["type"] for b in blocks] == [
        "text",
        "text",
        "image",
        "text",
        "image",
        "text",
        "image",
    ]
    assert blocks[1]["text"] == "\n\n<image 1>:\n"
    assert blocks[3]["text"] == "\n\n<image 2>:\n"
    assert blocks[5]["text"] == "\n\n<image 3>:\n"
    for left, right in zip(blocks, blocks[1:], strict=False):
        assert not (
            left["type"] == "text"
            and right["type"] == "text"
            and not right["text"].startswith("\n\n")
        )


def test_images_field_is_rejected() -> None:
    with pytest.raises(TranslationError, match="use assets for images"):
        run(build("photo", {"photo": asset()}, images=["http://x/y.png"]))


def test_resolution_applied_only_when_set() -> None:
    request = build("photo", {"photo": asset()})
    assert "resolution" not in run(request)["input"][2]
    assert run(request, image_resolution="high")["input"][2]["resolution"] == "high"
    assert "resolution" not in run(request, image_resolution=None)["input"][2]


def test_no_assets_means_plain_string_input_and_zero_images() -> None:
    result = translate_request(build("hi", {}), model="m")
    assert result.payload["input"] == "hi"
    assert result.image_count == 0
