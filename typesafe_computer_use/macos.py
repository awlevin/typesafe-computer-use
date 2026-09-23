"""macOS adapter: synthetic input, app control, screen capture, and the focused accessibility element.

This is the only module that touches Quartz, ApplicationServices, AppleScript, or Vision OCR.
windows.py provides the same functions for Windows; platform_adapter.py picks one. The bounded tree
walk itself lives in ax_walk.py, shared by both.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path

import ApplicationServices as AS
import Quartz
from ocrmac import ocrmac
from PIL import Image

from .apps import same_app
from .ax_walk import AX_PRESS, AxAttrs, Frame, walk_actionable
from .config import ABORT_CORNER_PX
from .models import Abort, AxNode, Field, MenuItem, WindowRef

# Virtual key codes: the keys the fixed actions press, and the letters and arrows of the named shortcuts.
KEYCODES = {
    "return": 36, "tab": 48, "escape": 53, "delete": 51, "[": 33, "]": 30, "=": 24, "-": 27,
    "a": 0, "c": 8, "f": 3, "n": 45, "r": 15, "s": 1, "t": 17, "v": 9, "w": 13, "x": 7, "z": 6,
    "left": 123, "right": 124, "down": 125, "up": 126,
}  # fmt: skip
MIN_WINDOW_SIDE_PT = 50.0  # anything smaller is a palette or a shadow, not the window being worked in
MAX_DISPLAYS = 8

# ------------------------------------------------------------------ escape hatch


def mouse_location() -> tuple[float, float]:
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


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
    return bool(AS.AXIsProcessTrusted())


# ------------------------------------------------------------------ input


def _post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.04)


def _down_then_up(event: Callable[[bool], object]) -> None:
    """Post the down event, then the up event even when the down is interrupted, so nothing stays held."""
    try:
        _post(event(True))
    finally:
        _post(event(False))


def click_at(point: tuple[float, float], clicks: int = 1, right: bool = False) -> None:
    """Click at a point, once or twice, with either button.

    A double click's second press carries a click count of two: without it the system sees two
    separate clicks, and a list opens nothing where a double click would have opened the file.
    """
    # Check before moving: the synthetic move would otherwise take the pointer out of the abort corner.
    check_abort()
    if right:
        button, kinds = Quartz.kCGMouseButtonRight, {True: Quartz.kCGEventRightMouseDown, False: Quartz.kCGEventRightMouseUp}
    else:
        button, kinds = Quartz.kCGMouseButtonLeft, {True: Quartz.kCGEventLeftMouseDown, False: Quartz.kCGEventLeftMouseUp}
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, button))
    for count in range(1, clicks + 1):
        check_abort()

        def event(down: bool, count: int = count):
            e = Quartz.CGEventCreateMouseEvent(None, kinds[down], point, button)
            if count > 1:
                Quartz.CGEventSetIntegerValueField(e, Quartz.kCGMouseEventClickState, count)
            return e

        _down_then_up(event)


def press(key: str, command: bool = False, shift: bool = False) -> None:
    check_abort()
    code = KEYCODES[key]
    flags = Quartz.kCGEventFlagMaskCommand if command else 0
    if shift:
        flags |= Quartz.kCGEventFlagMaskShift

    def event(down: bool):
        e = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if flags:
            Quartz.CGEventSetFlags(e, flags)
        return e

    _down_then_up(event)


def _unicode_key(ch: str, down: bool):
    event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
    Quartz.CGEventKeyboardSetUnicodeString(event, len(ch), ch)
    return event


def type_text(text: str) -> None:
    """One character at a time, checking the abort corner before each."""
    for ch in text:
        check_abort()
        _down_then_up(partial(_unicode_key, ch))


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int) -> None:
    """Scroll events go to the view under the cursor, so park it over the frontmost window first."""
    center = frontmost_window_center()
    check_abort()
    if center is not None:
        _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, center, Quartz.kCGMouseButtonLeft))
    _post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, lines))


# ------------------------------------------------------------------ apps and windows


def osascript(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True).stdout.strip()


def frontmost_app() -> str:
    return osascript('tell application "System Events" to get name of first application process whose frontmost is true')


def frontmost_app_and_pid() -> tuple[str, int]:
    """Name and pid of the frontmost process in one AppleScript round trip."""
    name, _, pid = osascript(
        'tell application "System Events" to tell (first application process whose frontmost is true) to get {name, unix id}'
    ).rpartition(", ")
    return name, int(pid)


def frontmost_pid() -> int:
    return int(osascript('tell application "System Events" to get unix id of first application process whose frontmost is true'))


def _launch(app: str, *args: str) -> bool:
    """`open -a`: launch or bring forward an app by the name the Finder shows, with any arguments.

    Neither the name nor a URL is ever spliced into a script: both reach `open` as arguments, so a
    quote in a writer-proposed URL is part of the URL and nothing more.
    """
    return subprocess.run(["open", "-a", app, *args], capture_output=True, check=False).returncode == 0


def applescript_string(value: str) -> str:
    """One AppleScript string literal. App names come off the disk, so they are escaped, not trusted."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app to the front, launching it if it is not running, and confirm it got there.

    `open -a` launches apps that carry no AppleScript dictionary and resolves the names the Finder
    shows. The process may go by another name (Visual Studio Code runs as Code), so arrival is
    judged by `same_app`. System Events is the last resort for an app that comes up behind another.
    """
    check_abort()
    if not _launch(app):
        return False
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        check_abort()
        if same_app(frontmost_app(), app):
            return True
        time.sleep(0.1)
    check_abort()
    try:
        osascript(f'tell application "System Events" to set frontmost of process {applescript_string(app)} to true')
    except subprocess.CalledProcessError:
        return False
    time.sleep(0.3)
    return same_app(frontmost_app(), app)


def open_url(browser: str, url: str) -> bool:
    """Open a URL in one browser and bring it forward."""
    check_abort()
    if not _launch(browser, url):
        return False
    return activate(browser)


def browser_url(browser: str) -> str | None:
    try:
        return osascript(f'tell application "{browser}" to get URL of active tab of front window') or None
    except subprocess.CalledProcessError:
        return None


def open_path(path: Path, as_text: bool = False) -> None:
    """Show a file to the user; `as_text` opens it in the default text editor."""
    subprocess.run(["open", *(["-t"] if as_text else []), str(path)], check=False)


def frontmost_window_bounds(pid: int | None = None) -> tuple[float, float, float, float] | None:
    """The frontmost app's topmost on-screen window as x, y, w, h in points. Pure Quartz, no AX needed.

    Pass the pid when the caller already has it; looking it up costs an AppleScript round trip.
    """
    pid = frontmost_pid() if pid is None else pid
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for window in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        if window.get("kCGWindowOwnerPID") == pid and window.get("kCGWindowLayer") == 0:
            b = window["kCGWindowBounds"]
            if b["Width"] > MIN_WINDOW_SIDE_PT and b["Height"] > MIN_WINDOW_SIDE_PT:
                return float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"])
    return None


# Where macOS keeps applications. Anything else an app launches from is a shortcut to one of these,
# so a one-level scan of the five is the whole inventory of an ordinary Mac.
APP_DIRECTORIES = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    "~/Applications",
)
# Worth offering though they live elsewhere. CoreServices holds dozens of internal apps nobody means
# to open, so it is named app by app rather than scanned.
EXTRA_APPS = ("/System/Library/CoreServices/Finder.app",)


def installed_apps() -> list[str]:
    """Every application on this Mac by the name the user sees, sorted without regard to case.

    Read from the disk rather than from a list in this repository, so what is offered is what this
    Mac has.
    """
    return scan_apps(APP_DIRECTORIES, EXTRA_APPS)


def scan_apps(directories: Iterable[str], extras: Iterable[str]) -> list[str]:
    """The `.app` bundles one level down each folder, plus the extras that exist. A folder that does
    not exist is simply not there."""
    names: set[str] = set()
    for directory in directories:
        try:
            names.update(entry.stem for entry in Path(directory).expanduser().iterdir() if entry.suffix == ".app")
        except OSError:
            continue
    names.update(Path(app).stem for app in extras if Path(app).exists())
    return sorted(names, key=str.casefold)


def running_apps() -> set[str]:
    """The apps with a Dock icon open right now. Background agents are not apps a user switches to."""
    try:
        from AppKit import NSApplicationActivationPolicyRegular, NSWorkspace
    except ImportError:
        return set()
    running = NSWorkspace.sharedWorkspace().runningApplications()
    return {str(app.localizedName()) for app in running if app.activationPolicy() == NSApplicationActivationPolicyRegular}


def app_windows(pid: int, limit: int = 12) -> list[WindowRef]:
    """One app's titled windows in the order it lists them, front first. Untitled ones are panels."""
    windows = _ax_attr(AS.AXUIElementCreateApplication(pid), "AXWindows") or []
    found = []
    for window in list(windows)[:limit]:
        title = _ax_attr(window, AS.kAXTitleAttribute)
        if isinstance(title, str) and title.strip():
            found.append(WindowRef(title=title.strip(), main=bool(_ax_attr(window, "AXMain")), ref=window))
    return found


