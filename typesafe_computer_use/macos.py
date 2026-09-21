"""macOS adapter: synthetic input, app control, screen capture, and the focused accessibility element.

This is the only module that touches Quartz, ApplicationServices, or AppleScript.
A Linux adapter would provide the same functions over xdotool and AT-SPI.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

import ApplicationServices as AS
import Quartz
from PIL import Image

from .config import ABORT_CORNER_PX
from .models import Abort, AxNode, Field

KEYCODES = {"return": 36, "tab": 48, "escape": 53, "a": 0, "delete": 51}
MIN_WINDOW_SIDE_PT = 50.0  # anything smaller is a palette or a shadow, not the window being worked in

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


def click_at(point: tuple[float, float]) -> None:
    for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        _post(Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft))


def press(key: str, command: bool = False) -> None:
    code = KEYCODES[key]
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if command:
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        _post(event)


def type_text(text: str) -> None:
    for ch in text:
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(ch), ch)
            _post(event)


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int) -> None:
    """Scroll events go to the view under the cursor, so park it over the frontmost window first."""
    center = frontmost_window_center()
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


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app to the front and confirm it got there."""
    osascript(f'tell application "{app}" to activate')
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if frontmost_app() == app:
            return True
        time.sleep(0.1)
    osascript(f'tell application "System Events" to set frontmost of process "{app}" to true')
    time.sleep(0.3)
    return frontmost_app() == app


def open_url(browser: str, url: str) -> bool:
    osascript(f'tell application "{browser}" to open location "{url}"')
    return activate(browser)


def browser_url(browser: str) -> str | None:
    try:
        return osascript(f'tell application "{browser}" to get URL of active tab of front window') or None
    except subprocess.CalledProcessError:
        return None


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


def frontmost_window_center(pid: int | None = None) -> tuple[float, float] | None:
    """Center of the frontmost app's topmost on-screen window, in points."""
    bounds = frontmost_window_bounds(pid)
    if bounds is None:
        return None
    x, y, w, h = bounds
    return x + w / 2, y + h / 2


# ------------------------------------------------------------------ capture and accessibility


def screenshot() -> Image.Image:
    path = Path(tempfile.mkdtemp()) / "screen.png"
    subprocess.run(["screencapture", "-x", "-D", "1", str(path)], check=True, capture_output=True)
    return Image.open(path).convert("RGB")


def display_scale(image: Image.Image) -> float:
    points_wide = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width
    return image.width / points_wide


def _ax_attr(element, name: str):
    """One attribute, or None. A dead or hostile element raises from the bridge; that is a miss, not a crash."""
    try:
        err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    return value if err == 0 else None


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
    )


# ------------------------------------------------------------------ acting on an element

AX_PRESS = "AXPress"

# An element accepts these directly, so a press lands on the control the app declared rather than
# on whatever pixel happens to sit at its center. Every one of them is best effort: the element may
# be dead, the app may refuse, and the bridge raises on both. False means "use synthetic input".


def ax_press(ref) -> bool:
    """Send AXPress to an element."""
    try:
        return AS.AXUIElementPerformAction(ref, AX_PRESS) == 0
    except Exception:
        return False


def ax_focus(ref) -> bool:
    """Give an element the keyboard focus."""
    try:
        return AS.AXUIElementSetAttributeValue(ref, AS.kAXFocusedAttribute, True) == 0
    except Exception:
        return False


def ax_set_value(ref, text: str) -> bool:
    """Write an element's value. A read-only or unwilling element reports an error."""
    try:
        return AS.AXUIElementSetAttributeValue(ref, AS.kAXValueAttribute, text) == 0
    except Exception:
        return False


def ax_value(ref) -> str | None:
    """An element's value, when it has a textual one."""
    value = _ax_attr(ref, AS.kAXValueAttribute)
    return value if isinstance(value, str) else None


# ------------------------------------------------------------------ actionable elements

