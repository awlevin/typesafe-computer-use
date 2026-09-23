"""The Windows adapter's pure rules. Nothing here touches Windows, so it runs everywhere."""

import ctypes

import pytest

from typesafe_computer_use import windows
from typesafe_computer_use.models import ROLE_WORDS, TEXT_ROLES, Missed
from typesafe_computer_use.windows import (
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    app_matches,
    display_name,
    key_events,
    landed,
    ocr_lines,
    pressable,
    role_for,
    unicode_events,
    wheel_delta,
)

# ------------------------------------------------------------------ roles


def test_control_types_map_onto_the_roles_the_shared_code_knows():
    assert role_for("ButtonControl") == "AXButton"
    assert role_for("HyperlinkControl") == "AXLink"
    assert role_for("EditControl") in TEXT_ROLES  # a focused edit box is a text field
    assert role_for("ComboBoxControl") in TEXT_ROLES
    assert role_for("TextControl") == "AXStaticText"  # where a list row's label is recovered from


def test_an_unknown_control_type_has_no_role():
    assert role_for("PaneControl") == ""
    assert role_for("") == ""


def test_every_mapped_control_role_has_a_word_or_is_structural():
    structural = {"AXGroup", "AXStaticText", "AXIncrementor"}
    assert {role for role in windows.CONTROL_TYPE_TO_ROLE.values()} - set(ROLE_WORDS) <= structural


# ------------------------------------------------------------------ what is pressable


def test_an_invoke_pattern_is_pressable():
    assert pressable(True, "")


def test_a_legacy_default_action_is_pressable():
    assert pressable(False, "Press")
    assert pressable(False, "Jump")


def test_the_legacy_pattern_alone_is_not_pressable():
    """Almost every element carries LegacyIAccessible; without a named action it is not a control."""
    assert not pressable(False, "")
    assert not pressable(False, "   ")


# ------------------------------------------------------------------ which window is the app


def test_an_app_matches_its_own_process_exactly():
    assert app_matches("notepad.exe", "Notepad.exe")
    assert app_matches("notepad", "notepad.exe")
    assert app_matches("OUTLOOK", "outlook.exe")


def test_a_browser_matches_by_its_product_name():
    assert app_matches("Google Chrome", "chrome.exe")
    assert app_matches("microsoft edge", "msedge.exe")


def test_no_substring_match():
    assert not app_matches("Outlook", "outlookhelper.exe")
    assert not app_matches("note", "notepad.exe")
    assert not app_matches("Google Chrome", "chromedriver.exe")


def test_a_chrome_tab_named_after_an_app_is_not_that_app():
    """The window title never counts: only the process that owns the window."""
    assert not app_matches("Outlook", "chrome.exe")


def test_a_browser_is_reported_by_its_product_name():
    assert display_name("chrome.exe") == "Google Chrome"
    assert display_name("msedge.exe") == "Microsoft Edge"
    assert display_name("notepad.exe") == "notepad.exe"
    assert app_matches(display_name("chrome.exe"), "chrome.exe")  # what is reported can be activated


# ------------------------------------------------------------------ input events


def test_a_plain_key_is_down_then_up():
    assert key_events("return") == [(0x0D, 0), (0x0D, KEYEVENTF_KEYUP)]


def test_command_becomes_control_held_around_the_key():
    assert key_events("a", command=True) == [(0x11, 0), (0x41, 0), (0x41, KEYEVENTF_KEYUP), (0x11, KEYEVENTF_KEYUP)]


def test_command_bracket_is_alt_left_the_windows_back():
    ext = KEYEVENTF_EXTENDEDKEY
    assert key_events("[", command=True) == [(0x12, 0), (0x25, ext), (0x25, ext | KEYEVENTF_KEYUP), (0x12, KEYEVENTF_KEYUP)]


def test_delete_is_an_extended_key():
    assert key_events("delete") == [(0x2E, KEYEVENTF_EXTENDEDKEY), (0x2E, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)]


def test_text_is_typed_as_unicode_characters_not_keys():
    down, up = KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
    assert unicode_events("aé") == [(ord("a"), down), (ord("a"), up), (0xE9, down), (0xE9, up)]


def test_text_outside_ascii_survives():
    """pyautogui.typewrite dropped every one of these."""
    text = "Zoë, 東京, Ω"
    units = [unit for unit, flags in unicode_events(text) if not flags & KEYEVENTF_KEYUP]
    assert "".join(map(chr, units)) == text


def test_a_character_outside_the_bmp_is_a_surrogate_pair():
    units = [unit for unit, flags in unicode_events("😀") if not flags & KEYEVENTF_KEYUP]
    assert units == [0xD83D, 0xDE00]


