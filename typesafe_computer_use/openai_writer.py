"""OpenAI-compatible writer backend for DeepSeek and local proxies."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

from openai import OpenAI
from PIL import Image

from .structured_output import StructuredOutputError, decode_json_object
from .writer_backend import StructuredRequest, WriterResponseError

PROMPT_ONLY_INSTRUCTION = "Return only one JSON object matching the supplied schema. Do not use Markdown fences."
STRUCTURED_MODES = ("json_schema", "json_object", "prompt")


def _image_data_url(image: Image.Image) -> str:
    shrunk = image.convert("RGB")
    shrunk.thumbnail((1568, 1568))
    buffer = io.BytesIO()
    shrunk.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _schema(properties: dict, required: tuple[str, ...] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else list(required),
        "additionalProperties": False,
    }


def _status_code(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _is_unsupported_format_error(error: Exception) -> bool:
    if _status_code(error) != 400:
        return False
    text = str(error).lower()
    format_name = any(
        term in text for term in ("response_format", "json_schema", "json_object", "structured output", "structured-output")
    )
    unsupported = any(term in text for term in ("unsupported", "not supported", "unrecognized", "unknown", "invalid"))
    return format_name and unsupported


def _is_vision_compatibility_error(error: Exception) -> bool:
    if _status_code(error) != 400:
        return False
    text = str(error).lower()
    image_term = any(term in text for term in ("image_url", "multimodal", "vision", "image input"))
    compatibility_term = any(term in text for term in ("unsupported", "not supported", "does not support", "not enabled"))
    return image_term and compatibility_term


class OpenAICompatibleWriterBackend:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        vision_enabled: bool = True,
        structured_mode: str = "auto",
    ) -> None:
        if structured_mode not in (*STRUCTURED_MODES, "auto"):
            raise ValueError(f"unsupported structured output mode: {structured_mode}")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.vision_enabled = vision_enabled
        self.structured_mode = structured_mode
        self.resolved_structured_mode: str | None = None

    def _modes(self) -> tuple[str, ...]:
        if self.structured_mode == "auto":
            start = STRUCTURED_MODES.index(self.resolved_structured_mode) if self.resolved_structured_mode else 0
            return STRUCTURED_MODES[start:]
        return (self.structured_mode,)

    def _messages(self, request: StructuredRequest, include_image: bool, mode: str) -> list[dict]:
        system = request.system
        if mode == "prompt":
            schema = json.dumps(_schema(request.properties, request.required), ensure_ascii=False)
            system = f"{system}\n\n{PROMPT_ONLY_INSTRUCTION}\nSchema:\n{schema}"
        content: list[dict] = [{"type": "text", "text": json.dumps(request.packet, ensure_ascii=False)}]
        if request.image is not None and include_image:
            content.insert(0, {"type": "image_url", "image_url": {"url": _image_data_url(request.image)}})
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

    def _create(self, request: StructuredRequest, mode: str, include_image: bool):
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": self._messages(request, include_image, mode),
            "max_tokens": request.max_tokens,
        }
        if mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "clicker_result",
                    "strict": True,
                    "schema": _schema(request.properties, request.required),
                },
            }
        elif mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        return self.client.chat.completions.create(**kwargs)

    @staticmethod
    def _content(response) -> str:
        content = response.choices[0].message.content
        if content is None:
            return ""
        return content if isinstance(content, str) else str(content)

    def _call_once(self, request: StructuredRequest, mode: str, include_image: bool) -> str:
        response = self._create(request, mode, include_image)
        content = self._content(response)
        if content:
            return content
        response = self._create(request, mode, include_image)
        content = self._content(response)
        if content:
            return content
        raise WriterResponseError("writer returned empty content twice")

    def generate(self, request: StructuredRequest) -> str:
        include_image = request.image is not None and self.vision_enabled
        modes = self._modes()
        for mode in modes:
            try:
                content = self._call_once(request, mode, include_image)
            except Exception as error:
                if (
                    self.structured_mode == "auto"
                    and mode != "prompt"
                    and _is_unsupported_format_error(error)
                ):
                    if self.resolved_structured_mode == mode:
                        self.resolved_structured_mode = None
                    continue
                if include_image and _is_vision_compatibility_error(error):
                    self.vision_enabled = False
                    include_image = False
                    content = self._call_once(request, mode, include_image)
                else:
                    raise
            try:
                decode_json_object(content, request.properties, required=request.required)
            except StructuredOutputError:
                if self.structured_mode == "auto" and mode != "prompt":
                    if self.resolved_structured_mode == mode:
                        self.resolved_structured_mode = None
                    continue
                return content
            self.resolved_structured_mode = mode
            return content
        raise WriterResponseError("writer could not negotiate structured output")
