"""Shared fixtures, plus import-only stand-ins for the macOS-only modules.

The suite is pure logic and should run on any OS. The platform adapter
(`typesafe_computer_use.macos`, `typesafe_computer_use.perception`) imports
Quartz, ApplicationServices and ocrmac at module scope, but the tests only ever
import it -- they never call it -- so a stand-in that exists and raises on any
real use is enough to run the whole suite off macOS. On macOS the real modules
are installed and nothing below is registered.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _absent(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


def _stub(name: str) -> types.ModuleType:
    """A module that can be imported and nothing else: every unset attribute raises."""
    module = types.ModuleType(name)

    def _getattr(attr: str) -> object:
        raise RuntimeError(f"{name}.{attr} is unavailable off macOS; tests must not call the platform adapter")

    module.__getattr__ = _getattr
    sys.modules[name] = module
    return module


if _absent("Quartz"):
    _stub("Quartz").kCGHIDEventTap = 0
if _absent("ApplicationServices"):
    _stub("ApplicationServices")
if _absent("ocrmac"):

    class _OCR:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("ocrmac is unavailable off macOS")

    _package = _stub("ocrmac")
    _package.__path__ = []
    _package.ocrmac = _stub("ocrmac.ocrmac")
    _package.ocrmac.OCR = _OCR

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from typesafe_computer_use.models import Item, Screen  # noqa: E402


@pytest.fixture
def screen() -> Screen:
    return Screen(image=Image.new("RGB", (2000, 1200)), scale=2.0, app="Google Chrome", field=None, url=None)


def item(index: int, text: str, x1=100, y1=100, x2=400, y2=130, conf=1.0) -> Item:
    return Item(index, text, conf, x1, y1, x2, y2)


@pytest.fixture
def make_item():
    return item


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLICKER_TEST_KEY", raising=False)
    return tmp_path
