"""Tolerant decoding and validation for structured writer responses."""

from __future__ import annotations

import json
from typing import Any

from .writer_backend import WriterError


class StructuredOutputError(WriterError):
    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw


def _strip_fence(text: str) -> str | None:
    if not text.startswith("```") or not text.endswith("```"):
        return None
    lines = text.splitlines()
    if len(lines) < 3:
        return None
    return "\n".join(lines[1:-1]).strip()


def _first_json_object(text: str) -> str | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            _, length = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return text[index : index + length]
    return None


def _validate(data: dict[str, Any], properties: dict) -> None:
    expected_types = {"boolean": bool, "string": str}
    for name, schema in properties.items():
        if name not in data:
            raise StructuredOutputError(f"writer response is missing {name!r}", json.dumps(data))
        expected = expected_types.get(schema.get("type"))
        if expected is not None and not isinstance(data[name], expected):
            raise StructuredOutputError(f"writer response has the wrong type for {name!r}", json.dumps(data))


def decode_json_object(raw: str, properties: dict) -> dict:
    text = raw.strip()
    if not text:
        raise StructuredOutputError("writer returned no text", raw)

    candidates = [text, _strip_fence(text), _first_json_object(text)]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            _validate(data, properties)
        except StructuredOutputError as error:
            raise StructuredOutputError(str(error), raw) from error
        return data

    raise StructuredOutputError("writer returned invalid structured output", raw)