def raise_window(ref) -> bool:
    """Bring one window of the frontmost app in front of that app's other windows."""
    check_abort()
    try:
        raised = AS.AXUIElementPerformAction(ref, "AXRaise") == 0
        AS.AXUIElementSetAttributeValue(ref, "AXMain", True)
    except Exception:
        return False
    return raised


def frontmost_window_center(pid: int | None = None) -> tuple[float, float] | None:
    """Center of the frontmost app's topmost on-screen window, in points."""
    bounds = frontmost_window_bounds(pid)
    if bounds is None:
        return None
    x, y, w, h = bounds
    return x + w / 2, y + h / 2


# ------------------------------------------------------------------ capture and accessibility


def screenshot(display: int = 1) -> Image.Image:
    """One display's pixels. `display` counts from 1, main first, in the order `active_displays` lists."""
    path = Path(tempfile.mkdtemp()) / "screen.png"
    subprocess.run(["screencapture", "-x", "-D", str(display), str(path)], check=True, capture_output=True)
    return Image.open(path).convert("RGB")


def active_displays() -> list[tuple[float, float, float, float]]:
    """Every display as x, y, w, h in global points, main first, in screencapture's `-D` order.

    Global points are one space across all displays: a screen left of the main one starts at a
    negative x. Every click is in that space, so a capture carries its display's origin with it.
    """
    main = Quartz.CGMainDisplayID()
    err, ids, count = Quartz.CGGetActiveDisplayList(MAX_DISPLAYS, None, None)
    ordered = sorted(ids[:count], key=lambda display: display != main) if not err and count else [main]
    frames = []
    for display in ordered:
        bounds = Quartz.CGDisplayBounds(display)
        frames.append((float(bounds.origin.x), float(bounds.origin.y), float(bounds.size.width), float(bounds.size.height)))
    return frames


