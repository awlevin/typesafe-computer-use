"""Windows desktop adapter for the TypeSafe computer-use loop.

The decision loop works with screenshots, labelled controls, and typed actions.
This module supplies those facts through Win32 and Microsoft UI Automation via
pywinauto. Optional imports stay lazy so replay and pure logic remain usable
without Windows automation packages installed.
"""

from __future__ import annotations

import ctypes
import time
import webbrowser
from contextlib import suppress
from pathlib import Path
from typing import Any

from PIL import Image, ImageGrab

from .config import ABORT_CORNER_PX
from .models import Abort, AxNode, Field

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", _Rect),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouse_data", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("extra_info", ctypes.c_void_p),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mouse", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", ctypes.c_uint32), ("data", _InputUnion)]


def _set_dpi_awareness() -> None:
    with suppress(AttributeError, OSError):
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2


_set_dpi_awareness()


def _pywinauto():
    try:
        from pywinauto import Desktop, keyboard, mouse
    except ImportError as exc:  # pragma: no cover - depends on host setup
        raise RuntimeError("Windows automation support is missing; install pywinauto and rapidocr-onnxruntime") from exc
    return Desktop, keyboard, mouse


def _foreground_hwnd() -> int:
    return int(_user32.GetForegroundWindow())


def _pid_for(hwnd: int) -> int:
    pid = ctypes.c_uint32()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(0x1400, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).stem
    finally:
        _kernel32.CloseHandle(handle)


def _window_title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def mouse_location() -> tuple[float, float]:
    point = _Point()
    return (float(point.x), float(point.y)) if _user32.GetCursorPos(ctypes.byref(point)) else (0.0, 0.0)


def check_abort() -> None:
    x, y = mouse_location()
    if x <= ABORT_CORNER_PX and y <= ABORT_CORNER_PX:
        raise Abort("mouse in top-left corner")


def sleep_watching(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_abort()
        time.sleep(0.1)


def accessibility_trusted() -> bool:
    return True  # Windows UI Automation has no macOS-style trust prompt.


def _absolute_mouse_position(point: tuple[float, float]) -> tuple[int, int]:
    virtual_left = _user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    virtual_top = _user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    virtual_width = _user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    virtual_height = _user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if virtual_width <= 1 or virtual_height <= 1:
        raise RuntimeError("the Windows virtual desktop has invalid dimensions")
    x = max(0, min(65535, round((point[0] - virtual_left) * 65535 / (virtual_width - 1))))
    y = max(0, min(65535, round((point[1] - virtual_top) * 65535 / (virtual_height - 1))))
    return x, y


def _send_mouse(flags: int, x: int = 0, y: int = 0) -> None:
    event = _Input(0, _InputUnion(_MouseInput(x, y, 0, flags, 0, None)))
    sent = _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def move_cursor(point: tuple[float, float], *, settle: float = 0.06) -> None:
    """Move the cursor through the active input desktop and verify its landing point."""
    x, y = _absolute_mouse_position(point)
    _send_mouse(0x0001 | 0x8000 | 0x4000, x, y)  # MOVE | ABSOLUTE | VIRTUALDESK
    time.sleep(settle)
    actual = mouse_location()
    if max(abs(actual[0] - point[0]), abs(actual[1] - point[1])) > 8:
        # Some Windows desktop/DPI combinations quantize absolute SendInput coordinates
        # against a slightly different desktop rectangle. SetCursorPos is a useful nudge
        # when available; the read-back below still prevents a click at an unknown point.
        with suppress(OSError):
            if _user32.SetCursorPos(round(point[0]), round(point[1])):
                time.sleep(0.03)
                actual = mouse_location()
    if max(abs(actual[0] - point[0]), abs(actual[1] - point[1])) > 8:
        raise RuntimeError(f"cursor did not reach target {point!r}; actual position is {actual!r}")


def click_at(point: tuple[float, float]) -> None:
    """Click a screen point through Win32 SendInput, including canvas apps.

    pywinauto's SetCursorPos can fail when the caller is attached to a different
    input desktop. SendInput targets the active desktop directly and also works
    for Unity/canvas windows that expose no UI Automation controls. The events
    are deliberately separated: some canvas engines do not reliably process a
    move, button-down, and button-up delivered in one zero-duration batch.
    """
    move_cursor(point)
    time.sleep(0.05)
    _send_mouse(0x0002)  # LEFTDOWN; keep the verified cursor position
    time.sleep(0.07)
    _send_mouse(0x0004)  # LEFTUP


def press(key: str, command: bool = False) -> None:
    _Desktop, keyboard, _mouse = _pywinauto()
    names = {"return": "{ENTER}", "tab": "{TAB}", "escape": "{ESC}", "delete": "{DELETE}"}
    token = names.get(key, key)
    keyboard.send_keys(("^" if command else "") + token)


def type_text(text: str) -> None:
    _Desktop, keyboard, _mouse = _pywinauto()
    keyboard.send_keys(text, with_spaces=True)


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int) -> None:
    _Desktop, _keyboard, mouse = _pywinauto()
    center = frontmost_window_center()
    if center is not None:
        mouse.move(coords=(round(center[0]), round(center[1])))
    mouse.scroll(lines)


def frontmost_app_and_pid() -> tuple[str, int]:
    hwnd = _foreground_hwnd()
    pid = _pid_for(hwnd)
    return _process_name(pid) or _window_title(hwnd) or "unknown", pid


def frontmost_app() -> str:
    return frontmost_app_and_pid()[0]


def frontmost_pid() -> int:
    return frontmost_app_and_pid()[1]