def test_the_scroll_the_actions_ask_for_is_whole_notches_in_the_macos_direction():
    assert wheel_delta(10) > 0 and wheel_delta(-10) < 0  # positive scrolls up on both
    assert wheel_delta(-10) == -400  # ten lines at three a notch
    assert wheel_delta(3) == windows.WHEEL_DELTA


def test_the_input_record_has_the_size_windows_expects():
    """SendInput rejects a record of the wrong size, so the union must be as wide as MOUSEINPUT."""
    pointer = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(windows._Input) == (40 if pointer == 8 else 28)


# ------------------------------------------------------------------ a click that reads back


def test_a_click_lands_only_where_the_cursor_went():
    assert landed((100, 200), (100.0, 200.0))
    assert landed((100, 200), (101.0, 199.0))
    assert not landed((100, 200), (0.0, 0.0))  # another desktop has the input
    assert not landed((100, 200), (100.0, 260.0))


def test_a_click_that_did_not_land_presses_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(windows, "_move", lambda point: None)
    monkeypatch.setattr(windows, "mouse_location", lambda: (0.0, 0.0))
    monkeypatch.setattr(windows, "_send", sent.append)
    with pytest.raises(Missed, match="not \\(640, 480\\)"):
        windows.click_at((640.0, 480.0))
    assert sent == []


def test_a_click_that_landed_presses_and_releases(monkeypatch):
    sent = []
    monkeypatch.setattr(windows, "_move", lambda point: None)
    monkeypatch.setattr(windows, "mouse_location", lambda: (640.0, 480.0))
    monkeypatch.setattr(windows, "_send", sent.append)
    windows.click_at((640.4, 479.6))
    assert [event.u.mi.dwFlags for event in sent] == [windows.MOUSEEVENTF_LEFTDOWN, windows.MOUSEEVENTF_LEFTUP]


# ------------------------------------------------------------------ OCR


def test_ocr_lines_box_the_words_and_report_full_confidence():
    result = {
        "text": "Sign in",
        "lines": [
            {
                "text": "Sign in",
                "words": [
                    {"text": "Sign", "bounding_rect": {"x": 10, "y": 20, "width": 30, "height": 12}},
                    {"text": "in", "bounding_rect": {"x": 45, "y": 18, "width": 12, "height": 16}},
                ],
            },
            {"text": "", "words": []},
        ],
    }
    assert ocr_lines(result) == [("Sign in", windows.OCR_CONFIDENCE, (10.0, 18.0, 57.0, 34.0))]
    assert windows.OCR_CONFIDENCE == 1.0  # Windows.Media.Ocr reports none


def test_shift_is_held_outside_the_other_modifiers():
    assert key_events("z", command=True, shift=True) == [
        (0x10, 0),
        (0x11, 0),
        (ord("Z"), 0),
        (ord("Z"), 2),
        (0x11, 2),
        (0x10, 2),
    ]


@pytest.mark.parametrize(("key", "chord"), [("]", (0x12, 0x27)), ("up", (0x11, 0x24)), ("down", (0x11, 0x23))])
def test_the_macos_chords_with_a_windows_spelling_of_their_own(key, chord):
    down = [vk for vk, flags in key_events(key, command=True) if not flags & 2]
    assert tuple(down) == chord


def test_a_double_click_is_two_clicks_and_a_right_click_uses_the_other_button():
    assert windows.button_events() == [windows.MOUSEEVENTF_LEFTDOWN, windows.MOUSEEVENTF_LEFTUP]
    assert windows.button_events(clicks=2) == [windows.MOUSEEVENTF_LEFTDOWN, windows.MOUSEEVENTF_LEFTUP] * 2
    assert windows.button_events(right=True) == [windows.MOUSEEVENTF_RIGHTDOWN, windows.MOUSEEVENTF_RIGHTUP]


def test_a_right_double_click_that_landed_sends_the_right_button_twice(monkeypatch):
    sent = []
    monkeypatch.setattr(windows, "_move", lambda point: None)
    monkeypatch.setattr(windows, "mouse_location", lambda: (200.0, 300.0))
    monkeypatch.setattr(windows, "_send", lambda event: sent.append(event.u.mi.dwFlags))

    windows.click_at((200.0, 300.0), clicks=2, right=True)

    assert sent == [windows.MOUSEEVENTF_RIGHTDOWN, windows.MOUSEEVENTF_RIGHTUP] * 2


def test_the_menu_bar_is_not_read_on_windows_so_press_menu_is_never_offered():
    assert windows.menu_items(1234, 50) == []
