"""What the desktop reads beyond the screen: the apps, the menu bar, the displays, the focused field.

Pure rules only. The adapters' calls into the machine are covered by the guard in conftest.py.
"""

from dataclasses import replace

import pytest
from PIL import Image

from typesafe_computer_use import macos
from typesafe_computer_use.apps import resolve_app, same_app
from typesafe_computer_use.models import Field, Item, Screen

# ------------------------------------------------------------------ apps


def test_the_applications_are_read_from_disk_rather_than_a_list_in_the_repository(tmp_path):
    (tmp_path / "Applications").mkdir()
    (tmp_path / "Applications" / "Notes.app").mkdir()
    (tmp_path / "Applications" / "zed.app").mkdir()
    (tmp_path / "Applications" / "notes.txt").touch()
    (tmp_path / "Terminal.app").mkdir()

    found = macos.scan_apps([str(tmp_path / "Applications"), str(tmp_path / "missing")], [str(tmp_path / "Terminal.app")])

    assert found == ["Notes", "Terminal", "zed"]  # sorted without regard to case, and no plain files


def test_an_extra_app_that_is_not_there_is_not_offered(tmp_path):
    assert macos.scan_apps([], [str(tmp_path / "Finder.app")]) == []


def test_an_app_whose_process_goes_by_another_name_still_counts_as_arrived():
    assert same_app("Code", "Visual Studio Code")
    assert same_app("Notes", "notes")
    assert not same_app("Notes", "Numbers")
    assert not same_app("", "Notes")


def test_a_near_miss_of_an_app_name_resolves_to_the_one_installed():
    assert resolve_app("chrome", ["Google Chrome", "Safari"]) == "Google Chrome"


def test_the_real_name_always_wins_over_a_near_miss():
    assert resolve_app("Safari", ["Safari Technology Preview", "Safari"]) == "Safari"


def test_a_name_nothing_matches_is_left_as_it_was():
    assert resolve_app("Arc", ["Google Chrome", "Safari"]) == "Arc"


def test_an_app_name_reaches_applescript_as_a_string_and_not_as_code():
    assert macos.applescript_string('Evil" to do shell script "rm') == '"Evil\\" to do shell script \\"rm"'


def test_an_app_is_launched_and_a_url_opened_as_arguments_never_as_script(monkeypatch):
    launched = []
    monkeypatch.setattr(macos, "_launch", lambda app, *args: launched.append((app, *args)) or True)
    monkeypatch.setattr(macos, "osascript", lambda script: pytest.fail("no script should run"))
    monkeypatch.setattr(macos, "frontmost_app", lambda: "Google Chrome")

    assert macos.open_url("Google Chrome", 'https://example.com/"quoted')

    assert launched == [("Google Chrome", 'https://example.com/"quoted'), ("Google Chrome",)]


def test_an_app_that_will_not_launch_is_a_miss_not_a_crash(monkeypatch):
    monkeypatch.setattr(macos, "_launch", lambda app, *args: False)
    assert macos.activate("Nothing Such") is False
    assert macos.open_url("Nothing Such", "https://example.com/") is False


# ------------------------------------------------------------------ the menu bar


def test_a_command_chord_is_its_key_with_command_held():
    assert macos.menu_chord("S", 0) == ("s", True, False)
    assert macos.menu_chord("Z", macos.MENU_SHIFT) == ("z", True, True)


def test_a_chord_no_named_key_sends_is_not_recorded():
    assert macos.menu_chord("F", macos.MENU_CONTROL) is None
    assert macos.menu_chord("", 0) is None
    assert macos.menu_chord(None, 0) is None


def test_a_menu_item_without_command_is_recorded_as_such():
    assert macos.menu_chord("\t", macos.MENU_NO_COMMAND) is None  # whitespace names no key
    assert macos.menu_chord("A", macos.MENU_NO_COMMAND) == ("a", False, False)


# ------------------------------------------------------------------ the focused field


def secure_field(**overrides) -> Field:
    base = Field(role="AXTextField", label="Password", placeholder="", value="hunter2", x=0, y=0, w=10, h=10, secure=True)
    return replace(base, **overrides)


def test_a_secure_field_never_shows_its_value_in_the_state():
    summary = secure_field().summary()
    assert summary["current_value"] == "" and summary["secure"] is True


def test_an_ordinary_field_reads_as_it_always_did():
    summary = secure_field(secure=False, value="hello").summary()
    assert summary["current_value"] == "hello" and "secure" not in summary


# ------------------------------------------------------------------ displays


def test_a_capture_of_the_main_display_needs_no_translating(screen):
    assert screen.to_pixels(100.0, 50.0) == (200.0, 100.0)
    assert screen.to_points(Item(0, "x", 1.0, 200, 100, 200, 100)) == (100.0, 50.0)


def test_a_capture_of_a_second_display_carries_its_origin():
    left = Screen(image=Image.new("RGB", (2560, 1600)), scale=2.0, app="Notes", field=None, url=None, origin=(-1280.0, 0.0))
    item = Item(0, "Save", 1.0, 100, 100, 140, 120)

    assert left.to_points(item) == (-1280.0 + 60.0, 55.0)  # a click lands on the left screen, not the main one
    assert left.to_pixels(*left.to_points(item)) == item.center


@pytest.mark.parametrize("bounds", [(0.0, 0.0, 1512.0, 982.0), (-1280.0, 0.0, 1280.0, 800.0)])
def test_the_scale_comes_from_the_display_that_was_captured(bounds):
    image = Image.new("RGB", (int(bounds[2] * 2), int(bounds[3] * 2)))
    assert macos.display_scale(image, bounds) == 2.0


# ------------------------------------------------------------------ input shapes, with Quartz replaced


@pytest.fixture
def quartz_events(monkeypatch):
    from types import SimpleNamespace

    events = []
    quartz = SimpleNamespace(
        kCGEventMouseMoved="move",
        kCGEventLeftMouseDown="down",
        kCGEventLeftMouseUp="up",
        kCGEventRightMouseDown="right-down",
        kCGEventRightMouseUp="right-up",
        kCGMouseButtonLeft=0,
        kCGMouseButtonRight=1,
        kCGMouseEventClickState="clicks",
        CGEventCreateMouseEvent=lambda _, kind, point, button: {"kind": kind, "button": button},
        CGEventSetIntegerValueField=lambda event, field, value: event.update({field: value}),
        CGEventCreateKeyboardEvent=lambda _, code, down: {"code": code, "down": down},
        kCGEventFlagMaskCommand=1,
        kCGEventFlagMaskShift=2,
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
    )
    monkeypatch.setattr(macos, "Quartz", quartz)
    monkeypatch.setattr(macos, "_post", events.append)
    return events


def test_a_double_click_carries_a_click_count_on_its_second_press(quartz_events):
    macos.click_at((10.0, 20.0), clicks=2)
    assert [(e["kind"], e.get("clicks")) for e in quartz_events] == [
        ("move", None),
        ("down", None),
        ("up", None),
        ("down", 2),
        ("up", 2),
    ]


def test_a_right_click_moves_and_presses_the_right_button(quartz_events):
    macos.click_at((10.0, 20.0), right=True)
    assert [(e["kind"], e["button"]) for e in quartz_events] == [("move", 1), ("right-down", 1), ("right-up", 1)]


def test_a_shifted_chord_holds_command_and_shift(quartz_events):
    macos.press("z", command=True, shift=True)
    assert quartz_events == [{"code": 6, "down": True, "flags": 3}, {"code": 6, "down": False, "flags": 3}]
