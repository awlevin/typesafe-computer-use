"""Windows adapter: synthetic input, app control, screen capture, and the focused UI Automation element.

This is the only module that touches pywin32, UI Automation, or pyautogui. macos.py provides the
same functions over Quartz, ApplicationServices, and AppleScript; the bounded tree walk itself lives
in ax_walk.py, shared by both. Once the process is per-monitor DPI aware (set below), Windows has no
Retina-style point/pixel split, so a screen point here already is a capture pixel.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time
import webbrowser
from contextlib import suppress
from pathlib import Path

import psutil
import pyautogui
import uiautomation as auto
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab

from .ax_walk import AxAttrs, Frame, walk_actionable
from .config import ABORT_CORNER_PX
from .models import Abort, AxNode, Field

with suppress(AttributeError, OSError):  # pre-8.1 Windows without shcore, or awareness set by the host process
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE

pyautogui.FAILSAFE = False  # check_abort() below is our own corner escape hatch
pyautogui.PAUSE = 0.0

KEYS = {"return": "enter", "tab": "tab", "escape": "esc", "a": "a", "delete": "delete"}
MIN_WINDOW_SIDE_PT = 50.0  # anything smaller is a palette or a shadow, not the window being worked in

# UI Automation's ControlTypeName mapped onto the "AX*" role vocabulary ax_walk.py, models.ROLE_WORDS,
# and models.TEXT_ROLES already speak, so none of that shared logic needs a Windows-specific branch.
CONTROL_TYPE_TO_ROLE = {
    "ButtonControl": "AXButton",
    "CheckBoxControl": "AXCheckBox",
    "ComboBoxControl": "AXComboBox",
    "DataItemControl": "AXCell",
    "EditControl": "AXTextField",
    "GroupControl": "AXGroup",
    "HyperlinkControl": "AXLink",
    "ImageControl": "AXImage",
    "ListItemControl": "AXRow",
    "MenuItemControl": "AXMenuBarItem",
    "RadioButtonControl": "AXRadioButton",
    "SliderControl": "AXSlider",
    "SpinnerControl": "AXIncrementor",
    "TabItemControl": "AXTab",
    "TextControl": "AXStaticText",
}
# Executable names for the browsers the site catalog opens; anything else falls back to the OS default.
BROWSER_EXES = {"google chrome": "chrome", "microsoft edge": "msedge", "firefox": "firefox", "brave": "brave"}

# ------------------------------------------------------------------ escape hatch


def mouse_location() -> tuple[float, float]:
    point = pyautogui.position()
    return float(point.x), float(point.y)


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
    """Windows has no Accessibility permission gate; confirm UI Automation can reach the desktop instead."""
    try:
        return auto.GetRootControl() is not None
    except Exception:
        return False


# ------------------------------------------------------------------ input


def click_at(point: tuple[float, float]) -> None:
    pyautogui.click(x=round(point[0]), y=round(point[1]))


def press(key: str, command: bool = False) -> None:
    name = KEYS[key]
    if command:
        pyautogui.hotkey("ctrl", name)
    else:
        pyautogui.press(name)


def type_text(text: str) -> None:
    pyautogui.typewrite(text, interval=0.01)


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int) -> None:
    """Scroll events go to the view under the cursor, so park it over the frontmost window first."""
    center = frontmost_window_center()
    if center is not None:
        pyautogui.moveTo(round(center[0]), round(center[1]))
    pyautogui.scroll(lines)


# ------------------------------------------------------------------ apps and windows


def frontmost_app() -> str:
    name, _ = frontmost_app_and_pid()
    return name


def frontmost_app_and_pid() -> tuple[str, int]:
    """Name and pid of the foreground window's process."""
    pid = frontmost_pid()
    try:
        return psutil.Process(pid).name(), pid
    except psutil.Error:
        return "", pid


def frontmost_pid() -> int:
    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return pid


def _find_window(app: str) -> int | None:
    """The first visible top-level window whose title or owning process name matches `app`."""
    needle = app.strip().lower()
    match: int | None = None

    def visit(hwnd, _):
        nonlocal match
        if match is not None or not win32gui.IsWindowVisible(hwnd):
            return
        if needle in win32gui.GetWindowText(hwnd).lower():
            match = hwnd
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if needle in psutil.Process(pid).name().lower():
                match = hwnd
        except psutil.Error:
            pass

    win32gui.EnumWindows(visit, None)
    return match


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app to the front and confirm it got there."""
    hwnd = _find_window(app)
    if hwnd is None:
        return False
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.1)
    return win32gui.GetForegroundWindow() == hwnd


def open_app(app: str, timeout: float = 20.0) -> bool:
    """Bring an app to the front, launching it first if it is not already running.

    Windows has no single call that both launches and waits like AppleScript's activate, so this
    checks for an already-running window first, then starts the app by its registered name (the
    same one Start > Run resolves through the App Paths registry key) and waits for it to appear.
    """
    if activate(app, timeout=1.0):
        return True
    try:
        os.startfile(app)
    except OSError:
        return False
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if activate(app, timeout=0.5):
            return True
        time.sleep(0.5)
    return False


def open_url(browser: str, url: str) -> bool:
    exe = BROWSER_EXES.get(browser.strip().lower())
    if exe and shutil.which(exe):
        subprocess.Popen([exe, url])
    else:
        webbrowser.open(url)
    return activate(browser)


def browser_url(browser: str) -> str | None:
    """The address bar's text, read through UI Automation. No AppleScript-style API exists for this
    on Windows, so it depends on the browser exposing an Edit control named for the address bar,
    which Chrome, Edge, and Firefox all do."""
    hwnd = _find_window(browser)
    if hwnd is None:
        return None
    try:
        window = auto.ControlFromHandle(hwnd)
        edit = window.EditControl(searchDepth=20, NameRegex="(?i)address")
        if not edit.Exists(0, 0):
            return None
        value = edit.GetValuePattern()
        return (value.Value or None) if value else None
    except Exception:
        return None


def open_path(path: Path) -> None:
    os.startfile(path)


def frontmost_window_bounds(pid: int | None = None) -> tuple[float, float, float, float] | None:
    """The foreground window's bounds as x, y, w, h in screen pixels.

    Pass the pid when the caller already has it, to confirm the foreground window still belongs
    to that process rather than one that grabbed focus since.
    """
    hwnd = win32gui.GetForegroundWindow()
    if pid is not None:
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid != pid:
            return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = float(right - left), float(bottom - top)
    if w > MIN_WINDOW_SIDE_PT and h > MIN_WINDOW_SIDE_PT:
        return float(left), float(top), w, h
    return None


def frontmost_window_center(pid: int | None = None) -> tuple[float, float] | None:
    """Center of the frontmost app's topmost on-screen window, in screen pixels."""
    bounds = frontmost_window_bounds(pid)
    if bounds is None:
        return None
    x, y, w, h = bounds
    return x + w / 2, y + h / 2


