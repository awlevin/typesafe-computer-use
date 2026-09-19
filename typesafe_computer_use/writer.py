"""The writer model: the only place free text is generated, when the classifier asks for it and once when the run ends."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import anthropic
from PIL import Image

from .config import answer_model, writer_model
from .dates import now_context
from .models import Item, Screen
from .perception import near_field


def make_writer() -> anthropic.Anthropic | None:
    """A client, or None when no Anthropic credentials resolve (the SDK only checks on first request)."""
    client = anthropic.Anthropic()
    if client.api_key or getattr(client, "auth_token", None):
        return client
    return None


ANSWER_IMAGE_EDGE = 1568  # the longest edge a vision model reads without shrinking the image itself


def _strip_fences(raw: str) -> str:
    """GLM-compatible output: the endpoint ignores output_config, so the model may wrap
    its JSON in ```json fences or prepend chatter. Peel all of that to find the object."""
    s = raw.strip()
    if s.startswith("```"):  # ```json ... ``` or bare ``` ... ```
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        s = s[start : end + 1]
    return s


def _structured(
    writer: anthropic.Anthropic,
    system: str,
    packet: dict,
    properties: dict,
    max_tokens: int,
    model: str | None = None,
    image: Image.Image | None = None,
) -> dict:
    content: list[dict] = [{"type": "text", "text": json.dumps(packet)}]
    if image is not None:
        content.insert(0, _image_block(image))
    # GLM's Anthropic-compatible endpoint ignores output_config (verified: the schema is
    # silently dropped) and enables thinking by default, which eats the token budget.
    # Spell the schema out in the system prompt instead, and disable thinking.
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    kwargs: dict = {}
    base_url = str(getattr(writer, "base_url", ""))
    if "bigmodel" in base_url or "zhipu" in base_url:
        system = (
            f"{system}\n\nAnswer with ONLY a JSON object matching this schema, no prose, no "
            f"markdown fences: {json.dumps(schema)}"
        )
        kwargs["thinking"] = {"type": "disabled"}
    else:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    response = writer.messages.create(
        model=model or writer_model(),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        **kwargs,
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    return json.loads(_strip_fences(raw))


def _image_block(image: Image.Image) -> dict:
    """The capture as a PNG the model can read. PNG because screen text does not survive JPEG well."""
    shrunk = image.convert("RGB")
    shrunk.thumbnail((ANSWER_IMAGE_EDGE, ANSWER_IMAGE_EDGE))
    buffer = io.BytesIO()
    shrunk.save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def compose_text(writer: anthropic.Anthropic, goal: str, screen: Screen, items: list[Item], history: list[str]) -> str:
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


def compose_url(writer: anthropic.Anthropic, goal: str, history: list[str]) -> str:
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
    writer: anthropic.Anthropic, goal: str, screen: Screen, items: list[Item], history: list[str], stopped: str
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
