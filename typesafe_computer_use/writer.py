"""The writer model: the only place free text is generated, when the classifier asks for it and whenever the classifier stops."""

from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import anthropic
from PIL import Image

from .config import answer_model, custom_writer_endpoint, writer_base_url, writer_model
from .dates import now_context
from .models import Guidance, Item, Screen
from .perception import near_field

ANSWER_IMAGE_EDGE = 1568  # the longest edge a vision model reads without shrinking the image itself
PLACEHOLDER_KEY = "not-needed"  # an endpoint you host yourself does not check a key


def make_writer() -> anthropic.Anthropic | None:
    """A client, or None when there is nothing to write with.

    A key only ever goes to the endpoint it belongs to. CLICKER_WRITER_BASE_URL (see
    config.writer_base_url) is paired with CLICKER_WRITER_API_KEY, or with a placeholder when that
    is unset, since an endpoint you host yourself checks no key. Otherwise the SDK reads the
    ANTHROPIC_* key, token, and base URL as it always does.
    """
    base_url = writer_base_url()
    if base_url:
        key = os.environ.get("CLICKER_WRITER_API_KEY") or PLACEHOLDER_KEY
        # Proxies differ in which header they read, so the key goes in both. Passing any key at all
        # also keeps the SDK from adding ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN on its own.
        return anthropic.Anthropic(base_url=base_url, api_key=key, auth_token=key)
    client = anthropic.Anthropic()
    if client.api_key or client.auth_token:
        return client
    return None


def provider(writer: anthropic.Anthropic) -> str:
    """Where the writer sends its requests, for logging. Credentials and query in the URL are left out."""
    url = writer.base_url
    port = f":{url.port}" if url.port else ""
    where = f"{url.scheme}://{url.host}{port}{url.path.rstrip('/')}"
    return f"{where}  models: {writer_model()} (writing), {answer_model()} (answering)"


def _structured(
    writer: anthropic.Anthropic,
    system: str,
    packet: dict,
    properties: dict,
    max_tokens: int,
    model: str | None = None,
    image: Image.Image | None = None,
) -> dict:
    schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    content: list[dict] = [{"type": "text", "text": json.dumps(packet)}]
    if image is not None:
        content.insert(0, _image_block(image))
    extra: dict = {}
    if custom_writer_endpoint():
        # Anthropic enforces output_config. Another endpoint may ignore it without a word, so the
        # schema is spelled out in the prompt as well.
        system = f"{system}\n\nAnswer with a single JSON object and nothing else, matching this schema:\n{json.dumps(schema)}"
        # Another endpoint may think by default, out of the same max_tokens: a 200-token call then
        # comes back with no text at all. Anthropic thinks only when asked.
        extra["thinking"] = {"type": "disabled"}
    response = writer.messages.create(
        model=model or writer_model(),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
        **extra,
    )
    return parse_json("".join(b.text for b in response.content if b.type == "text"))


def parse_json(text: str) -> dict:
    """The JSON object out of a reply, as long as one is in there.

    A model that was asked for JSON usually returns exactly that. Some wrap it in code fences or a
    sentence, so the object itself is looked for rather than assumed to fill the whole reply.
    """
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        stripped = stripped[start : end + 1]
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"the writer answered without usable JSON: {text[:400]!r}") from e