def display_scale(image: Image.Image, bounds: tuple[float, float, float, float] | None = None) -> float:
    """Capture pixels per point, from the display the capture came from; the main one when not said."""
    if bounds is not None:
        return image.width / bounds[2]
    points_wide = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width
    return image.width / points_wide


def _pasteboard():
    from AppKit import NSPasteboard

    return NSPasteboard.generalPasteboard()


def clipboard_text() -> str:
    """The clipboard's text now; empty when it holds an image, a file, or nothing."""
    try:
        from AppKit import NSPasteboardTypeString
    except ImportError:
        return ""
    return str(_pasteboard().stringForType_(NSPasteboardTypeString) or "")


def recognize_text(image: Image.Image) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """Vision OCR lines as text, confidence, and a box in the image's own pixels."""
    return ocrmac.OCR(image, recognition_level="accurate").recognize(px=True)


def _ax_attr(element, name: str):
    """One attribute, or None. A dead or hostile element raises from the bridge; that is a miss, not a crash."""
    try:
        err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    return value if err == 0 else None


SECURE_SUBROLE = "AXSecureTextField"  # a password field, whatever it is labelled


def focused_field() -> Field | None:
    system = AS.AXUIElementCreateSystemWide()
    element = _ax_attr(system, AS.kAXFocusedUIElementAttribute)
    if element is None:
        return None
    x = y = w = h = 0.0
    pos = _ax_attr(element, AS.kAXPositionAttribute)
    size = _ax_attr(element, AS.kAXSizeAttribute)
    if pos is not None and size is not None:
        _, pt = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
        _, sz = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        x, y, w, h = pt.x, pt.y, sz.width, sz.height
    value = _ax_attr(element, AS.kAXValueAttribute)
    label = _ax_attr(element, AS.kAXTitleAttribute) or _ax_attr(element, AS.kAXDescriptionAttribute) or ""
    secure = _ax_attr(element, AS.kAXSubroleAttribute) == SECURE_SUBROLE
    return Field(
        role=str(_ax_attr(element, AS.kAXRoleAttribute) or ""),
        label=str(label),
        placeholder=str(_ax_attr(element, AS.kAXPlaceholderValueAttribute) or ""),
        value=value if isinstance(value, str) else "",
        x=x,
        y=y,
        w=w,
        h=h,
        ref=element,
        secure=secure,
    )


