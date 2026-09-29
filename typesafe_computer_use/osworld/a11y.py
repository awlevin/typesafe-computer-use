"""The Ubuntu accessibility tree OSWorld returns, read in the vocabulary `ax_walk` speaks.

OSWorld's VM serializes AT-SPI as XML: the tag is the role name with spaces turned into hyphens
(`push-button`, `page-tab`), `name` is the label, each state is `st:<state>="true"`, the text is the
element's own text, and a visible, showing element carries `cp:screencoord` and `cp:size` as tuple
strings like `(12, 34)`, in screen pixels.

The tree is often missing: `obs["accessibility_tree"]` is None when the VM's fetch timed out, and
Chrome's tree can be shallow. So every helper takes None and returns its "nothing known" value,
never raising. Nothing here reaches a platform adapter, so it imports on any OS.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import replace

from ..ax_walk import AX_PRESS, AxAttrs, Frame, walk_actionable
from ..models import AxNode, Field, Popup

# The namespaces of OSWorld's Ubuntu tree: `_accessibility_ns_map["ubuntu"]` in `src/accessibility.py`
# of xlang-ai/osworld-server at a3cc3f0c64e463f020d1a44780307e9b46cbcab1, which is the
# `desktop_env/server` submodule of xlang-ai/OSWorld-V2 at 3d778a3c9a34a079316f70df023b166700445792.
NS_STATE = "https://accessibility.ubuntu.example.org/ns/state"
NS_ATTRIBUTES = "https://accessibility.ubuntu.example.org/ns/attributes"
NS_COMPONENT = "https://accessibility.ubuntu.example.org/ns/component"
NS_ACTION = "https://accessibility.ubuntu.example.org/ns/action"

# The AT-SPI actions that mean "this is what a click does", the ones Chromium on a Mac turns into
# AXPress. Each action is serialized as `act:<name>_desc`. Left out: `doDefault`, which nearly every
# node carries, `clickAncestor`, the text inside a link, and `showContextMenu`.
PRESS_ACTIONS = ("activate", "check", "click", "jump", "open", "press", "select", "uncheck")
# Roles a click always works on, whatever actions they list: Chrome's own menus (its app menu,
# a context menu) offer only `doDefault` on their items.
CLICK_ROLES = {"menu-item", "check-menu-item", "radio-menu-item"}

# AT-SPI roles onto the AX names `ax_walk` and `models` know. `password-text` gets a role of its own
# that is never a text role, so jev never types into a password field.
ROLES = {
    "push-button": "AXButton",
    "link": "AXLink",
    "entry": "AXTextField",
    "password-text": "AXSecureTextField",
    "check-box": "AXCheckBox",
    "combo-box": "AXComboBox",
    "page-tab": "AXTab",
    "list-item": "AXRow",
    "menu-item": "AXMenuItem",
    "check-menu-item": "AXMenuItem",
    "radio-menu-item": "AXMenuItem",
    # What Chrome's pages and settings are built from, as macOS names them: a toggle button (a
    # pressed/unpressed button, a switch) is a checkbox there, as it is for Chrome on a Mac.
    "toggle-button": "AXCheckBox",
    "radio-button": "AXRadioButton",
    "slider": "AXSlider",
    "spin-button": "AXIncrementor",
    "table-cell": "AXCell",
    "tree-item": "AXRow",
    # A list row keeps its label in a text child, which the walk recovers only from AXStaticText.
    "static": "AXStaticText",
    "label": "AXStaticText",
}
WINDOW_ROLES = {"frame", "window", "dialog", "alert", "file-chooser"}
# A popup's own close button, by its whole label: "Close this profile" is no way to close a popup.
CLOSE_LABELS = {"close", "dismiss"}
BROWSER_APPS = ("Google Chrome", "Chromium")
ADDRESS_BAR = "Address and search bar"  # Chromium's accessible name for the omnibox
LABEL_TEXT_CHARS = 120  # a short text stands in for a missing name, as a short AXValue does on macOS

_PAIR = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)")


def parse(xml: str | bytes | None) -> ET.Element | None:
    """The tree's root, or None when there is no tree or it is not XML (a fetch cut short)."""
    if not xml:
        return None
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        return None