def _image_block(image: Image.Image) -> dict:
    """The capture as a PNG the model can read. PNG because screen text does not survive JPEG well."""
    shrunk = image.convert("RGB")
    shrunk.thumbnail((ANSWER_IMAGE_EDGE, ANSWER_IMAGE_EDGE))
    buffer = io.BytesIO()
    shrunk.save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def compose_text(
    writer: anthropic.Anthropic,
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    guidance: Guidance | None = None,
) -> str:
    """The exact string to type into the focused field. Empty means the writer declined."""
    packet = {
        "goal": goal,
        **(guidance.state() if guidance else {}),
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
            "actions, the focused field's label and placeholder, and nearby screen text, and, when "
            "there are any, the step the agent is now working on and what the user said when asked. "
            "Decide the exact string to type. Never invent credentials, passwords, or personal data; for such "
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


def compose_url(writer: anthropic.Anthropic, goal: str, history: list[str], guidance: Guidance | None = None) -> str:
    """The URL to open for this goal. Empty means no sensible site, or an invalid proposal."""
    data = _structured(
        writer,
        system=(
            "Given a user's goal for their web browser, give the single best https URL to open first. "
            "Prefer the site's homepage or the most direct public page. If no website is implied, set ok to false."
        ),
        packet={"goal": goal, **(guidance.state() if guidance else {}), "now": now_context(), "previous_actions": history[-8:]},
        properties={"ok": {"type": "boolean"}, "url": {"type": "string"}, "reason": {"type": "string"}},
        max_tokens=200,
    )
    url = data["url"].strip() if data["ok"] else ""
    return url if valid_url(url) else ""


@dataclass(frozen=True)
class Answer:
    """What the writer made of a stop: the words for the user, and how the run goes on when it can.

    At most one of `focus` and `question` is acted on. A focus sends the classifier back to work,
    a question goes to the user first, and neither means the run is over.
    """

    text: str
    achieved: bool  # whether the screen itself shows the goal reached, in the writer's judgement
    focus: str = ""  # the next sub-goal for the classifier, in terms of the screen
    question: str = ""  # what only the user can say


ANSWER_SYSTEM = (
    "An agent is driving a user's computer toward the user's goal. A small classifier picks each "
    "action, and it has stopped and handed the run to you. You receive the goal, the actions taken, "
    "why the classifier stopped, a capture of the screen as it is now, the text read from that "
    "screen, and the text of the screens it passed through on the way, oldest first. You may also "
    "receive the focus the classifier was working on, the earlier times it stopped with the focus "
    "you gave each time, and what the user said when asked.\n\n"
    "Tell the user the result. When the goal asks for information, lead with that information, "
    "taken only from those screens and from what the user said: never from memory, and never a "
    "guess. When the goal asks for something to be done, say whether the screen shows it done. "
    "When the screen does not hold the result, say so plainly, then say what is on screen and the "
    "one next step that would get there. Trust the capture over the text where the two disagree. "
    "Plain text, no markdown, four sentences at most. Set achieved to true only when the screen "
    "itself shows the goal reached.\n\n"
    "When the goal is not reached you may keep the run going, in one of two ways and never both. "
    "Set focus to send the classifier back to work: one short imperative sentence naming the next "
    "step in terms of what this screen shows, quoting the text of the item to use when there is "
    "one. The classifier can click an on-screen item, type into a focused field, press Return or "
    "Escape, scroll, go back, wait, and open a website; it cannot read, compare, or remember, so a "
    "focus is one move, not a plan. Do not give again a focus from earlier_stops that changed "
    "nothing. Set question to ask the user, only when user_can_be_asked is true, and only for what "
    "the screens cannot tell you and the goal leaves open: a choice between options the user would "
    "care about, or a fact only the user has. One short question. Never ask for a password or any "
    "other credential, and never ask what user_said already answers. Leave both empty when the "
    "goal is reached, when no action of the agent's would help, or when the next step is one only "
    "the user should take, such as a login or a payment."
)


def compose_answer(
    writer: anthropic.Anthropic,
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    stopped: str,
    earlier: list[dict] | None = None,
    guidance: Guidance | None = None,
    earlier_stops: list[dict] | None = None,
    can_ask: bool = False,
) -> Answer:
    """What to tell the user now that the classifier has stopped, and how the run could go on.

    The classifier can stop on the right page but cannot say what the page says. The writer reads the
    capture itself as well as its text, since OCR misreads a letter here and there and drops layout.
    `earlier` is the text of the screens before this one, for a goal whose answer was on the way.
    `earlier_stops` are the times the classifier stopped before, each with the focus it was sent
    back with, so a focus that led nowhere is not given twice.
    """
    packet = {
        "goal": goal,
        **(guidance.state() if guidance else {}),
        "now": now_context(),
        "why_the_run_stopped": stopped,
        "actions_taken": history,
        **({"earlier_stops": earlier_stops} if earlier_stops else {}),
        "user_can_be_asked": can_ask,
        "frontmost_app": screen.app,
        "browser_active_tab_url": screen.url,
        "screen_text_in_reading_order": [it.text for it in items],
        **({"earlier_screens": earlier} if earlier else {}),
    }
    data = _structured(
        writer,
        system=ANSWER_SYSTEM,
        packet=packet,
        properties={
            "achieved": {"type": "boolean"},
            "answer": {"type": "string"},
            "focus": {"type": "string"},
            "question": {"type": "string"},
        },
        max_tokens=1024,
        model=answer_model(),
        image=screen.image,
    )
    return Answer(
        text=data["answer"].strip(), achieved=data["achieved"], focus=data["focus"].strip(), question=data["question"].strip()
    )
