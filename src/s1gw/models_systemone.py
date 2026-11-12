"""Pydantic v2 schemas for the System One (inbound) side."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Text = str | dict[str, Any] | list[Any]


class Question(BaseModel):
    type: Literal["noul", "choice", "score", "box"]
    instructions: Text
    # Shape depends on the type; translate.py validates it and answers 400.
    criteria: Any = None


class Asset(BaseModel):
    type: Literal["image"] | None = None
    format: Literal["png", "jpg", "jpeg"]
    data_base64: str


class SystemOneRequest(BaseModel):
    model: str
    state: Text
    questions: dict[str, Question] = Field(min_length=1)
    assets: dict[str, Asset] | None = None
    # aimlapi variant; rejected by translate.py when non-empty.
    images: Any = None
