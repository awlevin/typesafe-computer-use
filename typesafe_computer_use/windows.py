"""Windows adapter: synthetic input, app control, screen capture, OCR, and the focused UI Automation element.

Experimental. This is the only module that touches pywin32, UI Automation, Win32 SendInput, or
Windows.Media.Ocr; macos.py provides the same functions for macOS, and platform_adapter.py picks one.
The bounded tree walk itself lives in ax_walk.py, shared by both.

Once the process is per-monitor DPI aware (set below), Windows has no Retina-style point and pixel
split, so a screen point here already is a capture pixel. The pure rules (the role names, what counts
as pressable, which window belongs to an app, the input events) are plain functions, tested off Windows.
"""

from __future__ import annotations

import ctypes
import functools
import os
import shutil
import subprocess
import time
import webbrowser
from contextlib import suppress
from pathlib import Path

import psutil
import uiautomation as auto
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
import winocr
from PIL import Image, ImageGrab

from .ax_walk import AX_PRESS, AxAttrs, Frame, walk_actionable
from .config import ABORT_CORNER_PX
from .models import Abort, AxNode, Field, MenuItem, Missed, WindowRef

with suppress(AttributeError, OSError):  # pre-8.1 Windows without shcore, or awareness set by the host process
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE

MIN_WINDOW_SIDE_PT = 50.0  # anything smaller is a palette or a shadow, not the window being worked in
EVENT_GAP = 0.04  # seconds between synthetic events, as on macOS
CLICK_TOLERANCE_PX = 1  # how far the cursor may land from its target before a click is refused
WHEEL_DELTA = 120  # one notch of the wheel
LINES_PER_NOTCH = 3  # the Windows default; the actions ask for lines, as on macOS

# Windows.Media.Ocr reads one language per engine and reports no confidence, so every line it returns
# counts as certain and passes MIN_OCR_CONFIDENCE. The language pack must be installed (see README).
OCR_LANGUAGE = "en"
OCR_CONFIDENCE = 1.0

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

# The browsers the site catalog can open, by the name CLICKER_BROWSER gives and their executable.
# The frontmost app is reported by the same name, so the classifier sees one browser, not two.
BROWSER_EXES = {"Google Chrome": "chrome", "Microsoft Edge": "msedge", "Firefox": "firefox", "Brave Browser": "brave"}

# Virtual keys for the keys the actions press. macOS's Command becomes Control, except where Windows
# spells the gesture another way: Command-[ and Command-], a browser's Back and Forward, are Alt-Left
# and Alt-Right, and Command-Up and Command-Down, the top and bottom of a document, are Control-Home
# and Control-End.
VK = {
    "return": 0x0D, "tab": 0x09, "escape": 0x1B, "delete": 0x2E, "[": 0xDB, "]": 0xDD, "=": 0xBB, "-": 0xBD,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23,
    **{letter: ord(letter.upper()) for letter in "acfnrstvwxz"},
}  # fmt: skip
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_ALT = 0x12
EXTENDED_KEYS = {0x2E, 0x25, 0x26, 0x27, 0x28, 0x24, 0x23}  # Delete, the arrows, Home and End live on the extended keypad
COMMAND_CHORDS = {
    "[": (VK_ALT, VK["left"]),
    "]": (VK_ALT, VK["right"]),
    "up": (VK_CONTROL, VK["home"]),
    "down": (VK_CONTROL, VK["end"]),
}

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MONITORINFOF_PRIMARY = 0x1


# ------------------------------------------------------------------ pure rules


def role_for(control_type_name: str) -> str:
    """The AX role a UI Automation control type stands for, or "" for one the walk ignores."""
    return CONTROL_TYPE_TO_ROLE.get(control_type_name, "")


def pressable(has_invoke: bool, default_action: str) -> bool:
    """Whether an element accepts a press. Almost every element carries the LegacyIAccessible
    pattern, so that pattern alone says nothing; only a named default action makes it a control."""
    return has_invoke or bool(default_action.strip())


