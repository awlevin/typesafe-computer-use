"""Provider-neutral writer requests and errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL import Image


class WriterError(RuntimeError):
    """Base class for controlled writer failures."""


class WriterResponseError(WriterError):
    """The provider returned a response that could not be used."""


@dataclass(frozen=True)
class StructuredRequest:
    system: str
    packet: dict
    properties: dict
    max_tokens: int
    model: str
    required: tuple[str, ...] | None = None
    image: Image.Image | None = None


class WriterBackend(Protocol):
    def generate(self, request: StructuredRequest) -> str: ...
