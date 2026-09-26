"""Every adapter provides the whole Desktop surface, macOS stays the default, and a block can swap in another."""

import inspect
import sys
from types import SimpleNamespace

import pytest

from typesafe_computer_use import macos, windows
from typesafe_computer_use.osworld.desktop import OSWorldDesktop
from typesafe_computer_use.platform_adapter import Desktop, NoDesktop, current, desktop, host, pick_host, using

SURFACE = sorted(name for name, value in vars(Desktop).items() if callable(value) and not name.startswith("_"))


def shape(function) -> list[tuple[str, object, object]]:
    """Parameter names, kinds and defaults: what a caller relies on, without the annotations."""
    params = list(inspect.signature(function).parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    return [(p.name, p.kind, p.default) for p in params]


# Read at collection, before the conftest guard swaps the machine-touching functions for refusals.
PROVIDED = {
    (adapter.__name__, name): shape(getattr(adapter, name)) if callable(getattr(adapter, name, None)) else None
    for adapter in (macos, windows, OSWorldDesktop)
    for name in SURFACE
}


@pytest.mark.parametrize("adapter", [macos, windows, OSWorldDesktop], ids=["macos", "windows", "osworld"])
@pytest.mark.parametrize("name", SURFACE)
def test_each_adapter_provides_the_desktop_surface(adapter, name):
    provided = PROVIDED[(adapter.__name__, name)]
    assert provided is not None, f"{adapter.__name__} lacks {name}"
    assert provided == shape(getattr(Desktop, name))


def test_the_surface_covers_what_the_callers_use():
    assert len(SURFACE) >= 20 and {"click_at", "recognize_text", "actionable_elements"} <= set(SURFACE)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows picks windows.py")
def test_macos_is_the_adapter_off_windows():
    assert host is macos and current() is macos


def test_off_macos_a_host_without_the_mac_packages_has_no_desktop(monkeypatch):
    monkeypatch.setitem(sys.modules, "typesafe_computer_use.macos", None)  # as on Linux, where Quartz is absent
    monkeypatch.setattr(sys, "platform", "linux")
    picked = pick_host()
    assert isinstance(picked, NoDesktop)
    with pytest.raises(RuntimeError, match="no desktop to call click_at on: linux has no desktop adapter"):
        picked.click_at((1.0, 2.0))


def test_on_macos_a_missing_mac_package_is_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "typesafe_computer_use.macos", None)
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ImportError):
        pick_host()


def fake_desktop() -> SimpleNamespace:
    return SimpleNamespace(frontmost_app_and_pid=lambda: ("Fake", 7), click_at=lambda point: None)


def test_using_swaps_the_adapter_for_the_block_and_restores_the_host():
    fake = fake_desktop()
    with using(fake) as swapped:
        assert swapped is fake and current() is fake
        assert desktop.frontmost_app_and_pid() == ("Fake", 7)
    assert current() is host


def test_using_restores_the_host_after_an_exception():
    with pytest.raises(RuntimeError, match="inside"), using(fake_desktop()):
        raise RuntimeError("inside")
    assert current() is host


def test_blocks_nest_and_unwind_in_order():
    outer, inner = fake_desktop(), fake_desktop()
    with using(outer):
        with using(inner):
            assert current() is inner
        assert current() is outer
    assert current() is host


def test_a_patch_through_desktop_inside_the_block_lands_on_the_fake(monkeypatch):
    fake = fake_desktop()
    fake_click_at, host_click_at = fake.click_at, host.click_at
    clicked = []
    with using(fake), monkeypatch.context() as patch:
        patch.setattr(desktop, "click_at", clicked.append)
        desktop.click_at((1.0, 2.0))
        assert fake.click_at == clicked.append
        assert host.click_at is host_click_at
    assert clicked == [(1.0, 2.0)]
    assert fake.click_at is fake_click_at, "the undo went to the fake, inside the block"
    assert host.click_at is host_click_at


def test_a_patch_through_desktop_outside_any_block_lands_on_the_host(monkeypatch):
    host_screenshot = host.screenshot
    with monkeypatch.context() as patch:
        patch.setattr(desktop, "screenshot", lambda: "patched")
        assert host.screenshot() == "patched"
    assert host.screenshot is host_screenshot


def test_deletes_go_to_the_adapter_in_use():
    fake = fake_desktop()
    with using(fake):
        del desktop.frontmost_app_and_pid
        with pytest.raises(AttributeError):
            desktop.frontmost_app_and_pid  # noqa: B018
    assert not hasattr(fake, "frontmost_app_and_pid")


def test_windows_presses_every_key_macos_presses():
    assert set(macos.KEYCODES) <= set(windows.VK)
