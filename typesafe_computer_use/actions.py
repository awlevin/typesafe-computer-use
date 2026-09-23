"""Execute one decided action. Every function returns a one-line description for the history."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from typesafe_sdk import TypeSafeClient

from .config import KEY_ACTIONS, SITES
from .decide import (
    DOUBLE_PREFIX,
    OFFSCREEN_PREFIX,
    RIGHT_PREFIX,
    Decision,
    apps_offered,
    menu_offered,
    row_mates,
    typable,
    verify_typed,
    windows_offered,
)
from .models import Field, Guidance, Item, Missed, Screen
from .platform_adapter import desktop
from .writer import Writer, WriterError, compose_text, compose_url, credential_field

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
    answerer: Writer | None = None  # reads the screen when the classifier stops; None leaves it to the writer
    apps: tuple[str, ...] = ()  # the applications open_app may bring up, read once per run
    guidance: Guidance = field(default_factory=Guidance)  # the runner replaces the context when the writer or the user adds to it


def perform(decision: Decision, screen: Screen, items: list[Item], ctx: Context) -> str:
    desktop.check_abort()
    key = decision.chosen
    by_index = {str(it.index): it for it in items}
    gesture = "click"
    for prefix, name in ((DOUBLE_PREFIX, "double"), (RIGHT_PREFIX, "right")):
        if key.startswith(prefix) and key[len(prefix) :] in by_index:
            key, gesture = key[len(prefix) :], name
    if key in by_index:
        item = by_index[key]
        what = click_item(item, screen) if gesture == "click" else click_twice_or_right(item, screen, gesture == "right")
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
    if ref is not None and desktop.ax_press(ref):
        return f"pressed {item.text!r} via accessibility"
    try:
        desktop.click_at(screen.to_points(item))
    except Missed as e:
        return f"click refused: {item.text!r} was not clicked, {e}"
    if ref is None:
        return f"clicked {item.text!r}"
    return f"clicked {item.text!r} (accessibility press did not take)"


def click_twice_or_right(item: Item, screen: Screen, right: bool) -> str:
    """A double or a right click, always on the pixel: an accessibility press is one activation, and
    neither a double click nor a context menu is that."""
    word = "right-clicked" if right else "double-clicked"
    try:
        desktop.click_at(screen.to_points(item), **({"right": True} if right else {"clicks": 2}))
    except Missed as e:
        return f"click refused: {item.text!r} was not {word}, {e}"
    return f"{word} {item.text!r}"


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
    if desktop.ax_press(node.ref):
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
    if credential_field(screen.field):
        return "type_email refused: the focused field asks for a credential"
    how = fill_field(screen.field, ctx.email or "")
    return f"typed email {how}"


def _type_text(decision, screen: Screen, items, ctx: Context) -> str:
    if not (screen.field and screen.field.is_text):
        return "type_text refused: no text field is focused"
    if credential_field(screen.field):
        return "type_text refused: the focused field asks for a credential"
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
    p = verify_typed(ctx.typesafe, ctx.goal, screen.field, text, desktop.focused_field())
    if p < VERIFY_THRESHOLD:
        recovery = "restored previous value" if restore_field(screen.field, text) else "could not safely restore previous value"
        return f"typed {text!r} into {screen.field.label!r} {how} but verification failed ({p:.2f}); {recovery}"
    return f"typed {text!r} into {screen.field.label!r} {how} (verified {p:.2f})"


def _press_key(decision, screen: Screen, items, ctx: Context) -> str:
    """One shortcut from the catalog. A name outside it is refused rather than improvised, and paste
    never reaches a credential field, the same as typing."""
    name = decision.key.choice if getattr(decision, "key", None) else ""
    action = KEY_ACTIONS.get(name)
    if action is None:
        return f"press_key refused: {name!r} is not a shortcut this knows"
    if name == "paste" and screen.field is not None and credential_field(screen.field):
        return "press_key refused: paste into a field that asks for a credential"
    if name == "backspace" and not typable(screen.field):
        return "press_key refused: backspace needs a text field that is not a credential field"
    desktop.press(action.key, command=action.command, shift=action.shift)
    return f"pressed {name}"


def _press_menu(decision, screen: Screen, items, ctx: Context) -> str:
    """Run a menu command through the accessibility tree, without the menu ever opening on screen."""
    key = decision.menu.choice if getattr(decision, "menu", None) else ""
    item = dict(menu_offered(screen)).get(int(key)) if key.isdigit() else None
    if item is None:
        return f"press_menu refused: there is no menu command {key!r} on offer"
    if desktop.ax_press(item.ref):
        return f"ran the menu command {item.path!r}"
    return f"press_menu refused: {item.path!r} did not accept the press"


def _focus_window(decision, screen: Screen, items, ctx: Context) -> str:
    key = decision.window.choice if getattr(decision, "window", None) else ""
    window = dict(windows_offered(screen)).get(int(key)) if key.isdigit() else None
    if window is None:
        return f"focus_window refused: there is no other window {key!r}"
    if desktop.raise_window(window.ref):
        return f"brought the window {window.title!r} to the front"
    return f"focus_window refused: {window.title!r} would not come forward"


def _open_app(decision, screen: Screen, items, ctx: Context) -> str:
    """Bring up the app the app answer named, launching it when it is not running.

    Nothing on screen points at an app that is not open, so this is the one action that is not a
    reply to the capture. An app outside what was offered is refused rather than guessed at: the
    list is this machine's own, less the browser and the app already in front.
    """
    name = decision.app.choice if getattr(decision, "app", None) else ""
    if name not in apps_offered(ctx.apps, screen, ctx.browser):
        return f"open_app refused: {name!r} is not an app on offer"
    if desktop.activate(name):
        return f"opened {name}"
    return f"open_app failed: {name} did not come to the front"


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
    """Give a loading page time. The step's own delay follows, so a wait is worth both."""
    desktop.sleep_watching(WAIT_SECONDS)
    return "waited"


_HANDLERS = {
    "press_key": _press_key,
    "press_menu": _press_menu,
    "focus_window": _focus_window,
    "open_app": _open_app,
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