def exe_stem(name: str) -> str:
    """`Chrome.exe`, `chrome` and ` CHROME ` are one process name."""
    stem = name.strip().lower()
    return stem.removesuffix(".exe")


def app_matches(app: str, process_name: str) -> bool:
    """Whether a process is the app a caller named: the executable itself, or a known browser by
    its product name. Exact, never a substring, and never by window title, so a Chrome tab named
    after an app is not that app."""
    wanted = exe_stem(app)
    browsers = {name.lower(): exe for name, exe in BROWSER_EXES.items()}
    return exe_stem(process_name) in {wanted, browsers.get(wanted, wanted)}


def display_name(process_name: str) -> str:
    """The name to report for a process: a known browser by its product name, anything else as is."""
    stem = exe_stem(process_name)
    return next((name for name, exe in BROWSER_EXES.items() if exe == stem), process_name)


def key_events(key: str, command: bool = False, shift: bool = False) -> list[tuple[int, int]]:
    """(virtual key, flags) for pressing a key, with its modifiers held around it."""
    keys = COMMAND_CHORDS.get(key, (VK_CONTROL, VK[key])) if command else (VK[key],)
    if shift:
        keys = (VK_SHIFT, *keys)
    down = [(vk, KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0) for vk in keys]
    up = [(vk, flags | KEYEVENTF_KEYUP) for vk, flags in reversed(down)]
    return down + up


def unicode_events(text: str) -> list[tuple[int, int]]:
    """(UTF-16 code unit, flags) for typing text as characters, not keys, so any script and any
    emoji arrive whatever the keyboard layout. A character outside the BMP is two units."""
    data = text.encode("utf-16-le")
    units = [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]
    return [(unit, flags) for unit in units for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]


def wheel_delta(lines: int) -> int:
    """The wheel delta that scrolls `lines` lines; positive scrolls up, as on macOS."""
    return round(lines * WHEEL_DELTA / LINES_PER_NOTCH)


def button_events(clicks: int = 1, right: bool = False) -> list[int]:
    """The mouse flags for a click: down then up, once per click. Windows reads two quick clicks
    as a double click by itself, so a double click needs no count of its own."""
    down, up = (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if right else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
    return [down, up] * clicks


def landed(target: tuple[int, int], actual: tuple[float, float]) -> bool:
    return abs(actual[0] - target[0]) <= CLICK_TOLERANCE_PX and abs(actual[1] - target[1]) <= CLICK_TOLERANCE_PX


def ocr_lines(result: dict) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """winocr's plain-dict result as text, confidence, and a box around the line's words."""
    out = []
    for line in result.get("lines", []):
        rects = [word["bounding_rect"] for word in line.get("words", [])]
        if not rects:
            continue
        x1 = min(r["x"] for r in rects)
        y1 = min(r["y"] for r in rects)
        x2 = max(r["x"] + r["width"] for r in rects)
        y2 = max(r["y"] + r["height"] for r in rects)
        out.append((line["text"], OCR_CONFIDENCE, (float(x1), float(y1), float(x2), float(y2))))
    return out


# ------------------------------------------------------------------ SendInput


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _InputUnion)]


@functools.cache
def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    return user32


def _send(event: _Input) -> None:
    """One synthetic event, then the same pause macOS leaves. A blocked event (a UAC prompt or the
    lock screen has the input desktop) raises rather than going missing."""
    if _user32().SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
        raise ctypes.WinError(ctypes.get_last_error())
    time.sleep(EVENT_GAP)


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    return _Input(type=INPUT_KEYBOARD, u=_InputUnion(ki=_KeyboardInput(wVk=vk, wScan=scan, dwFlags=flags)))


def _mouse(flags: int, data: int = 0) -> _Input:
    return _Input(type=INPUT_MOUSE, u=_InputUnion(mi=_MouseInput(mouseData=data & 0xFFFFFFFF, dwFlags=flags)))


# ------------------------------------------------------------------ escape hatch


def mouse_location() -> tuple[float, float]:
    x, y = win32api.GetCursorPos()
    return float(x), float(y)


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


def _move(point: tuple[int, int]) -> None:
    win32api.SetCursorPos(point)
    time.sleep(EVENT_GAP)


