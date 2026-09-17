"""The writer model: the only place free text is generated, and only when the classifier asks for it."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import anthropic

from .config import writer_model
from .dates import now_context
from .models import Item, Screen
from .perception import near_field


def make_writer() -> anthropic.Anthropic | None:
    """A client, or None when no Anthropic credentials resolve (the SDK only checks on first request)."""
    client = anthropic.Anthropic()
    if client.api_key or getattr(client, "auth_token", None):
        return client
    return None


def _structured(writer: anthropic.Anthropic, system: str, packet: dict, properties: dict, max_tokens: int) -> dict:
    response = writer.messages.create(
        model=writer_model(),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": json.dumps(packet)}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            }
        },
    )
    return json.loads("".join(b.text for b in response.content if b.type == "text"))


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


def looks_credential(label: str) -> bool:
    lowered = (label or "").lower()
    return any(hint in lowered for hint in CREDENTIAL_HINTS)


def compose_browser_text(
    writer: anthropic.Anthropic,
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
    text = str(data.get("text", "")).strip() if data.get("fill") else ""
    # Guard again on the way out: a label that looked innocent can still attract
    # a credential-shaped value.
    if looks_credential(field_label) or looks_credential(text):
        return ""
    return text


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
