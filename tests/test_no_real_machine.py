"""The guard in conftest.py: a test that forgets to patch the machine fails instead of driving it."""

from pathlib import Path

import pytest

from typesafe_computer_use import macos, windows


@pytest.mark.parametrize(
    "touch",
    [
        lambda: macos.click_at((200.0, 200.0)),
        lambda: macos.press("return"),
        lambda: macos.type_text("hi"),
        lambda: macos.scroll(-5),
        lambda: macos.open_url("Google Chrome", "https://example.com"),
        lambda: macos.activate("Finder"),
        lambda: macos.screenshot(),
    ],
)
def test_an_unpatched_call_refuses_instead_of_reaching_the_machine(touch):
    # Off macOS the stand-in Quartz refuses first, before the guard is reached.
    with pytest.raises(RuntimeError, match=r"real machine|unavailable on this OS"):
        touch()


def test_the_pointer_reads_as_mid_screen_so_no_test_aborts_by_chance():
    assert macos.mouse_location() == (500.0, 500.0)
    macos.check_abort()


@pytest.mark.parametrize(
    "touch",
    [
        lambda: windows.click_at((200.0, 200.0)),
        lambda: windows.press("return"),
        lambda: windows.press("[", command=True),
        lambda: windows.type_text("hi"),
        lambda: windows.scroll(-5),
        lambda: windows.open_url("Google Chrome", "https://example.com"),
        lambda: windows.activate("notepad.exe"),
        lambda: windows.open_path(Path("state.txt")),
        lambda: windows.screenshot(),
        lambda: windows.ax_press(object()),
        lambda: windows.ax_focus(object()),
        lambda: windows.ax_set_value(object(), "hi"),
    ],
)
def test_an_unpatched_windows_call_refuses_instead_of_reaching_the_machine(touch):
    # Off Windows a stand-in module may refuse first, before the guard is reached.
    with pytest.raises(RuntimeError, match=r"real machine|unavailable on this OS"):
        touch()


def test_the_windows_pointer_reads_as_mid_screen_too():
    assert windows.mouse_location() == (500.0, 500.0)
    windows.check_abort()