# ------------------------------------------------------------------ capture and accessibility


def screenshot() -> Image.Image:
    return ImageGrab.grab().convert("RGB")


def display_scale(image: Image.Image) -> float:
    """Per-monitor DPI awareness keeps every coordinate in physical pixels, so a screen point
    already is a capture pixel; unlike macOS, there is no separate points-vs-pixels scale."""
    return 1.0


def _ui_value(element) -> str | None:
    try:
        pattern = element.GetValuePattern()
        return pattern.Value if pattern else None
    except Exception:
        return None


def _ui_placeholder(element) -> str:
    try:
        legacy = element.GetLegacyIAccessiblePattern()
        return (legacy.Description or "") if legacy else ""
    except Exception:
        return ""


def focused_field() -> Field | None:
    try:
        element = auto.GetFocusedControl()
    except Exception:
        return None
    if element is None:
        return None
    rect = _ui_frame(element)
    x, y, w, h = rect if rect is not None else (0.0, 0.0, 0.0, 0.0)
    return Field(
        role=CONTROL_TYPE_TO_ROLE.get(getattr(element, "ControlTypeName", ""), ""),
        label=(getattr(element, "Name", "") or "").strip(),
        placeholder=_ui_placeholder(element),
        value=_ui_value(element) or "",
        x=x,
        y=y,
        w=w,
        h=h,
        ref=element,
    )


# ------------------------------------------------------------------ acting on an element

# An element accepts these directly, so a press lands on the control the app declared rather than
# on whatever pixel happens to sit at its center. Every one of them is best effort: the element may
# be dead, the app may refuse, and the bridge raises on both. False means "use synthetic input".


def ax_press(ref) -> bool:
    """Invoke an element: InvokePattern first, a legacy default action otherwise."""
    try:
        pattern = ref.GetInvokePattern()
        if pattern:
            pattern.Invoke()
            return True
    except Exception:
        pass
    try:
        legacy = ref.GetLegacyIAccessiblePattern()
        if legacy:
            legacy.DoDefaultAction()
            return True
    except Exception:
        pass
    return False


def ax_focus(ref) -> bool:
    """Give an element the keyboard focus."""
    try:
        ref.SetFocus()
        return True
    except Exception:
        return False


def ax_set_value(ref, text: str) -> bool:
    """Write an element's value. A read-only or unwilling element reports an error."""
    try:
        pattern = ref.GetValuePattern()
        if pattern:
            pattern.SetValue(text)
            return True
    except Exception:
        pass
    return False


def ax_value(ref) -> str | None:
    """An element's value, when it has a textual one."""
    return _ui_value(ref)


# ------------------------------------------------------------------ actionable elements


def _ui_children(element) -> list:
    try:
        return list(element.GetChildren())
    except Exception:
        return []


def _ui_frame(element) -> Frame | None:
    try:
        rect = element.BoundingRectangle
    except Exception:
        return None
    if rect is None:
        return None
    w, h = float(rect.width()), float(rect.height())
    return float(rect.left), float(rect.top), w, h


def _ui_attrs(element) -> AxAttrs:
    role = CONTROL_TYPE_TO_ROLE.get(getattr(element, "ControlTypeName", ""), "")
    label = (getattr(element, "Name", "") or "").strip()
    return AxAttrs(role, label, _ui_frame(element))


def _ui_actions(element) -> list[str]:
    try:
        if element.GetInvokePattern() or element.GetLegacyIAccessiblePattern():
            return ["AXPress"]
    except Exception:
        pass
    return []


def actionable_elements(pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
    """Labelled controls of the foreground window, in screen pixels, and the pressable off-screen
    ones. Best effort: a window that refuses UI Automation, or belongs to a different process, or
    has none, yields nothing."""
    hwnd = win32gui.GetForegroundWindow()
    _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
    if window_pid != pid:
        return [], [], False
    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception:
        return [], [], False
    return walk_actionable(root, _ui_children, _ui_attrs, _ui_actions, display_w_pt, display_h_pt)