def click_at(point: tuple[float, float], clicks: int = 1, right: bool = False) -> None:
    """Move, read back where the cursor landed, then press and release there, once or twice.

    A cursor that did not reach the target (another desktop has the input, or the point is off
    every monitor) means the click would land somewhere unknown, so nothing is pressed.
    """
    target = (round(point[0]), round(point[1]))
    _move(target)
    actual = mouse_location()
    if not landed(target, actual):
        raise Missed(f"the cursor went to {actual}, not {target}")
    for flags in button_events(clicks, right):
        _send(_mouse(flags))


def press(key: str, command: bool = False, shift: bool = False) -> None:
    for vk, flags in key_events(key, command, shift):
        _send(_key(vk=vk, flags=flags))


def type_text(text: str) -> None:
    for unit, flags in unicode_events(text):
        _send(_key(scan=unit, flags=flags))


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int) -> None:
    """Scroll events go to the view under the cursor, so park it over the frontmost window first."""
    center = frontmost_window_center()
    if center is not None:
        _move((round(center[0]), round(center[1])))
    _send(_mouse(MOUSEEVENTF_WHEEL, wheel_delta(lines)))


# ------------------------------------------------------------------ apps and windows


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return ""


def _window_pid(hwnd: int) -> int:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return pid


def frontmost_app() -> str:
    name, _ = frontmost_app_and_pid()
    return name


def frontmost_app_and_pid() -> tuple[str, int]:
    """Name and pid of the foreground window's process."""
    pid = frontmost_pid()
    return display_name(_process_name(pid)), pid


def frontmost_pid() -> int:
    return _window_pid(win32gui.GetForegroundWindow())


def _find_window(app: str) -> int | None:
    """The topmost visible, titled top-level window whose process is `app` (see app_matches)."""
    found: list[int] = []

    def visit(hwnd, _):  # returns None, so the enumeration runs to the end without raising
        titled = not found and win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd)
        if titled and app_matches(app, _process_name(_window_pid(hwnd))):
            found.append(hwnd)

    win32gui.EnumWindows(visit, None)  # top of the z-order first
    return found[0] if found else None


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app to the front and confirm it got there."""
    hwnd = _find_window(app)
    if hwnd is None:
        return False
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    with suppress(Exception):  # Windows may refuse focus theft; the check below reports it
        win32gui.SetForegroundWindow(hwnd)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.1)
    return win32gui.GetForegroundWindow() == hwnd


def _titled_windows(pid: int | None = None) -> list[int]:
    """Visible, titled top-level windows, top of the z-order first; one process's when `pid` is given."""
    found: list[int] = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) and (pid is None or _window_pid(hwnd) == pid):
            found.append(hwnd)

    win32gui.EnumWindows(visit, None)
    return found


def installed_apps() -> list[str]:
    """The apps that have a window open, by the name `activate` finds them by.

    `activate` brings an app forward through a window it already has; nothing here launches one by
    name, so an app with no window is not one open_app could reach, and it is not offered. A Start
    menu scan would list apps the loop cannot open.
    """
    return sorted({display_name(_process_name(_window_pid(hwnd))) for hwnd in _titled_windows()} - {""}, key=str.casefold)


def running_apps() -> set[str]:
    """The same apps: on Windows only an app with a window can be switched to."""
    return set(installed_apps())


def app_windows(pid: int, limit: int = 12) -> list[WindowRef]:
    """One process's titled windows, front first. The foreground one is the main window."""
    front = win32gui.GetForegroundWindow()
    return [
        WindowRef(title=win32gui.GetWindowText(hwnd).strip(), main=hwnd == front, ref=hwnd)
        for hwnd in _titled_windows(pid)[:limit]
    ]


