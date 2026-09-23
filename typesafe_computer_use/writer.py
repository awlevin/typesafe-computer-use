"""The writer model: the only place free text is generated, when the classifier asks for it and whenever the classifier stops."""

from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlparse

import anthropic
import openai
from PIL import Image

from .config import (
    ANTHROPIC_HOST,
    answer_model,
    custom_writer_endpoint,
    writer_api,
    writer_base_url,
    writer_model,
    writer_vision,
)
from .dates import now_context
from .models import Field, Guidance, Item, Screen
from .openai_writer import OpenAIWriter
from .perception import near_field

ANSWER_IMAGE_EDGE = 1568  # the longest edge a vision model reads without shrinking the image itself
PLACEHOLDER_KEY = "not-needed"  # an endpoint you host yourself does not check a key


type Writer = anthropic.Anthropic | OpenAIWriter | Configured  # all answer `messages.create` the Anthropic way


class WriterError(Exception):
    """The writer could not be reached, or answered with nothing usable. The step it served is refused."""


def make_writer() -> Writer | None:
    """A client, or None when there is nothing to write with.

    A key only ever goes to the endpoint it belongs to. CLICKER_WRITER_BASE_URL (see
    config.writer_base_url) is paired with CLICKER_WRITER_API_KEY, or with a placeholder when that
    is unset, since an endpoint you host yourself checks no key. Otherwise the SDK reads the
    ANTHROPIC_* key, token, and base URL as it always does. An endpoint that speaks OpenAI's API
    (CLICKER_WRITER_API=openai) has no default to fall back on, so it must be named.
    """
    base_url = writer_base_url()
    key = os.environ.get("CLICKER_WRITER_API_KEY") or PLACEHOLDER_KEY
    if writer_api() == "openai":
        if not base_url:
            raise ValueError("CLICKER_WRITER_API=openai needs CLICKER_WRITER_BASE_URL, such as http://localhost:1234/v1")
        return OpenAIWriter(base_url, key)
    if base_url:
        # Proxies differ in which header they read, so the key goes in both. Passing any key at all
        # also keeps the SDK from adding ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN on its own.
        return anthropic.Anthropic(base_url=base_url, api_key=key, auth_token=key)
    client = anthropic.Anthropic()
    if client.api_key or client.auth_token:
        return client
    return None


def client_for(api: str, base_url: str | None, key: str | None) -> anthropic.Anthropic | OpenAIWriter | None:
    """A client for one endpoint the settings name, or None when it has nothing to authenticate with.

    The same two clients `make_writer` builds, with the same rule: a key goes only to the endpoint
    it was given for. A key for Anthropic's own API goes to that API by name, never to an
    ANTHROPIC_BASE_URL the environment may hold for another key. With no key at all, the SDK reads
    the environment's own pair, ANTHROPIC_AUTH_TOKEN included, exactly as `make_writer` does.
    """
    if api == "openai":
        if not base_url:
            raise ValueError("an OpenAI-compatible writer needs a base URL, such as http://localhost:1234/v1")
        return OpenAIWriter(base_url, key or PLACEHOLDER_KEY)
    if base_url:
        return anthropic.Anthropic(base_url=base_url, api_key=key or PLACEHOLDER_KEY, auth_token=key or PLACEHOLDER_KEY)
    if key:
        return anthropic.Anthropic(api_key=key, base_url=f"https://{ANTHROPIC_HOST}")
    client = anthropic.Anthropic()
    return client if client.api_key or client.auth_token else None


class Configured:
    """A writer client with the model the settings chose for it, and whether that model reads images.

    The environment names one endpoint and two models, one for writing and one for answering. The
    settings may name two endpoints, so each carries its own model, and every request through it is
    sent to that model whatever the caller asked. `custom` is what `custom_writer_endpoint` says of
    the environment, said of this endpoint.
    """

    def __init__(self, client: anthropic.Anthropic | OpenAIWriter, model: str, vision: bool, label: str):
        self.client = client
        self.model = model
        self.vision = vision
        self.label = label
        self.base_url = client.base_url
        self.custom = isinstance(client, OpenAIWriter) or client.base_url.host != ANTHROPIC_HOST
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        return self.client.messages.create(**{**request, "model": self.model})


