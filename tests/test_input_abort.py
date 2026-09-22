"""Exercise the real input adapter with OS calls replaced; no synthetic input reaches the host."""

from types import SimpleNamespace

import pytest

from typesafe_computer_use import actions, macos
from typesafe_computer_use.models import Abort


@pytest.fixture
def input_events(monkeypatch):
    events = []
    quartz = SimpleNamespace(
        kCGEventMouseMoved="move",
        kCGEventLeftMouseDown="down",
        kCGEventLeftMouseUp="up",
        kCGMouseButtonLeft=0,
        CGEventCreateMouseEvent=lambda _, kind, point, button: (kind, point),
        CGEventCreateKeyboardEvent=lambda _, code, down: {"down": down},
        CGEventKeyboardSetUnicodeString=lambda event, length, text: event.update(text=text),
        kCGEventFlagMaskCommand="command",
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        kCGScrollEventUnitLine="line",
        CGEventCreateScrollWheelEvent=lambda _, unit, count, lines: ("scroll", lines),
    )
    monkeypatch.setattr(macos, "Quartz", quartz)
    monkeypatch.setattr(macos, "_post", events.append)
    return events


def test_corner_abort_prevents_the_click_from_moving_the_pointer(input_events, monkeypatch):
    monkeypatch.setattr(macos, "mouse_location", lambda: (0, 0))
    with pytest.raises(Abort):
        macos.click_at((200, 200))
    assert input_events == []


def test_typing_checks_abort_between_characters_and_releases_the_key(input_events, monkeypatch):
    monkeypatch.setattr(macos, "mouse_location", lambda: (0, 0) if input_events else (500, 500))
    with pytest.raises(Abort):
        macos.type_text("abc")
    assert input_events == [{"down": True, "text": "a"}, {"down": False, "text": "a"}]


def test_click_releases_the_button_when_interrupted_after_mouse_down(input_events, monkeypatch):
    def post(event):
        input_events.append(event)
        if event[0] == "down":
            raise KeyboardInterrupt

    monkeypatch.setattr(macos, "_post", post)
    with pytest.raises(KeyboardInterrupt):
        macos.click_at((200, 200))
    assert [event[0] for event in input_events] == ["move", "down", "up"]


def test_a_key_goes_back_up_when_interrupted_after_key_down(input_events, monkeypatch):
    def post(event):
        input_events.append(dict(event))
        if event["down"]:
            raise KeyboardInterrupt

    monkeypatch.setattr(macos, "_post", post)
    with pytest.raises(KeyboardInterrupt):
        macos.press("a", command=True)
    assert input_events == [{"down": True, "flags": "command"}, {"down": False, "flags": "command"}]


def test_corner_abort_stops_a_scroll_before_the_pointer_is_parked(input_events, monkeypatch):
    monkeypatch.setattr(macos, "frontmost_window_center", lambda: (300.0, 300.0))
    monkeypatch.setattr(macos, "mouse_location", lambda: (0, 0))
    with pytest.raises(Abort):
        macos.scroll(-10)
    assert input_events == []


@pytest.mark.parametrize(
    "action", [lambda: macos.open_url("Google Chrome", "https://example.com"), lambda: macos.activate("Finder")]
)
def test_corner_abort_stops_an_app_switch_before_any_applescript(action, monkeypatch):
    monkeypatch.setattr(macos, "mouse_location", lambda: (0, 0))
    with pytest.raises(Abort):
        action()


def test_pending_abort_after_a_decision_prevents_dispatch(screen, monkeypatch):
    monkeypatch.setattr(macos, "mouse_location", lambda: (0, 0))
    monkeypatch.setattr(macos, "press", lambda *a: pytest.fail("must not submit after abort"))
    with pytest.raises(Abort):
        actions.perform(SimpleNamespace(chosen="press_enter"), screen, [], None)


@pytest.mark.parametrize(
    "action", [lambda: macos.ax_press(object()), lambda: macos.ax_focus(object()), lambda: macos.ax_set_value(object(), "text")]
)
def test_accessibility_helpers_do_not_swallow_abort(action, monkeypatch):
    monkeypatch.setattr(macos, "mouse_location", lambda: (0, 0))
    with pytest.raises(Abort):
        action()
