"""The macOS adapter on a second display (#38), with Quartz, AX, and screencapture replaced.

The main display is 1440x900 points at the origin. A built-in display sits to its left and lower
down, at (-1512, 200), 1512x982 points, captured at 2x. The frontmost window is on the built-in one.
"""

from types import SimpleNamespace

import pytest
from PIL import Image

from typesafe_computer_use import macos
from typesafe_computer_use.models import AxNode

# conftest's guard replaces macos.screenshot for every test; this is the real one, taken at import.
SCREENSHOT = macos.screenshot

MAIN, BUILT_IN = 1, 2
BOUNDS = {MAIN: (0.0, 0.0, 1440.0, 900.0), BUILT_IN: (-1512.0, 200.0, 1512.0, 982.0)}
CAPTURE_PX = {1: (2880, 1800), 2: (3024, 1964)}  # what screencapture -D n writes
WINDOW = (-1400.0, 300.0, 800.0, 600.0)  # global points, on the built-in display
PID = 4242


def rect(x, y, w, h):
    return SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=w, height=h))


@pytest.fixture
def two_displays(monkeypatch):
    events, captured = [], []
    windows = [
        {"kCGWindowOwnerPID": 1, "kCGWindowLayer": 25, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1440, "Height": 24}},
        {
            "kCGWindowOwnerPID": PID,
            "kCGWindowLayer": 0,
            "kCGWindowBounds": dict(zip(("X", "Y", "Width", "Height"), WINDOW, strict=True)),
        },
        {"kCGWindowOwnerPID": 7, "kCGWindowLayer": 0, "kCGWindowBounds": {"X": 100, "Y": 100, "Width": 900, "Height": 700}},
    ]
    quartz = SimpleNamespace(
        kCGWindowListOptionOnScreenOnly=1,
        kCGWindowListExcludeDesktopElements=16,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda options, window: windows,
        CGMainDisplayID=lambda: MAIN,
        CGGetActiveDisplayList=lambda most, ids, count: (0, (MAIN, BUILT_IN), 2),
        CGDisplayBounds=lambda display: rect(*BOUNDS[display]),
        kCGEventMouseMoved="move",
        kCGEventLeftMouseDown="down",
        kCGEventLeftMouseUp="up",
        kCGMouseButtonLeft=0,
        CGEventCreateMouseEvent=lambda _, kind, point, button: (kind, tuple(point)),
    )

    def run(args, **kwargs):
        assert args[0] == "screencapture"
        display = int(args[args.index("-D") + 1])
        captured.append(display)
        Image.new("RGB", CAPTURE_PX[display]).save(args[-1])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(macos, "Quartz", quartz)
    monkeypatch.setattr(macos, "subprocess", SimpleNamespace(run=run, CalledProcessError=Exception))
    monkeypatch.setattr(macos, "screenshot", SCREENSHOT)
    monkeypatch.setattr(macos, "_post", events.append)
    monkeypatch.setattr(macos, "frontmost_pid", lambda: PID)
    monkeypatch.setattr(macos, "_display", None, raising=False)  # what the captures below choose is undone after the test
    return SimpleNamespace(events=events, captured=captured, windows=windows)


def test_a_window_on_a_second_display_is_captured_there_and_read_in_its_points(two_displays):
    image = macos.screenshot()
    assert two_displays.captured == [BUILT_IN]
    assert image.size == CAPTURE_PX[BUILT_IN]
    assert macos.display_scale(image) == 2.0
    # The window, relative to the built-in display's top-left corner.
    assert macos.frontmost_window_bounds(PID) == (112.0, 100.0, 800.0, 600.0)


def test_a_click_on_a_second_display_capture_lands_on_that_display(two_displays):
    macos.screenshot()
    macos.click_at((150.0, 120.0))
    assert [point for _, point in two_displays.events] == [(-1362.0, 320.0)] * 3


def test_controls_on_a_second_display_are_kept_and_shifted(two_displays, monkeypatch):
    button = SimpleNamespace(role="AXButton", label="Play", frame=(-1300.0, 400.0, 40.0, 30.0))
    root = SimpleNamespace(role="AXWindow", label="", frame=WINDOW, kids=[button])
    button.kids = []
    monkeypatch.setattr(
        macos,
        "AS",
        SimpleNamespace(AXUIElementCreateApplication=lambda pid: root, AXUIElementSetMessagingTimeout=lambda *a: 0),
    )
    monkeypatch.setattr(macos, "_ax_children", lambda element: element.kids)
    monkeypatch.setattr(macos, "_ax_attrs", lambda element: macos.AxAttrs(element.role, element.label, element.frame))
    monkeypatch.setattr(macos, "_ax_actions", lambda element: ["AXPress"])

    image = macos.screenshot()
    width_pt = image.width / macos.display_scale(image)
    height_pt = image.height / macos.display_scale(image)
    visible, hidden, _capped = macos.actionable_elements(PID, width_pt, height_pt)
    assert [(n.label, n.x, n.y, n.w, n.h) for n in visible if isinstance(n, AxNode)] == [("Play", 212.0, 200.0, 40.0, 30.0)]
    assert not [n for n in hidden if n.label == "Play"]


def test_the_main_display_reads_as_before(two_displays):
    two_displays.windows[1]["kCGWindowBounds"] = {"X": 200, "Y": 150, "Width": 800, "Height": 600}
    image = macos.screenshot()
    assert two_displays.captured == [MAIN]
    assert macos.display_scale(image) == 2.0
    assert macos.frontmost_window_bounds(PID) == (200.0, 150.0, 800.0, 600.0)
    macos.click_at((10.0, 20.0))
    assert two_displays.events[-1] == ("up", (10.0, 20.0))


def test_a_capture_that_does_not_match_the_display_falls_back_to_the_main_display(two_displays):
    CAPTURE_PX[BUILT_IN] = (2000, 2000)  # not the built-in display's shape: screencapture numbered them differently
    try:
        image = macos.screenshot()
    finally:
        CAPTURE_PX[BUILT_IN] = (3024, 1964)
    assert two_displays.captured == [BUILT_IN, MAIN]
    assert image.size == CAPTURE_PX[MAIN]
    assert macos.display_scale(image) == 2.0