def speaks_to_anthropic(writer: Writer) -> bool:
    """Whether the writer's endpoint is Anthropic's own API: said by a configured writer, else by the environment."""
    custom = getattr(writer, "custom", None)
    return not (custom_writer_endpoint() if custom is None else custom)


def reads_images(writer: Writer) -> bool:
    """Whether the answer model gets the screenshot. CLICKER_WRITER_VISION, when set, decides; otherwise
    a configured writer knows its own model, and anything else reads images as it always did."""
    if os.environ.get("CLICKER_WRITER_VISION", "").strip():
        return writer_vision()
    vision = getattr(writer, "vision", None)
    return writer_vision() if vision is None else bool(vision)


def provider(writer: Writer) -> str:
    """Where the writer sends its requests, for logging. Credentials and query in the URL are left out."""
    if isinstance(writer, Configured):
        return writer.label
    url = writer.base_url
    port = f":{url.port}" if url.port else ""
    where = f"{url.scheme}://{url.host}{port}{url.path.rstrip('/')}"
    return f"{where} ({writer_api()} API)  models: {writer_model()} (writing), {answer_model()} (answering)"


def _structured(
    writer: Writer,
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
    if not speaks_to_anthropic(writer):
        # Anthropic enforces output_config. Another endpoint may ignore it without a word, so the
        # schema is spelled out in the prompt as well.
        system = f"{system}\n\nAnswer with a single JSON object and nothing else, matching this schema:\n{json.dumps(schema)}"
        # Another endpoint may think by default, out of the same max_tokens: a 200-token call then
        # comes back with no text at all. Anthropic thinks only when asked.
        extra["thinking"] = {"type": "disabled"}
    try:
        response = writer.messages.create(
            model=model or writer_model(),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            **extra,
        )
    except (anthropic.APIError, openai.APIError) as e:
        raise WriterError(f"the request failed: {e}") from e
    return checked(parse_json("".join(b.text for b in response.content if b.type == "text")), properties)


def parse_json(text: str) -> dict:
    """The first JSON object in a reply, as long as one is in there.

    A model that was asked for JSON usually returns exactly that. Some wrap it in code fences or a
    sentence, so the object is looked for rather than assumed to fill the whole reply. A reply cut
    off before its object closes holds none, and nothing is guessed out of it.
    """
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            data, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise WriterError(f"the writer answered without usable JSON: {text[:400]!r}")


def checked(data: dict, properties: dict) -> dict:
    """The reply with each field of its schema's type. Anthropic enforces the schema itself; another
    endpoint may leave a field out or send a string where a flag belongs. A flag decides what happens
    next, so one missing is an error. A missing string is empty, which every caller reads as nothing
    to type, open, or say."""
    out = dict(data)
    for name, spec in properties.items():
        if spec["type"] == "string" and name not in out:
            out[name] = ""
        if not isinstance(out.get(name), {"boolean": bool, "string": str, "array": list}[spec["type"]]):
            raise WriterError(f"the writer's reply has no {spec['type']} {name!r}: {json.dumps(data)[:400]}")
    return out


def _image_block(image: Image.Image) -> dict:
    """The capture as a PNG the model can read. PNG because screen text does not survive JPEG well."""
    shrunk = image.convert("RGB")
    shrunk.thumbnail((ANSWER_IMAGE_EDGE, ANSWER_IMAGE_EDGE))
    buffer = io.BytesIO()
    shrunk.save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def compose_text(
    writer: Writer,
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    guidance: Guidance | None = None,
) -> str:
    """The exact string to type into the focused field. Empty means the writer declined or was refused.

    A field that asks for a credential is refused before any request is made, whichever endpoint
    the writer is: the prompt tells the model not to fill one, and this is the code-side guard.
    """
    if screen.field is None or credential_field(screen.field):
        return ""
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


ICON_SYSTEM = (
    "Each red box on this crop of a screenshot is a control with no name of its own. For every numbered "
    "box, say in two or three words what that control does, as a person would say it: 'close window', "
    "'add a note', 'play', 'share'. Judge it from the icon and from what surrounds it. When you cannot "
    "tell, give an empty name rather than a guess: a wrong name here becomes a wrong click."
)


def compose_icon_names(writer: Writer, app: str, roles: list[str], image: Image.Image) -> dict[int, str]:
    """A short name for each numbered box on `image`, as far as the model can tell; the rest are left out.

    Icon names are free text a click is chosen by, so they come from here like every other string the
    loop acts on, and only numbers that were drawn can come back.
    """
    data = _structured(
        writer,
        system=ICON_SYSTEM,
        packet={"app": app, "boxes": [{"box": i, "shape": role} for i, role in enumerate(roles)]},
        properties={
            "names": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"box": {"type": "integer"}, "name": {"type": "string"}},
                    "required": ["box", "name"],
                    "additionalProperties": False,
                },
            }
        },
        max_tokens=600,
        model=answer_model(),
        image=image,
    )
    named: dict[int, str] = {}
    for entry in data["names"]:
        index = entry.get("box") if isinstance(entry, dict) else None
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(index, int) and 0 <= index < len(roles) and isinstance(name, str) and name.strip():
            named[index] = " ".join(name.split())[:60]
    return named


def valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and "." in parsed.netloc and not any(ch.isspace() for ch in url)


# --------------------------------------------------------------------------- #
# Browser backend. Free text stays in this module, per CONTRIBUTING: the writer
# is the only place free text is generated, replies are structured, and a
# code-side guard runs before anything is typed.
# --------------------------------------------------------------------------- #

# Code-side guard. The writer is told not to fill credentials, and this is the
# second line of defence: never add a path that types a password.
CREDENTIAL_HINTS = (
    "password",
    "passwd",
    "passcode",
    "passphrase",
    "one-time",
    "one time",
    "otp",
    "2fa",
    "mfa",
    "security code",
    "pin",
    "cvv",
    "cvc",
    "card number",
    "credit card",
    "expiry",
    "social security",
    "ssn",
    "secret",
    "api key",
    "token",
    "private key",
    "recovery code",
    "seed phrase",
)


# A short hint matches as a whole word only: "pin" is in "Shipping address" and "Typing speed" too.
SHORT_HINT = 4
_CREDENTIAL = re.compile(
    "|".join(
        rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])" if len(hint) <= SHORT_HINT else re.escape(hint)
        for hint in CREDENTIAL_HINTS
    )
)


def looks_credential(label: str) -> bool:
    return _CREDENTIAL.search((label or "").lower()) is not None


def credential_field(field: Field) -> bool:
    """Whether a focused field asks for a credential: the platform marks it secure, or its label or
    placeholder names one. Nothing is typed or pasted into such a field on any path."""
    return field.secure or looks_credential(field.label) or looks_credential(field.placeholder)


def compose_browser_text(
    writer: Writer,
    goal: str,
    *,
    field_label: str,
    page_title: str,
    url: str,
    nearby_text: list[str],
    history: list[str],
) -> str:
    """The exact string to type into a browser field. Empty means declined or refused."""
    if looks_credential(field_label):
        return ""
    packet = {
        "goal": goal,
        "now": now_context(),
        "page": {"title": page_title, "url": url},
        "focused_field": field_label,
        "text_near_field": nearby_text,
        "previous_actions": history[-8:],
    }
    data = _structured(
        writer,
        system=(
            "You fill in one text field on a web page for a user working toward a goal. You "
            "receive the goal, recent actions, the field's label or placeholder, and nearby page "
            "text. Decide the exact string to type. Never invent credentials, passwords, one-time "
            "codes, card numbers, or personal data; for such fields, or when the field should not "
            "be filled, set fill to false. Keep it short and literal - no explanation."
        ),
        packet=packet,
        properties={"fill": {"type": "boolean"}, "text": {"type": "string"}, "reason": {"type": "string"}},
        max_tokens=256,
    )
    text = data["text"].strip() if data["fill"] else ""
    # Guard again on the way out: a label that looked innocent can still attract
    # a credential-shaped value.
    if looks_credential(field_label) or looks_credential(text):
        return ""
    return text


def compose_url(writer: Writer, goal: str, history: list[str], guidance: Guidance | None = None) -> str:
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
    writer: Writer,
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
        image=screen.image if reads_images(writer) else None,
    )
    return Answer(
        text=data["answer"].strip(), achieved=data["achieved"], focus=data["focus"].strip(), question=data["question"].strip()
    )
