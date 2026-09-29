"""Execute one decided action. Every function returns a one-line description for the history."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from typesafe_sdk import TypeSafeClient

from .config import SITES
from .decide import OFFSCREEN_PREFIX, Decision, row_mates, verify_typed
from .models import Field, Guidance, Item, Missed, Popup, Screen
from .platform_adapter import desktop
from .writer import Writer, WriterError, compose_text, compose_url

VERIFY_THRESHOLD = 0.5
WAIT_SECONDS = 0.5  # a wait's pause before the next look; a page that takes longer gets several (see runner.MAX_WAITS)
CLOSE_SECONDS = 0.5  # for a closed popup to leave the screen before the click under it
MENU_ITEM = "AXMenuItem"


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
    desktop.check_abort()
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
    A click there would land on a popup in front of the item, so that popup is closed first.
    """
    ref = screen.ax_refs.get(item.index)
    if ref is not None and desktop.ax_press(ref):
        return f"pressed {item.text!r} via accessibility"
    popup = screen.covered.get(item.index)
    closed = f"{close_popup(popup)} then " if popup is not None else ""
    try:
        desktop.click_at(screen.to_points(item))
    except Missed as e:
        return f"{closed}click refused: {item.text!r} was not clicked, {e}"
    if ref is None:
        return f"{closed}clicked {item.text!r}"
    return f"{closed}clicked {item.text!r} (accessibility press did not take)"


def close_popup(popup: Popup) -> str:
    """Close a popup with its own close button, or with Escape when it has none, and give it a moment
    to go. Never with any of its other buttons: those answer it, as 'Restore' restores the last
    session's tabs."""
    button = popup.close
    if button is not None:
        try:
            desktop.click_at((button.x + button.w / 2, button.y + button.h / 2))
        except Missed:
            button = None  # the pointer never got there
    if button is None:
        desktop.press("escape")
    desktop.sleep_watching(CLOSE_SECONDS)
    return f"closed {popup.title}" if button is not None else f"closed {popup.title} with Escape"


def press_offscreen(key: str, screen: Screen) -> str:
    """Press a control the app exposes but does not show.

    AXPress does not need the element to be visible: a note row scrolled thousands of points down
    and a link the browser parked above the viewport both take it. There is mostly no pixel to fall
    back on, so a refusal reads as a no-op. The one exception is an item of a menu the tree has open
    on the display and the capture does not show: the menu is there, not drawn yet (Chrome in
    OSWorld can leave a new menu unpainted for seconds), so it takes a click where the tree puts the
    item. A page's own undrawn control does not: it may be clipped out of sight, and a click there
    lands on the page.
    """
    nodes = screen.offscreen
    node = nodes[int(key)] if key.isdigit() and int(key) < len(nodes) else None
    if node is None:
        return f"press_offscreen refused: there is no off-screen control {key!r}"
    if desktop.ax_press(node.ref):
        return f"pressed {node.label!r} (off-screen control) via accessibility"
    if node.role == MENU_ITEM and node in screen.undrawn:
        try:
            desktop.click_at((node.x + node.w / 2, node.y + node.h / 2))
        except Missed as e:
            return f"press_offscreen refused: {node.label!r} was not clicked, {e}"
        return f"clicked {node.label!r} where the tree puts it, in a menu the capture does not show yet"
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
        desktop.ax_focus(ref)
        if desktop.ax_set_value(ref, text):
            back = desktop.ax_value(ref)
            if back is not None and back.endswith(text):
                return "via accessibility"
    desktop.clear_field()
    desktop.type_text(text)
    return "via keystrokes"


def restore_field(field: Field, typed: str) -> bool:
    """Undo only our write, through the original element, never through the current focus.

    If the element disappeared or its value changed again, leave it alone. A failed AX restore
    has no keyboard fallback: Select All/Delete could destroy an unrelated field's contents.
    """
    ref = field.ref
    if ref is None or desktop.ax_value(ref) != typed:
        return False
    return desktop.ax_set_value(ref, field.value) and desktop.ax_value(ref) == field.value


def _use_browser(decision: Decision, screen, items, ctx: Context) -> str:
    """Go to the browser, and open the website the site answer named.

    `none` is the page already open there, so bringing the browser forward is the whole action. A
    catalog key is its URL, and `other` is a site outside the catalog, which only the writer can
    name. Opening a URL activates the browser too, so the three cases differ only in the page.
    """
    site = decision.site.choice
    if site == "none":
        if desktop.activate(ctx.browser):
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
    if desktop.open_url(ctx.browser, url):
        return f"opened {url}"
    return f"use_browser failed: opened {url} but {ctx.browser} did not come to the front"


def _type_email(decision, screen: Screen, items, ctx: Context) -> str:
    if not (screen.field and screen.field.is_text):
        return "type_email refused: no text field is focused"
    how = fill_field(screen.field, ctx.email or "")
    return f"typed email {how}"


def _type_text(decision, screen: Screen, items, ctx: Context) -> str:
    """Fill the focused field with the writer's text, and check it, or submit it with Return.

    Text the writer submits is not checked: Return usually takes the field away (a dialog closes, a
    search runs), so a read after it no longer shows the field, and a read before it would split
    the keystrokes and the Return into two OSWorld steps. The next screen says whether it took.
    """
    if not (screen.field and screen.field.is_text):
        return "type_text refused: no text field is focused"
    if ctx.writer is None:
        return "type_text refused: no writer available"
    try:
        fill = compose_text(ctx.writer, ctx.goal, screen, items, ctx.history, ctx.guidance)
    except WriterError as e:
        return f"type_text refused: the writer failed ({e})"
    text = fill.text
    if not text:
        return "type_text refused: writer declined to fill this field"
    how = fill_field(screen.field, text)
    if fill.submit:
        desktop.press("return")
        return f"typed {text!r} into {screen.field.label!r} {how} and pressed Return"
    time.sleep(0.3)
    p = verify_typed(ctx.typesafe, ctx.goal, screen.field, text, desktop.focused_field())
    if p < VERIFY_THRESHOLD:
        recovery = "restored previous value" if restore_field(screen.field, text) else "could not safely restore previous value"
        return f"typed {text!r} into {screen.field.label!r} {how} but verification failed ({p:.2f}); {recovery}"
    return f"typed {text!r} into {screen.field.label!r} {how} (verified {p:.2f})"


def _key(name: str, description: str, command: bool = False):
    def handler(decision, screen, items, ctx) -> str:
        desktop.press(name, command)
        return description

    return handler


def _scroll(lines: int, description: str):
    def handler(decision, screen, items, ctx) -> str:
        desktop.scroll(lines)
        return description

    return handler


def _wait(decision, screen, items, ctx) -> str:
    """Give a loading page a moment, then look again. A short pause looks again sooner: the page
    may show what the goal needs before it finishes, and the next step acts on it then."""
    desktop.sleep_watching(WAIT_SECONDS)
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
