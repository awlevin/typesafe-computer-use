"""The guard in conftest.py: a test that forgets to patch the machine fails instead of driving it."""

import pytest

from typesafe_computer_use import macos


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
    with pytest.raises(RuntimeError, match="real machine|unavailable off macOS"):
        touch()


def test_the_pointer_reads_as_mid_screen_so_no_test_aborts_by_chance():
    assert macos.mouse_location() == (500.0, 500.0)
    macos.check_abort()