def activate(app: str, timeout: float = 3.0) -> bool:
    wanted = app.lower().removesuffix(".exe")
    needles = (wanted,)
    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def visit(hwnd, _lparam):
        hwnd = int(hwnd)
        process_name = _process_name(_pid_for(hwnd)).lower()
        title = _window_title(hwnd).lower()
        if _user32.IsWindowVisible(hwnd) and any(needle in process_name or needle in title for needle in needles):
            matches.append(hwnd)
        return True

    _user32.EnumWindows(visit, 0)
    if not matches:
        return False
    hwnd = matches[0]
    _user32.ShowWindow(hwnd, 5)
    _user32.SetForegroundWindow(hwnd)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _foreground_hwnd() == hwnd:
            return True
        time.sleep(0.05)
    return _foreground_hwnd() == hwnd


def open_url(browser: str, url: str) -> bool:
    webbrowser.open(url)
    time.sleep(0.3)
    return activate(browser)


def browser_url(browser: str) -> str | None:
    # Browser address-bar extraction is browser-specific. Do not send Ctrl+L/C
    # during a run just to scrape it; screen/UIA state remains authoritative.
    return None


def frontmost_window_bounds(pid: int | None = None) -> tuple[float, float, float, float] | None:
    hwnd = _foreground_hwnd()
    if pid is not None and _pid_for(hwnd) != pid:
        return None
    rect = _Rect()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    return (float(rect.left), float(rect.top), float(width), float(height)) if width > 0 and height > 0 else None


def frontmost_window_center(pid: int | None = None) -> tuple[float, float] | None:
    bounds = frontmost_window_bounds(pid)
    if bounds is None:
        return None
    x, y, w, h = bounds
    return x + w / 2, y + h / 2


def screenshot() -> Image.Image:
    return ImageGrab.grab(all_screens=True).convert("RGB")


def display_scale(image: Image.Image) -> float:
    return 1.0  # process is made per-monitor DPI aware before the first capture


def _desktop_window(hwnd: int):
    Desktop, _keyboard, _mouse = _pywinauto()
    return Desktop(backend="uia").window(handle=hwnd)


def _control_type(wrapper) -> str:
    return str(getattr(wrapper.element_info, "control_type", "") or "")


def _label(wrapper) -> str:
    info = wrapper.element_info
    for value in (getattr(info, "name", ""), wrapper.window_text()):
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _rect(wrapper) -> tuple[float, float, float, float] | None:
    try:
        r = wrapper.rectangle()
    except Exception:
        return None
    return float(r.left), float(r.top), float(r.width()), float(r.height())


def _focused_hwnd() -> int:
    info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
    return int(info.hwndFocus or 0) if _user32.GetGUIThreadInfo(0, ctypes.byref(info)) else 0


def _field_from(wrapper) -> Field | None:
    role = _control_type(wrapper)
    if role not in {"Edit", "Document", "ComboBox", "List", "Spinner"}:
        return None
    box = _rect(wrapper)
    if box is None:
        return None
    try:
        value = wrapper.get_value() if hasattr(wrapper, "get_value") else wrapper.window_text()
    except Exception:
        value = wrapper.window_text()
    return Field(role, _label(wrapper), "", str(value or ""), *box, ref=wrapper)


def focused_field() -> Field | None:
    hwnd = _focused_hwnd()
    if not hwnd:
        return None
    try:
        return _field_from(_desktop_window(hwnd))
    except Exception:
        return None


def ax_press(ref: Any) -> bool:
    try:
        if hasattr(ref, "invoke"):
            ref.invoke()
        else:
            ref.click_input()
        return True
    except Exception:
        try:
            ref.click_input()
            return True
        except Exception:
            return False


def ax_focus(ref: Any) -> bool:
    try:
        ref.set_focus()
        return True
    except Exception:
        return False


def ax_set_value(ref: Any, text: str) -> bool:
    try:
        ref.set_edit_text(text)
        return True
    except Exception:
        try:
            ref.set_value(text)
            return True
        except Exception:
            return False


def ax_value(ref: Any) -> str | None:
    try:
        value = ref.get_value() if hasattr(ref, "get_value") else ref.window_text()
    except Exception:
        return None
    return str(value) if value is not None else None


_ACTIONABLE = {
    "Button",
    "CheckBox",
    "ComboBox",
    "Hyperlink",
    "ListItem",
    "MenuItem",
    "RadioButton",
    "ScrollBar",
    "Slider",
    "TabItem",
    "TreeItem",
    "Edit",
    "Document",
}


def actionable_elements(pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
    hwnd = _foreground_hwnd()
    if _pid_for(hwnd) != pid:
        return [], [], False
    try:
        root = _desktop_window(hwnd)
        descendants = [root, *root.descendants()]
    except Exception:
        return [], [], False

    visible: list[AxNode] = []
    offscreen: list[AxNode] = []
    started = time.monotonic()
    capped = False
    for index, wrapper in enumerate(descendants):
        if index >= 4000 or time.monotonic() - started >= 0.6:
            capped = True
            break
        role, label, box = _control_type(wrapper), _label(wrapper), _rect(wrapper)
        if not label or box is None or role not in _ACTIONABLE:
            continue
        x, y, w, h = box
        node = AxNode(role, label, x, y, w, h, True, wrapper)
        on_screen = w >= 4 and h >= 4 and x < display_w_pt and y < display_h_pt and x + w > 0 and y + h > 0
        (visible if on_screen else offscreen).append(node)
        if len(offscreen) >= 120:
            offscreen = offscreen[:120]
    return visible, offscreen, capped
