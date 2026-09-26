"""The desktop this process drives: one platform adapter at a time, behind one name.

Every other module reaches the platform through `desktop` and never imports macos.py or windows.py
itself. `host` is the adapter for the OS this process runs on, chosen once at import. Windows gets
windows.py. Every other OS gets macos.py, the default: on macOS it is the real adapter, and on the
Linux test runner tests/conftest.py stands in for the modules it imports, so the suite runs there
unchanged. Only the Windows path imports a Windows-only package. Where macos.py cannot load off
macOS, such as a Linux machine that drives an OSWorld VM, `host` is a `NoDesktop`: the package
still imports, and any call on the host says there is none.

`desktop` is not an adapter itself. It forwards every attribute read, write, and delete to the
adapter in use: `host`, unless a `using(adapter)` block has put another one in its place, such as
a remote computer that this process drives in place of its own. Callers hold `desktop` and never
see the swap.

`Desktop` is the surface every adapter provides, and tests/test_platform_adapter.py holds macos.py
and windows.py to it, parameter for parameter.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from PIL import Image

from .models import AxNode, Box, Field

OcrLine = tuple[str, float, Box]  # text, confidence, box in the image's own pixels


class Desktop(Protocol):
    # the escape hatch
    def check_abort(self) -> None: ...
    def sleep_watching(self, seconds: float) -> None: ...
    def accessibility_trusted(self) -> bool: ...

    # input
    def click_at(self, point: tuple[float, float]) -> None: ...
    def press(self, key: str, command: bool = False) -> None: ...
    def type_text(self, text: str) -> None: ...
    def clear_field(self) -> None: ...
    def scroll(self, lines: int) -> None: ...

    # apps and windows
    def frontmost_app_and_pid(self) -> tuple[str, int]: ...
    def activate(self, app: str, timeout: float = 3.0) -> bool: ...
    def open_url(self, browser: str, url: str) -> bool: ...
    def browser_url(self, browser: str) -> str | None: ...
    def open_path(self, path: Path, as_text: bool = False) -> None: ...
    def frontmost_window_bounds(self, pid: int | None = None) -> tuple[float, float, float, float] | None: ...

    # capture, OCR, and accessibility
    def screenshot(self) -> Image.Image: ...
    def display_scale(self, image: Image.Image) -> float: ...
    def recognize_text(self, image: Image.Image) -> list[OcrLine]: ...
    def focused_field(self) -> Field | None: ...
    def actionable_elements(
        self, pid: int, display_w_pt: float, display_h_pt: float
    ) -> tuple[list[AxNode], list[AxNode], bool]: ...

    # acting on an element
    def ax_press(self, ref) -> bool: ...
    def ax_focus(self, ref) -> bool: ...
    def ax_set_value(self, ref, text: str) -> bool: ...
    def ax_value(self, ref) -> str | None: ...


class NoDesktop:
    """The host adapter of a process that has no desktop of its own: every call raises and says why.

    It lets the package import where no adapter can, so a remote computer can be driven from
    there through `using(adapter)`.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)  # copy, pickle, and friends probe for these
        raise RuntimeError(f"no desktop to call {name} on: {self._reason}; drive a computer through using(adapter)")

    def __repr__(self) -> str:
        return f"<no desktop: {self._reason}>"


def pick_host() -> Desktop:
    """The adapter for the OS this process runs on. Off macOS and Windows, macos.py when it loads (the
    Linux test runner stands in for its packages), and a `NoDesktop` when it does not."""
    if sys.platform == "win32":
        return importlib.import_module(f"{__package__}.windows")
    try:
        return importlib.import_module(f"{__package__}.macos")
    except ImportError as missing:
        if sys.platform == "darwin":
            raise
        return NoDesktop(f"{sys.platform} has no desktop adapter ({missing})")


host: Desktop = pick_host()


class _InUse:
    """Stands in for the adapter on top of its stack: reads, writes, and deletes all go there.

    Forwarding writes is what lets a test do `monkeypatch.setattr(desktop, "click_at", fake)`: the
    patch, and its undo, land on whichever adapter is in use at that moment.
    """

    __slots__ = ("_stack",)

    def __init__(self, adapter: Desktop) -> None:
        object.__setattr__(self, "_stack", [adapter])

    def __getattr__(self, name: str):
        return getattr(self._stack[-1], name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._stack[-1], name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._stack[-1], name)

    def __repr__(self) -> str:
        return f"<desktop in use: {self._stack[-1]!r}>"


_in_use = _InUse(host)
desktop: Desktop = _in_use  # forwards the whole surface, so it is one


def current() -> Desktop:
    """The adapter that `desktop` forwards to right now."""
    return _in_use._stack[-1]


@contextmanager
def using(adapter: Desktop) -> Iterator[Desktop]:
    """Drive `adapter` in place of the current one until the block ends, also when it raises.

    Not thread-safe across adapters: the stack is shared by the whole process, so a block in one
    thread swaps the adapter for every thread. One process drives one computer at a time; while a
    block is open, no other thread may use `desktop`. Blocks nest, and must end in the order they
    began.

    A patch made through `desktop` inside the block lands on `adapter`. Undo it inside the block
    too (for example with `monkeypatch.context()`): an undo after the block would land on the
    adapter in use by then.
    """
    stack = _in_use._stack
    stack.append(adapter)
    try:
        yield adapter
    finally:
        stack.pop()