AX_ACTIONABLE_ROLES = {
    "AXButton",
    "AXCell",
    "AXCheckBox",
    "AXComboBox",
    "AXDisclosureTriangle",
    "AXImage",
    "AXIncrementor",
    "AXLink",
    "AXMenuBarItem",
    "AXMenuButton",
    "AXPopUpButton",
    "AXRadioButton",
    "AXRow",
    "AXSearchField",
    "AXSlider",
    "AXTab",
    "AXTextArea",
    "AXTextField",
}
# A bare child, usually a decorative AXImage, borrows the label of a parent that is itself a control.
AX_LABEL_PARENT_ROLES = {
    "AXButton",
    "AXCell",
    "AXCheckBox",
    "AXLink",
    "AXMenuButton",
    "AXPopUpButton",
    "AXRadioButton",
    "AXRow",
    "AXTab",
}
# List containers keep their label in a shallow AXStaticText rather than on themselves.
AX_LABEL_DESCENDANT_ROLES = {"AXCell", "AXRow"}
AX_SKIP_SUBTREE_ROLES = {"AXMenu"}  # a closed menu: thousands of zero-sized items, none on screen
AX_NODE_CAP = 4000
AX_TIME_CAP = 0.6
AX_OFFSCREEN_CAP = 120  # off-screen controls collected before the walk stops looking for more
AX_MIN_SIDE_PT = 4.0  # anything thinner is a Chromium sliver for a scrolled-out node
AX_MESSAGE_TIMEOUT = 0.2
AX_FANOUT = 8  # children scanned per level when recovering a label
AX_VALUE_CHARS = 120

Frame = tuple[float, float, float, float]  # x, y, w, h in points


class AxAttrs(NamedTuple):
    role: str
    label: str
    frame: Frame | None


def off_display(frame: Frame | None, display_w_pt: float, display_h_pt: float) -> bool:
    """True when a real frame lies wholly outside the display: a note list thousands of screens down,
    or a web node the browser parked above the viewport. A zero-size frame claims nothing, which is
    what an application element and a closed menu report, so their subtrees are still worth a look.
    """
    if frame is None:
        return False
    x, y, w, h = frame
    if w <= 0 or h <= 0:
        return False
    return x >= display_w_pt or y >= display_h_pt or x + w <= 0 or y + h <= 0


def center_on_display(frame: Frame | None, display_w_pt: float, display_h_pt: float) -> bool:
    """Whether a frame's centre falls on the display. Chromium pages report a giant `shell` frame
    that starts tens of thousands of points above the viewport yet crosses it, so `off_display`
    alone lets it through as partially visible — and clicking it would aim far off the screen.
    Such a frame cannot be pressed where it claims to be, so it belongs with the off-screen nodes.
    A frameless or zero-size frame claims nothing (an application element, a closed menu), and its
    centre is meaningless, so those stay as they were.
    """
    if frame is None:
        return True
    x, y, w, h = frame
    if w <= 0 or h <= 0:
        return True
    return 0 <= x + w / 2 < display_w_pt and 0 <= y + h / 2 < display_h_pt


def node_identity(node) -> object:
    """Accessibility elements hash by the element they wrap, so two fetches of one control compare
    equal; anything unhashable (a fake node in a test) falls back to object identity."""
    try:
        hash(node)
    except TypeError:
        return ("id", id(node))
    return node


def subtree_key(role: str, label: str, frame: Frame | None) -> tuple | None:
    """Identity of a node for de-duplication: same role, label and frame is the same control, whatever
    object the bridge wrapped it in. Frameless and zero-size nodes are containers and are never keyed."""
    if frame is None or frame[2] <= 0 or frame[3] <= 0:
        return None
    return (role, label, round(frame[0]), round(frame[1]), round(frame[2]), round(frame[3]))


def clickable(frame: Frame | None) -> bool:
    return frame is not None and min(frame[2], frame[3]) >= AX_MIN_SIDE_PT


