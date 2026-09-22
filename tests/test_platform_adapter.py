"""Both adapters provide the whole Desktop surface, and macOS stays the default."""

import inspect
import sys

import pytest

from typesafe_computer_use import macos, windows
from typesafe_computer_use.platform_adapter import Desktop, desktop

SURFACE = sorted(name for name, value in vars(Desktop).items() if callable(value) and not name.startswith("_"))


def shape(function) -> list[tuple[str, object, object]]:
    """Parameter names, kinds and defaults: what a caller relies on, without the annotations."""
    params = list(inspect.signature(function).parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    return [(p.name, p.kind, p.default) for p in params]


@pytest.mark.parametrize("adapter", [macos, windows], ids=["macos", "windows"])
@pytest.mark.parametrize("name", SURFACE)
def test_each_adapter_provides_the_desktop_surface(adapter, name):
    assert callable(getattr(adapter, name, None)), f"{adapter.__name__} lacks {name}"
    assert shape(getattr(adapter, name)) == shape(getattr(Desktop, name))


def test_the_surface_covers_what_the_callers_use():
    assert len(SURFACE) >= 20 and {"click_at", "recognize_text", "actionable_elements"} <= set(SURFACE)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows picks windows.py")
def test_macos_is_the_adapter_off_windows():
    assert desktop is macos


def test_windows_presses_every_key_macos_presses():
    assert set(macos.KEYCODES) <= set(windows.VK)