# ------------------------------------------------------------------ acting on an element

# An element accepts these directly, so a press lands on the control the app declared rather than
# on whatever pixel happens to sit at its center. Every one of them is best effort: the element may
# be dead, the app may refuse, and the bridge raises on both. False means "use synthetic input".


def ax_press(ref) -> bool:
    """Send AXPress to an element."""
    check_abort()
    try:
        return AS.AXUIElementPerformAction(ref, AX_PRESS) == 0
    except Exception:
        return False


def ax_focus(ref) -> bool:
    """Give an element the keyboard focus."""
    check_abort()
    try:
        return AS.AXUIElementSetAttributeValue(ref, AS.kAXFocusedAttribute, True) == 0
    except Exception:
        return False


def ax_set_value(ref, text: str) -> bool:
    """Write an element's value. A read-only or unwilling element reports an error."""
    check_abort()
    try:
        return AS.AXUIElementSetAttributeValue(ref, AS.kAXValueAttribute, text) == 0
    except Exception:
        return False


def ax_value(ref) -> str | None:
    """An element's value, when it has a textual one."""
    value = _ax_attr(ref, AS.kAXValueAttribute)
    return value if isinstance(value, str) else None


# ------------------------------------------------------------------ actionable elements

AX_MESSAGE_TIMEOUT = 0.2
AX_VALUE_CHARS = 120


def _ax_children(element) -> list:
    return list(_ax_attr(element, AS.kAXChildrenAttribute) or [])


def _ax_label(element) -> str:
    """AXTitle on AppKit, AXDescription on web and Electron, a short AXValue as a last resort."""
    for name in (AS.kAXTitleAttribute, AS.kAXDescriptionAttribute):
        text = _ax_attr(element, name)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())
    value = _ax_attr(element, AS.kAXValueAttribute)
    if isinstance(value, str) and 0 < len(value.strip()) <= AX_VALUE_CHARS:
        return " ".join(value.split())
    return ""


def _ax_frame(element) -> Frame | None:
    pos = _ax_attr(element, AS.kAXPositionAttribute)
    size = _ax_attr(element, AS.kAXSizeAttribute)
    if pos is None or size is None:
        return None
    ok_pos, pt = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
    ok_size, sz = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not (ok_pos and ok_size):
        return None
    return float(pt.x), float(pt.y), float(sz.width), float(sz.height)


def _ax_attrs(element) -> AxAttrs:
    return AxAttrs(str(_ax_attr(element, AS.kAXRoleAttribute) or ""), _ax_label(element), _ax_frame(element))


def _ax_actions(element) -> list[str]:
    try:
        err, names = AS.AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    return [str(n) for n in names] if err == 0 and names else []


