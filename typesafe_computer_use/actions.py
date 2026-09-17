"""Execute one decided action. Every function returns a one-line description for the history."""

from __future__ import annotations

import time
from dataclasses import dataclass

import anthropic
from typesafe_sdk import TypeSafeClient

from . import macos
from .config import SITES
from .decide import Decision, verify_typed
from .models import Field, Item, Screen
from .writer import compose_text, compose_url

VERIFY_THRESHOLD = 0.5
NOOP_MARKERS = ("refused", "failed", "waited")


@dataclass(frozen=True)
class Context:
    goal: str
    browser: str
    email: str | None
    typesafe: TypeSafeClient
    writer: anthropic.Anthropic | None
    history: list[str]


def is_noop(description: str) -> bool:
    return any(marker in description for marker in NOOP_MARKERS)


def perform(decision: Decision, screen: Screen, items: list[Item], ctx: Context) -> str:
    key = decision.chosen
    by_index = {str(it.index): it for it in items}
    if key in by_index:
        return click_item(by_index[key], screen)
    handler = _HANDLERS.get(key)
    if handler is None:
        raise ValueError(f"unknown action {key!r}")
    return handler(decision, screen, items, ctx)


def click_item(item: Item, screen: Screen) -> str:
    """Press an item the app declared through the accessibility tree; click the pixel under it otherwise.

    A press goes to the control itself, so it lands even when the center of the box is covered by
    a sticky header, a cookie banner, or a tooltip. An element that refuses still has a location.
    """
    ref = screen.ax_refs.get(item.index)
    if ref is not None and macos.ax_press(ref):
        return f"pressed {item.text!r} via accessibility"
    macos.click_at(screen.to_points(item))
    if ref is None:
        return f"clicked {item.text!r}"
    return f"clicked {item.text!r} (accessibility press did not take)"


def fill_field(field: Field, text: str) -> str:
    """Put text in the focused field, by value if the element accepts one and keystrokes otherwise.

    Setting the value is one message instead of one per character, and it cannot be stolen by a
    page that moves the focus mid-word. It is also widely ignored, so the value is read back and
    only a field that really holds the text counts. Returns which path ran, for the history.
    """
    ref = field.ref
    if ref is not None:
        macos.ax_focus(ref)
        if macos.ax_set_value(ref, text):
            back = macos.ax_value(ref)
            if back is not None and back.endswith(text):
                return "via accessibility"
    macos.type_text(text)
    return "via keystrokes"


def _switch_to_browser(decision, screen, items, ctx: Context) -> str:
    if macos.activate(ctx.browser):
        return f"activated {ctx.browser}"
    return f"switch_to_browser failed: {ctx.browser} did not come to the front"


def _open_site(decision: Decision, screen, items, ctx: Context) -> str:
    url = SITES.get(decision.site.choice) or (compose_url(ctx.writer, ctx.goal, ctx.history) if ctx.writer else "")
    if not url:
        return "open_site refused: no known site matches and no writer available to propose a URL"
    if macos.open_url(ctx.browser, url):
        return f"opened {url}"
    return f"open_site failed: opened {url} but {ctx.browser} did not come to the front"


def _type_email(decision, screen: Screen, items, ctx: Context) -> str:
    if not (screen.field and screen.field.is_text):
        return "type_email refused: no text field is focused"
    how = fill_field(screen.field, ctx.email or "")
    return f"typed email {how}"


def _type_text(decision, screen: Screen, items, ctx: Context) -> str:
    if not (screen.field and screen.field.is_text):
        return "type_text refused: no text field is focused"
    if ctx.writer is None:
        return "type_text refused: no writer available"
    text = compose_text(ctx.writer, ctx.goal, screen, items, ctx.history)
    if not text:
        return "type_text refused: writer declined to fill this field"
    how = fill_field(screen.field, text)
    time.sleep(0.3)
    p = verify_typed(ctx.typesafe, ctx.goal, screen.field, text, macos.focused_field())
    if p < VERIFY_THRESHOLD:
        macos.clear_field()
        return f"typed {text!r} into {screen.field.label!r} {how} but verification failed ({p:.2f}); cleared it"
    return f"typed {text!r} into {screen.field.label!r} {how} (verified {p:.2f})"


def _key(name: str, description: str):
    def handler(decision, screen, items, ctx) -> str:
        macos.press(name)
        return description

    return handler


def _scroll(lines: int, description: str):
    def handler(decision, screen, items, ctx) -> str:
        macos.scroll(lines)
        return description

    return handler


_HANDLERS = {
    "switch_to_browser": _switch_to_browser,
    "open_site": _open_site,
    "type_email": _type_email,
    "type_text": _type_text,
    "press_enter": _key("return", "pressed Return"),
    "press_escape": _key("escape", "pressed Escape"),
    "scroll_down": _scroll(-10, "scrolled down"),
    "scroll_up": _scroll(10, "scrolled up"),
    "wait": lambda decision, screen, items, ctx: "waited",
}
