"""Execute one decided action. Every function returns a one-line description for the history."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from typesafe_sdk import TypeSafeClient

from . import macos
from .config import SITES
from .decide import OFFSCREEN_PREFIX, Decision, row_mates, verify_typed
from .models import Field, Guidance, Item, Screen
from .writer import Writer, WriterError, compose_text, compose_url

VERIFY_THRESHOLD = 0.5
WAIT_SECONDS = 3.0  # what a wait adds to the settle delay every step already gets; three of them cover a slow page


@dataclass(frozen=True)
class Context:
    goal: str
    browser: str
    email: str | None
    typesafe: TypeSafeClient
    writer: Writer | None
    history: list[str]
    ask: Callable[[str], str] | None = None  # puts the writer's question to the user; None when nobody is there to answer
    guidance: Guidance = field(default_factory=Guidance)  # the runner replaces the context when the writer or the user adds to it


def perform(decision: Decision, screen: Screen, items: list[Item], ctx: Context) -> str:
    macos.check_abort()
    key = decision.chosen
    by_index = {str(it.index): it for it in items}
    if key in by_index:
        what = click_item(by_index[key], screen)
        mates = row_mates(items, limit=None).get(int(key))
        # Three rows each end in a Buy: the line must say which, or trying one marks them all as tried.
        # The whole row goes in, so two rows that open alike still get lines of their own.
        return f"{what} beside {', '.join(repr(m) for m in mates)}" if mates else what
    if key.startswith(OFFSCREEN_PREFIX):
        return press_offscreen(key[len(OFFSCREEN_PREFIX) :], screen)
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


def press_offscreen(key: str, screen: Screen) -> str:
    """Press a control the app exposes but does not show.

    AXPress does not need the element to be visible: a note row scrolled thousands of points down
    and a link the browser parked above the viewport both take it. There is no pixel to fall back
    on, so a refusal is the end of it and reads as a no-op.
    """
    nodes = screen.offscreen
    node = nodes[int(key)] if key.isdigit() and int(key) < len(nodes) else None
    if node is None:
        return f"press_offscreen refused: there is no off-screen control {key!r}"
    if macos.ax_press(node.ref):
        return f"pressed {node.label!r} (off-screen control) via accessibility"
    return f"press_offscreen refused: {node.label!r} did not accept the press"


def fill_field(field: Field, text: str) -> str:
    """Put text in the focused field, by value if the element accepts one and keystrokes otherwise.

    Setting the value is one message instead of one per character, and it cannot be stolen by a
    page that moves the focus mid-word. It is also widely ignored, so the value is read back and
    only a field that really holds the text counts. Keystrokes land after whatever the field
    holds, including a value the element took but did not read back, so the field is always
    emptied first. Returns which path ran, for the history.
    """
    ref = field.ref
    if ref is not None:
        macos.ax_focus(ref)
        if macos.ax_set_value(ref, text):
            back = macos.ax_value(ref)
            if back is not None and back.endswith(text):
                return "via accessibility"
    macos.clear_field()
    macos.type_text(text)
    return "via keystrokes"


def restore_field(field: Field, typed: str) -> bool:
    """Undo only our write, through the original element, never through the current focus.

    If the element disappeared or its value changed again, leave it alone. A failed AX restore
    has no keyboard fallback: Select All/Delete could destroy an unrelated field's contents.
    """
    ref = field.ref
    if ref is None or macos.ax_value(ref) != typed:
        return False
    return macos.ax_set_value(ref, field.value) and macos.ax_value(ref) == field.value


def _use_browser(decision: Decision, screen, items, ctx: Context) -> str:
    """Go to the browser, and open the website the site answer named.

    `none` is the page already open there, so bringing the browser forward is the whole action. A
    catalog key is its URL, and `other` is a site outside the catalog, which only the writer can
    name. Opening a URL activates the browser too, so the three cases differ only in the page.
    """
    site = decision.site.choice
    if site == "none":
        if macos.activate(ctx.browser):
            return f"activated {ctx.browser}"
        return f"use_browser failed: {ctx.browser} did not come to the front"
    url = SITES.get(site)
    if url is None:
        if ctx.writer is None:
            return "use_browser refused: the site is outside the catalog and no writer is available to propose a URL"
        try:
            url = compose_url(ctx.writer, ctx.goal, ctx.history, ctx.guidance)
        except WriterError as e:
            return f"use_browser refused: the writer failed ({e})"
    if not url:
        return "use_browser refused: the writer proposed no usable URL for this goal"
    if macos.open_url(ctx.browser, url):
        return f"opened {url}"
    return f"use_browser failed: opened {url} but {ctx.browser} did not come to the front"


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
    try:
        text = compose_text(ctx.writer, ctx.goal, screen, items, ctx.history, ctx.guidance)
    except WriterError as e:
        return f"type_text refused: the writer failed ({e})"
    if not text:
        return "type_text refused: writer declined to fill this field"
    how = fill_field(screen.field, text)
    time.sleep(0.3)
    p = verify_typed(ctx.typesafe, ctx.goal, screen.field, text, macos.focused_field())
    if p < VERIFY_THRESHOLD:
        recovery = "restored previous value" if restore_field(screen.field, text) else "could not safely restore previous value"
        return f"typed {text!r} into {screen.field.label!r} {how} but verification failed ({p:.2f}); {recovery}"
    return f"typed {text!r} into {screen.field.label!r} {how} (verified {p:.2f})"


def _key(name: str, description: str, command: bool = False):
    def handler(decision, screen, items, ctx) -> str:
        macos.press(name, command)
        return description

    return handler


def _scroll(lines: int, description: str):
    def handler(decision, screen, items, ctx) -> str:
        macos.scroll(lines)
        return description

    return handler


def _wait(decision, screen, items, ctx) -> str:
    """Give a loading page time. The step's own delay follows, so a wait is worth both."""
    macos.sleep_watching(WAIT_SECONDS)
    return "waited"


_HANDLERS = {
    "use_browser": _use_browser,
    "type_email": _type_email,
    "type_text": _type_text,
    "press_enter": _key("return", "pressed Return"),
    "press_escape": _key("escape", "pressed Escape"),
    "go_back": _key("[", "went back", command=True),
    "scroll_down": _scroll(-10, "scrolled down"),
    "scroll_up": _scroll(10, "scrolled up"),
    "wait": _wait,
}