def actionable_elements(pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
    """Labelled controls of one process: the on-screen ones in points, the pressable off-screen ones,
    and whether a cap cut the walk short."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, AX_MESSAGE_TIMEOUT)
    return walk_actionable(app, _ax_children, _ax_attrs, _ax_actions, display_w_pt, display_h_pt)


def nameless_elements(pid: int, display_w_pt: float, display_h_pt: float) -> list[AxNode]:
    """The visible pressable controls of one process that carry no label at all, in points.

    A walk of its own, the same bounded one, so `actionable_elements` stays as it is for every run
    that has nothing to name these with. Only a run with a writer that reads images calls it.
    """
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, AX_MESSAGE_TIMEOUT)
    out: list[AxNode] = []
    walk_actionable(app, _ax_children, _ax_attrs, _ax_actions, display_w_pt, display_h_pt, nameless=out)
    return out


# ------------------------------------------------------------------ the menu bar

MENU_SEPARATOR = " \u25b8 "  # how a menu path reads: "File \u25b8 Export \u25b8 PDF\u2026"
MENU_DEPTH = 2  # a top-level menu's items, and one level of submenu
MENU_PER_TOP = 40  # History and Bookmarks run to hundreds; the first few dozen are the commands
# Never offered, whatever app publishes them. The Apple menu is skipped whole, which covers Shut
# Down, Restart, Log Out and Sleep; these are the few elsewhere that are as hard to take back.
MENU_DENIED = ("erase all content", "empty trash", "erase assistant")
SKIPPED_MENUS = ("Apple",)
# AXMenuItemCmdModifiers: 0 is Command alone; these bits add to it, and the last takes Command away.
MENU_SHIFT, MENU_OPTION, MENU_CONTROL, MENU_NO_COMMAND = 1, 2, 4, 8


def menu_chord(char: object, modifiers: object) -> tuple[str, bool, bool] | None:
    """A menu item's shortcut as (key, command, shift), when it is one Command and Shift alone make.

    A chord with Option or Control in it is left as None: no named key sends one, so there is
    nothing for it to be told apart from.
    """
    if not isinstance(char, str) or not char.strip():
        return None
    mods = modifiers if isinstance(modifiers, int) else 0
    if mods & (MENU_OPTION | MENU_CONTROL):
        return None
    return char.strip().lower(), not mods & MENU_NO_COMMAND, bool(mods & MENU_SHIFT)


def menu_items(pid: int, limit: int) -> list[MenuItem]:
    """The enabled commands of one app's menu bar, as paths that can be pressed without opening it.

    The menu bar is part of the accessibility tree, so the app's whole command set, commands with no
    button on screen among them, reads in milliseconds. Disabled items are what the app says it
    cannot do now, and are left out.
    """
    bar = _ax_attr(AS.AXUIElementCreateApplication(pid), "AXMenuBar")
    if bar is None:
        return []
    found: list[MenuItem] = []
    for top in _ax_children(bar):
        title = _ax_attr(top, AS.kAXTitleAttribute)
        if not isinstance(title, str) or not title or title in SKIPPED_MENUS:
            continue
        _collect_menu(top, title, found, MENU_PER_TOP, MENU_DEPTH)
        if len(found) >= limit:
            break
    return found[:limit]


def _collect_menu(element, path: str, found: list[MenuItem], budget: int, depth: int) -> None:
    """One menu's enabled commands, appended as paths. A submenu is followed one level down."""
    children = _ax_children(element)
    menu = children[0] if children and _ax_attr(children[0], AS.kAXRoleAttribute) == "AXMenu" else None
    if menu is None:
        return
    taken = 0
    for item in _ax_children(menu):
        if taken >= budget:
            return
        title = _ax_attr(item, AS.kAXTitleAttribute)
        title = title.strip() if isinstance(title, str) else ""
        if not title or any(denied in title.casefold() for denied in MENU_DENIED):
            continue  # a separator has no title
        full = f"{path}{MENU_SEPARATOR}{title}"
        if _ax_children(item) and depth > 1:
            _collect_menu(item, full, found, budget, depth - 1)
            continue
        if _ax_attr(item, "AXEnabled"):
            chord = menu_chord(_ax_attr(item, "AXMenuItemCmdChar"), _ax_attr(item, "AXMenuItemCmdModifiers"))
            found.append(MenuItem(path=full, chord=chord, ref=item))
            taken += 1