def ax_role(tag: str) -> str:
    """The AX name for an AT-SPI role. A role with no AX counterpart keeps its own name."""
    return ROLES.get(tag, tag)


def has_state(element: ET.Element | None, state: str) -> bool:
    return element is not None and element.get(f"{{{NS_STATE}}}{state}") == "true"


def name(element: ET.Element | None) -> str:
    """The element's name with its whitespace collapsed, or "" for no element."""
    if element is None:
        return ""
    return " ".join((element.get("name") or "").split())


def text(element: ET.Element | None) -> str:
    """The element's own text. Whitespace alone is layout between tags, not content."""
    if element is None or element.text is None or not element.text.strip():
        return ""
    return element.text


def label(element: ET.Element | None) -> str:
    """The name, or a short text when the name is empty. A password's text is never a label."""
    own = name(element)
    if own or element is None or element.tag == "password-text":
        return own
    content = " ".join(text(element).split())
    return content if len(content) <= LABEL_TEXT_CHARS else ""


def placeholder(element: ET.Element | None) -> str:
    """The hint an empty field shows. Chromium calls the attribute `placeholder`, GTK
    `placeholder-text`."""
    if element is None:
        return ""
    return element.get(f"{{{NS_ATTRIBUTES}}}placeholder") or element.get(f"{{{NS_ATTRIBUTES}}}placeholder-text") or ""


def _pair(value: str | None) -> tuple[float, float] | None:
    match = _PAIR.fullmatch(value.strip()) if value else None
    return (float(match[1]), float(match[2])) if match else None


def frame(element: ET.Element | None) -> Frame | None:
    """x, y, w, h in screen pixels. OSWorld writes coordinates only for a visible, showing element."""
    if element is None:
        return None
    origin = _pair(element.get(f"{{{NS_COMPONENT}}}screencoord"))
    size = _pair(element.get(f"{{{NS_COMPONENT}}}size"))
    if origin is None or size is None:
        return None
    return origin[0], origin[1], size[0], size[1]


def _windows(app: ET.Element) -> list[ET.Element]:
    return [child for child in app if child.tag in WINDOW_ROLES]


def active_window(root: ET.Element | None) -> ET.Element | None:
    """The top-level window AT-SPI marks active: the one with the keyboard."""
    if root is None:
        return None
    for app in root:
        for window in _windows(app):
            if has_state(window, "active"):
                return window
    return None


def active_app(root: ET.Element | None) -> ET.Element | None:
    """The application that owns the active window, or, when no window claims it, the one holding
    the focus.

    GNOME Shell's full-screen window is focused in every tree, whichever app has the keyboard, so a
    focused control counts before a focused top-level window, wherever its app falls in the tree.
    """
    if root is None:
        return None
    for app in root:
        if any(has_state(window, "active") for window in _windows(app)):
            return app
    for app in root:
        windows = _windows(app)
        if any(has_state(element, "focused") and element not in windows for element in app.iter()):
            return app
    for app in root:
        if any(has_state(window, "focused") for window in _windows(app)):
            return app
    return None


def window_title(root: ET.Element | None) -> str | None:
    """The title of the window in front, the name of the frame the active application draws.

    Chrome draws a menu, a bubble, or a dialog as a window of its own, which may be the active one,
    but none is a window with a title: the frame behind it is.
    """
    window = active_window(root)
    if window is not None and window.tag == "frame" and name(window):
        return name(window)
    app = active_app(root)
    return next((name(w) for w in _windows(app) if w.tag == "frame" and name(w)), None) if app is not None else None


def focused_field(root: ET.Element | None) -> Field | None:
    """The focused element of the active window, or, when no window is active, of the application
    holding the focus. The whole tree is no scope: GNOME Shell's window is focused in every tree.

    A focused container can hold the focused control, and a descendant follows its ancestor in
    document order, so the last focused element is the innermost. A password's value is never read.
    A multi-line entry, a `textarea`, is a text area, as it is on a Mac.
    """
    scope = active_window(root)
    if scope is None:
        scope = active_app(root)
    if scope is None:
        return None
    focused = [element for element in scope.iter() if has_state(element, "focused")]
    if not focused:
        return None
    element = focused[-1]
    x, y, w, h = frame(element) or (0.0, 0.0, 0.0, 0.0)
    return Field(
        role="AXTextArea" if element.tag == "entry" and has_state(element, "multi_line") else ax_role(element.tag),
        label=name(element),
        placeholder=placeholder(element),
        value="" if element.tag == "password-text" else text(element),
        x=x,
        y=y,
        w=w,
        h=h,
    )