def descendant_label(kids: list, children: Callable, attrs: Callable[..., AxAttrs]) -> str:
    """The first static text within two levels, which is where list rows hide their label."""
    for kid in kids[:AX_FANOUT]:
        role, label, _ = attrs(kid)
        if role == "AXStaticText" and label:
            return label
    for kid in kids[:AX_FANOUT]:
        for grandkid in list(children(kid))[:AX_FANOUT]:
            role, label, _ = attrs(grandkid)
            if role == "AXStaticText" and label:
                return label
    return ""


def walk_actionable(
    root,
    children: Callable[..., Iterable],
    attrs: Callable[..., AxAttrs],
    actions: Callable[..., Iterable[str]],
    display_w_pt: float,
    display_h_pt: float,
    node_cap: int = AX_NODE_CAP,
    time_cap: float = AX_TIME_CAP,
    offscreen_cap: int = AX_OFFSCREEN_CAP,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[AxNode], list[AxNode], bool]:
    """Breadth-first hunt for labelled controls: the on-screen ones, the reachable off-screen ones,
    and whether a cap cut the walk short.

    The four callables are the only way into the tree, so the pruning rules are platform-free
    and testable against a plain dict. The caps are the point: an unbounded walk of a note list
    or a long web page costs seconds and finds nothing on screen.

    A node that misses the display, or that the app clamped to a sliver, is not on screen and is
    not offered as one: its subtree stays pruned from `found`. But AXPress does not need a node to
    be visible, so a labelled one that accepts the action is collected separately, down to
    `offscreen_cap`, after which those subtrees are dropped again and the walk is the old one.
    """
    found: list[AxNode] = []
    offscreen: list[AxNode] = []
    deadline = clock() + time_cap
    queue = deque([(root, "", False, False)])
    seen = 0
    visited: set = set()  # elements compare by identity across fetches, so a self-listing app is walked once
    visited_keys: set[tuple] = set()  # and a control handed over as several distinct objects is kept once
    while queue:
        if seen >= node_cap or clock() >= deadline:
            return found, offscreen, True
        node, parent_label, parent_emitted, hidden = queue.popleft()
        identity = node_identity(node)
        if identity in visited:
            continue
        visited.add(identity)
        seen += 1
        role, own_label, frame = attrs(node)
        if role in AX_SKIP_SUBTREE_ROLES:
            continue
        key = subtree_key(role, own_label, frame)
        if key is not None:
            if key in visited_keys:
                continue
            visited_keys.add(key)
        hidden = (
            hidden or off_display(frame, display_w_pt, display_h_pt) or not center_on_display(frame, display_w_pt, display_h_pt)
        )
        if hidden and len(offscreen) >= offscreen_cap:
            continue  # nothing left to collect down there, and it never counted on screen
        kids = list(children(node))
        label, inherited = own_label, False
        if not label and role in AX_LABEL_DESCENDANT_ROLES:
            label = descendant_label(kids, children, attrs)
        if not label and parent_label:
            label, inherited = parent_label, True
        emitted = False
        duplicate = inherited and parent_emitted  # the parent already stands for this label
        nameless_group = role == "AXGroup" and not own_label  # a Chromium layout box, not a control
        visible = not hidden and clickable(frame)
        if label and not duplicate and not nameless_group:
            if visible:
                pressable = AX_PRESS in actions(node)
                if pressable or role in AX_ACTIONABLE_ROLES:
                    x, y, w, h = frame
                    found.append(AxNode(role=role, label=label, x=x, y=y, w=w, h=h, pressable=pressable, ref=node))
                    emitted = True
            elif frame is not None and len(offscreen) < offscreen_cap and AX_PRESS in actions(node):
                x, y, w, h = frame
                offscreen.append(AxNode(role=role, label=label, x=x, y=y, w=w, h=h, pressable=True, ref=node))
        child_label = own_label if role in AX_LABEL_PARENT_ROLES else ""
        queue.extend((kid, child_label, emitted, hidden) for kid in kids)
    return found, offscreen, False


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
