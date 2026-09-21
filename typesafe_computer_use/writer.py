"""The writer model: the only place free text is generated, when the classifier asks for it and once when the run ends."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import anthropic
from PIL import Image

from . import config
from .config import answer_model, writer_model
from .dates import now_context
from .models import Item, Screen
from .openai_writer import OpenAICompatibleWriterBackend
from .perception import near_field
from .structured_output import decode_json_object
from .writer_backend import StructuredRequest, WriterBackend


class AnthropicWriterBackend:
    def __init__(self, client: anthropic.Anthropic):
        self.client = client

    def generate(self, request: StructuredRequest) -> str:
        content = [{"type": "text", "text": json.dumps(request.packet)}]
        if request.image is not None:
            content.insert(0, _image_block(request.image))
        response = self.client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": content}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": request.properties,
                        "required": list(request.properties),
                        "additionalProperties": False,
                    },
                }
            },
        )
        return "".join(block.text for block in response.content if block.type == "text")


def make_writer() -> WriterBackend | None:
    """Build the configured writer backend, or None when its credentials are incomplete."""
    if config.writer_provider() == "openai-compatible":
        api_key = config.writer_api_key()
        base_url = config.writer_base_url()
        if not api_key or not base_url:
            return None
        return OpenAICompatibleWriterBackend(
            api_key=api_key,
            base_url=base_url,
            vision_enabled=config.writer_vision(),
            structured_mode=config.structured_output_mode(),
        )

    client = anthropic.Anthropic()
    if client.api_key or getattr(client, "auth_token", None):
        return AnthropicWriterBackend(client)
    return None


ANSWER_IMAGE_EDGE = 1568  # the longest edge a vision model reads without shrinking the image itself


def _structured(
    writer: WriterBackend,
    system: str,
    packet: dict,
    properties: dict,
    max_tokens: int,
    model: str | None = None,
    image: Image.Image | None = None,
) -> dict:
    request = StructuredRequest(
        system=system,
        packet=packet,
        properties=properties,
        max_tokens=max_tokens,
        model=model or writer_model(),
        image=image,
    )
    return decode_json_object(writer.generate(request), properties)


def _image_block(image: Image.Image) -> dict:
    """The capture as a PNG the model can read. PNG because screen text does not survive JPEG well."""
    shrunk = image.convert("RGB")
    shrunk.thumbnail((ANSWER_IMAGE_EDGE, ANSWER_IMAGE_EDGE))
    buffer = io.BytesIO()
    shrunk.save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def compose_text(writer: WriterBackend, goal: str, screen: Screen, items: list[Item], history: list[str]) -> str:
    """The exact string to type into the focused field. Empty means the writer declined."""
    packet = {
        "goal": goal,
        "now": now_context(),
        "frontmost_app": screen.app,
        "previous_actions": history[-8:],
        "focused_field": screen.field.summary() if screen.field else None,
        "text_near_field": near_field(screen, items),
        "all_screen_text": [it.text for it in items][:120],
    }
    data = _structured(
        writer,
        system=(
            "You fill in one text field on a user's screen. You receive the user's goal, recent "
            "actions, the focused field's label and placeholder, and nearby screen text. Decide the "
            "exact string to type. Never invent credentials, passwords, or personal data; for such "
            "fields, or when the field should not be filled, set fill to false."
        ),
        packet=packet,
        properties={"fill": {"type": "boolean"}, "text": {"type": "string"}, "reason": {"type": "string"}},
        max_tokens=256,
    )
    return data["text"].strip() if data["fill"] else ""


def valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and "." in parsed.netloc and not any(ch.isspace() for ch in url)


def compose_url(writer: WriterBackend, goal: str, history: list[str]) -> str:
    """The URL to open for this goal. Empty means no sensible site, or an invalid proposal."""
    data = _structured(
        writer,
        system=(
            "Given a user's goal for their web browser, give the single best https URL to open first. "
            "Prefer the site's homepage or the most direct public page. If no website is implied, set ok to false."
        ),
        packet={"goal": goal, "now": now_context(), "previous_actions": history[-8:]},
        properties={"ok": {"type": "boolean"}, "url": {"type": "string"}, "reason": {"type": "string"}},
        max_tokens=200,
    )
    url = data["url"].strip() if data["ok"] else ""
    return url if valid_url(url) else ""


@dataclass(frozen=True)
class Answer:
    text: str
    achieved: bool  # whether the screen itself shows the goal reached, in the writer's judgement


def compose_answer(
    writer: WriterBackend, goal: str, screen: Screen, items: list[Item], history: list[str], stopped: str
) -> Answer:
    """What to tell the user now that the run is over: the result when the screen holds it, where things stand when not.

    The classifier can stop on the right page but cannot say what the page says. The writer reads the
    capture itself as well as its text, since OCR misreads a letter here and there and drops layout.
    """
    packet = {
        "goal": goal,
        "now": now_context(),
        "why_the_run_stopped": stopped,
        "actions_taken": history,
        "frontmost_app": screen.app,
        "browser_active_tab_url": screen.url,
        "screen_text_in_reading_order": [it.text for it in items],
    }
    data = _structured(
        writer,
        system=(
            "An agent drove a user's computer toward the user's goal and has now stopped. You receive "
            "the goal, the actions it took, why it stopped, a capture of the screen as it is now, and "
            "the text read from that screen. Tell the user the result. When the goal asks for "
            "information, lead with that information, taken only from the screen: never from memory, "
            "and never a guess. When the goal asks for something to be done, say whether the screen "
            "shows it done. When the screen does not hold the result, say so plainly, then say what is "
            "on screen and the one next step that would get there. Trust the capture over the text "
            "where the two disagree. Plain text, no markdown, four sentences at most. Set achieved to "
            "true only when the screen itself shows the goal reached."
        ),
        packet=packet,
        properties={"achieved": {"type": "boolean"}, "answer": {"type": "string"}},
        max_tokens=1024,
        model=answer_model(),
        image=screen.image,
    )
    return Answer(text=data["answer"].strip(), achieved=data["achieved"])