def browser_url(root: ET.Element | None, browser: str | None = None) -> str | None:
    """The text of Chrome's address bar, from the active browser window first.

    It is what the omnibox shows: Chrome drops the scheme from a URL it is not editing, and while
    it is being edited it holds whatever was typed. `browser` names one browser application.
    """
    if root is None:
        return None
    apps = [app for app in root if app.tag == "application" and name(app) in ((browser,) if browser else BROWSER_APPS)]
    front = active_app(root)
    for app in sorted(apps, key=lambda a: a is not front):
        for window in sorted(_windows(app), key=lambda w: not has_state(w, "active")):
            for entry in window.iter("entry"):
                if name(entry) == ADDRESS_BAR:
                    return text(entry).strip() or None
    return None


def _attrs(element: ET.Element) -> AxAttrs:
    return AxAttrs(ax_role(element.tag), label(element), frame(element))


def clicks(element: ET.Element) -> bool:
    """Whether the element answers a click: it is a menu item, or it offers one of `PRESS_ACTIONS`.
    Neither is a control role on a Mac, where a menu's items are read as text and a web page's
    clickable nodes take AXPress; this is how the walk knows them anyway. A password field is never
    offered as one."""
    if element.tag == "password-text":
        return False
    if element.tag in CLICK_ROLES:
        return True
    return any(f"{{{NS_ACTION}}}{action}_desc" in element.attrib for action in PRESS_ACTIONS)


def _actions(element: ET.Element) -> list[str]:
    return [AX_PRESS] if clicks(element) else []


def walk(app: ET.Element | None, display_w: float, display_h: float) -> tuple[list[AxNode], list[AxNode], bool]:
    """Labelled on-screen controls under `app`, in the shape `Desktop.actionable_elements` returns.

    A control is a node with a control role, or one that answers a click, as a Mac walk keeps
    whatever takes AXPress. But nothing in a VM can be pressed through this tree: no node comes back
    pressable, none carries an element ref, and the off-screen list, which exists only for pressing,
    stays off and empty. So a click is the only way to a control, and one under a popup says so.
    """
    if app is None:
        return [], [], False
    found, _offscreen, capped = walk_actionable(app, list, _attrs, _actions, display_w, display_h, offscreen_cap=0)
    return covered([replace(node, ref=None, pressable=False) for node in found], app), [], capped


def covered(nodes: list[AxNode], app: ET.Element) -> list[AxNode]:
    """The nodes, each control of the page that sits under a popup marked with that popup.

    Chrome draws each bubble, menu, and dialog as a window of its own, which the tree lists beside
    the browser window: an `alert` ("Restore pages?"), a `dialog` ("Bookmark added"), a nameless
    `frame` (a menu). The browser windows are the frames with a name. A popup is drawn above the
    window it belongs to, so a click on a page control whose center lies inside one lands on the
    popup; where two hold the point, the one listed last, opened last, is on top. A window with no
    control of its own is not one: Chrome's status bubble, the link address it shows in a corner,
    moves out of the pointer's way. A popup's controls are known by role and frame, since Chrome
    lists a popup inside the browser window as well, and the walk keeps only the first copy.
    """
    popups = []
    for window in _windows(app):
        box = frame(window)
        if box is None or (window.tag == "frame" and name(window)):
            continue
        own = {(ax_role(element.tag), frame(element)) for element in window.iter()}
        controls = [node for node in nodes if (node.role, (node.x, node.y, node.w, node.h)) in own]
        if controls:
            close = next((n for n in controls if n.role == "AXButton" and n.label.strip().lower() in CLOSE_LABELS), None)
            popups.append((Popup(name(window), *box, close=close), own))
    out = []
    for node in nodes:
        if not any((node.role, (node.x, node.y, node.w, node.h)) in own for _, own in popups):
            center = (node.x + node.w / 2, node.y + node.h / 2)
            over = [popup for popup, _ in popups if popup.holds(center)]
            node = replace(node, covered_by=over[-1] if over else None)
        out.append(node)
    return out