def raise_window(ref) -> bool:
    """Bring one window to the front. Windows may refuse to hand the focus over; that reads as False."""
    check_abort()
    try:
        win32gui.ShowWindow(ref, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(ref)
    except Exception:
        return False
    return win32gui.GetForegroundWindow() == ref


def menu_items(pid: int, limit: int) -> list[MenuItem]:
    """None, on Windows. UI Automation lists a menu's items only once the menu is expanded, which
    opens it on screen and takes the focus; reading the menu bar here would mean driving it. So
    press_menu is never offered on Windows, which is what an empty list means to the classifier."""
    return []


def clipboard_text() -> str:
    """The clipboard's text now; empty when it holds something else, or another app has it open."""
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return ""
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return ""
        return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or "")
    except Exception:
        return ""
    finally:
        win32clipboard.CloseClipboard()


def open_url(browser: str, url: str) -> bool:
    exe = next((exe for name, exe in BROWSER_EXES.items() if name.lower() == browser.strip().lower()), None)
    if exe and shutil.which(exe):
        subprocess.Popen([exe, url])
    else:
        webbrowser.open(url)
    return activate(browser)


def browser_url(browser: str) -> str | None:
    """The address bar's text, read through UI Automation. No AppleScript-style API exists for this
    on Windows, so it depends on the browser exposing an Edit control named for the address bar,
    which Chrome, Edge, and Firefox do. Chrome and Edge show it without the scheme."""
    hwnd = _find_window(browser)
    if hwnd is None:
        return None
    try:
        window = auto.ControlFromHandle(hwnd)
        edit = window.EditControl(searchDepth=20, RegexName="(?i)address")
        if not edit.Exists(0, 0):
            return None
        return _ui_value(edit) or None
    except Exception:
        return None


def open_path(path: Path, as_text: bool = False) -> None:
    """Show a file to the user in its default app; a .txt already opens in the text editor."""
    os.startfile(path)


def frontmost_window_bounds(pid: int | None = None) -> tuple[float, float, float, float] | None:
    """The foreground window's bounds as x, y, w, h in screen pixels.

    Pass the pid when the caller already has it, to confirm the foreground window still belongs
    to that process rather than one that grabbed focus since.
    """
    hwnd = win32gui.GetForegroundWindow()
    if pid is not None and _window_pid(hwnd) != pid:
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


# ------------------------------------------------------------------ capture, OCR, and accessibility


def active_displays() -> list[tuple[float, float, float, float]]:
    """Every monitor as x, y, w, h in virtual-screen pixels, the primary one first."""
    monitors = []
    for handle, _, rect in win32api.EnumDisplayMonitors(None, None):
        info = win32api.GetMonitorInfo(handle)
        left, top, right, bottom = info.get("Monitor", rect)
        monitors.append(
            (
                bool(info.get("Flags", 0) & MONITORINFOF_PRIMARY),
                (float(left), float(top), float(right - left), float(bottom - top)),
            )
        )
    monitors.sort(key=lambda monitor: not monitor[0])
    return [frame for _, frame in monitors] or [
        (0.0, 0.0, float(win32api.GetSystemMetrics(0)), float(win32api.GetSystemMetrics(1)))
    ]


def screenshot(display: int = 1) -> Image.Image:
    """One monitor, counted from 1 in the order `active_displays` lists them, primary first."""
    frames = active_displays()
    x, y, w, h = frames[display - 1] if 0 < display <= len(frames) else frames[0]
    return ImageGrab.grab(bbox=(int(x), int(y), int(x + w), int(y + h)), all_screens=True).convert("RGB")


def display_scale(image: Image.Image, bounds: tuple[float, float, float, float] | None = None) -> float:
    """Per-monitor DPI awareness keeps every coordinate in physical pixels, so a screen point
    already is a capture pixel; unlike macOS, there is no separate points-vs-pixels scale."""
    return 1.0


def recognize_text(image: Image.Image) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """Windows.Media.Ocr lines as text, confidence, and a box in the image's own pixels."""
    return ocr_lines(winocr.recognize_pil_sync(image, OCR_LANGUAGE))


def _pattern(element, pattern_id):
    """A UI Automation pattern, or None when the element does not support it or has died."""
    try:
        return element.GetPattern(pattern_id)
    except Exception:
        return None


def _ui_value(element) -> str | None:
    pattern = _pattern(element, auto.PatternId.ValuePattern)
    try:
        return pattern.Value if pattern is not None else None
    except Exception:
        return None


