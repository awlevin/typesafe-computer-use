"""Execute a decision through CDP. Each handler returns a history line.

Every action is a real input event (`Input.dispatchMouseEvent` /
`Input.insertText`), not a JS `.click()`, so frameworks that listen for genuine
pointer events behave exactly as they would for a human.
"""

from __future__ import annotations

import time
from typing import Any

from .cdp import Session
from .perceive import Element, Page, perceive

# CDP expects these key codes; enter is the only one the loop needs beyond typing.
KEYS = {
    "enter": ("Enter", "\r"),
    "escape": ("Escape", "\u001b"),
}


def _select(index: int) -> str:
    return f'[data-tscu="{index}"]'


def element_rect(session: Session, index: int) -> dict | None:
    return session.evaluate(
        f"""(() => {{
          const el = document.querySelector({_select(index)!r});
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return {{x: Math.round(r.left + Math.min(r.width,1400)/2), y: Math.round(r.top + r.height/2),
                   w: Math.round(r.width), h: Math.round(r.height), vw: innerWidth, vh: innerHeight}};
        }})()"""
    )


def is_gone(session: Session, index: int) -> bool:
    return session.evaluate(f"!document.querySelector({_select(index)!r})") is True


def click(session: Session, index: int, element: Element, page: Page) -> str:
    """Real mouse press/release at the element's centre."""
    if not element.in_view:
        session.evaluate(
            f"""(() => {{
              const el = document.querySelector({_select(index)!r});
              if (el) el.scrollIntoView({{block: "center", inline: "center"}});
              return true;
            }})()"""
        )
        time.sleep(0.05)
    rect = element_rect(session, index)
    if rect is None:
        return f"click {index} FAILED (element gone)"
    x = max(1, min(int(rect["x"]), max(1, int(rect["vw"]) - 2)))
    y = max(1, min(int(rect["y"]), max(1, int(rect["vh"]) - 2)))
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        session.call(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1, "buttons": 1 if kind != "mouseReleased" else 0},
        )
    return f"click [{index}] {element.name[:60]!r} at ({x},{y})"


def focus(session: Session, index: int) -> bool:
    return (
        session.evaluate(
            f"""(() => {{
          const el = document.querySelector({_select(index)!r});
          if (!el) return false;
          el.focus();
          if (el.select) {{ try {{ el.select(); }} catch (e) {{}} }}
          return document.activeElement === el;
        }})()"""
        )
        is True
    )


def type_text(session: Session, index: int, text: str) -> str:
    if not focus(session, index):
        return f"type FAILED (could not focus [{index}])"
    session.call("Input.insertText", {"text": text})
    return f"type {text!r} into [{index}]"


def field_value(session: Session, index: int) -> str | None:
    """What a field holds after the loop typed into it, for the verify check. A password field
    is never read: the loop never types into one, and this makes sure of it a second time."""
    value = session.evaluate(
        f"""(() => {{ const el = document.querySelector({_select(index)!r});
                     if (!el || el.value === undefined || String(el.type).toLowerCase() === "password") return null;
                     return String(el.value); }})()"""
    )
    return None if value is None else str(value)


def clear_field(session: Session, index: int) -> None:
    focus(session, index)
    session.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "modifiers": 4, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65},
    )
    session.call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "modifiers": 4, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65},
    )
    session.call("Input.insertText", {"text": ""})
    session.call(
        "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8}
    )
    session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})


def press(session: Session, key: str) -> str:
    k, text = KEYS[key]
    for event in ("keyDown", "keyUp"):
        session.call(
            "Input.dispatchKeyEvent",
            {
                "type": event,
                "key": k,
                "text": text if event == "keyDown" else "",
                "unmodifiedText": text,
                "windowsVirtualKeyCode": 13 if key == "enter" else 27,
            },
        )
    return f"press {key}"


def scroll(session: Session, lines: int, page: Page) -> str:
    session.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseWheel", "x": max(1, page.vw // 2), "y": max(1, page.vh // 2), "deltaX": 0, "deltaY": lines * 120},
    )
    return f"scroll {'down' if lines > 0 else 'up'} {abs(lines)} lines"


def navigate(session: Session, url: str, *, timeout_ms: int = 15000) -> str:
    session.call("Page.navigate", {"url": url})
    return f"navigate {url}"


def wait_for_load(session: Session, *, timeout_ms: int = 15000, settle_ms: int = 120) -> float:
    """Fixed-sleep variant, kept for callers that want deterministic pacing."""
    start = time.perf_counter()
    deadline = start + timeout_ms / 1000
    while time.perf_counter() < deadline:
        if session.evaluate("document.readyState") == "complete":
            break
        time.sleep(0.03)
    time.sleep(settle_ms / 1000)
    return (time.perf_counter() - start) * 1000


def fingerprint(page: Page) -> tuple:
    """Cheap identity for 'is this still the same page?'."""
    return (page.url, page.title, page.scroll_y, tuple((e.index, e.name, e.x, e.y) for e in page.items))


def observe_until_changed(
    session: Session,
    before: tuple | None,
    *,
    timeout_ms: int = 300,
    poll_ms: int = 8,
    budget: int = 120,
) -> tuple[Page, float, bool]:
    """Perceive until the page differs from `before`. Returns the post-action page.

    This replaces the settle sleep entirely. The observation we need for the
    *next* decision doubles as the wait for *this* action, so waiting costs no
    extra round trips: in the common case the change lands within ~20-40ms and
    we detect it on the first or second poll.

    The timeout is deliberately short. A page that has not changed in 300ms is
    better handled by the loop's own `wait` action than by stalling here.
    """
    start = time.perf_counter()
    if before is None:
        page = perceive(session, budget=budget)
        return page, (time.perf_counter() - start) * 1000, True
    deadline = start + timeout_ms / 1000
    page = perceive(session, budget=budget)
    while time.perf_counter() < deadline:
        if fingerprint(page) != before:
            return page, (time.perf_counter() - start) * 1000, True
        time.sleep(poll_ms / 1000)
        page = perceive(session, budget=budget)
    return page, (time.perf_counter() - start) * 1000, False


def go_back(session: Session) -> str:
    session.evaluate("history.back(); true")
    return "back"


def snapshot(session: Session, **kwargs: Any) -> Page:
    return perceive(session, **kwargs)