def _ui_placeholder(element) -> str:
    legacy = _pattern(element, auto.PatternId.LegacyIAccessiblePattern)
    try:
        return (legacy.Description or "") if legacy is not None else ""
    except Exception:
        return ""


def _ui_default_action(element) -> str:
    legacy = _pattern(element, auto.PatternId.LegacyIAccessiblePattern)
    try:
        return (legacy.DefaultAction or "") if legacy is not None else ""
    except Exception:
        return ""


def focused_field() -> Field | None:
    try:
        element = auto.GetFocusedControl()
    except Exception:
        return None
    if element is None:
        return None
    frame = _ui_frame(element)
    x, y, w, h = frame if frame is not None else (0.0, 0.0, 0.0, 0.0)
    return Field(
        role=role_for(getattr(element, "ControlTypeName", "")),
        label=(getattr(element, "Name", "") or "").strip(),
        placeholder=_ui_placeholder(element),
        value=_ui_value(element) or "",
        x=x,
        y=y,
        w=w,
        h=h,
        ref=element,
        secure=_ui_is_password(element),
    )


def _ui_is_password(element) -> bool:
    try:
        return bool(getattr(element, "IsPassword", False))
    except Exception:
        return False


# ------------------------------------------------------------------ acting on an element

# An element accepts these directly, so a press lands on the control the app declared rather than
# on whatever pixel happens to sit at its center. Every one of them is best effort: the element may
# be dead, the app may refuse, and the bridge raises on both. False means "use synthetic input".


def ax_press(ref) -> bool:
    """Invoke an element, or run its named legacy default action: the same rule as `pressable`."""
    invoke = _pattern(ref, auto.PatternId.InvokePattern)
    try:
        if invoke is not None:
            invoke.Invoke()
            return True
        if _ui_default_action(ref).strip():
            _pattern(ref, auto.PatternId.LegacyIAccessiblePattern).DoDefaultAction()
            return True
    except Exception:
        pass
    return False


def ax_focus(ref) -> bool:
    """Give an element the keyboard focus."""
    try:
        return bool(ref.SetFocus())
    except Exception:
        return False


def ax_set_value(ref, text: str) -> bool:
    """Write an element's value. A read-only or unwilling element reports an error."""
    pattern = _pattern(ref, auto.PatternId.ValuePattern)
    try:
        return pattern is not None and bool(pattern.SetValue(text))
    except Exception:
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
    return float(rect.left), float(rect.top), float(rect.width()), float(rect.height())


def _ui_attrs(element) -> AxAttrs:
    label = (getattr(element, "Name", "") or "").strip()
    return AxAttrs(role_for(getattr(element, "ControlTypeName", "")), label, _ui_frame(element))


def _ui_actions(element) -> list[str]:
    has_invoke = _pattern(element, auto.PatternId.InvokePattern) is not None
    return [AX_PRESS] if pressable(has_invoke, "" if has_invoke else _ui_default_action(element)) else []


def actionable_elements(pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
    """Labelled controls of the foreground window, in screen pixels, and the pressable off-screen
    ones. Best effort: a window that refuses UI Automation, or belongs to a different process, or
    has none, yields nothing."""
    hwnd = win32gui.GetForegroundWindow()
    if _window_pid(hwnd) != pid:
        return [], [], False
    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception:
        return [], [], False
    return walk_actionable(root, _ui_children, _ui_attrs, _ui_actions, display_w_pt, display_h_pt)


def nameless_elements(pid: int, display_w_pt: float, display_h_pt: float) -> list[AxNode]:
    """The foreground window's visible pressable controls with no name at all, in screen pixels:
    the same walk as `actionable_elements`, keeping only what it would otherwise drop unnamed."""
    hwnd = win32gui.GetForegroundWindow()
    if _window_pid(hwnd) != pid:
        return []
    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception:
        return []
    out: list[AxNode] = []
    walk_actionable(root, _ui_children, _ui_attrs, _ui_actions, display_w_pt, display_h_pt, nameless=out)
    return out
