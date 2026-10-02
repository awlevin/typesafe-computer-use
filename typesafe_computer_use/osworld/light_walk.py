"""jev's accessibility-tree walk, run inside an OSWorld VM: the application in front, alone.

OSWorld's `/accessibility` endpoint walks every application on the desktop, about 2,500 nodes on a
Chrome task (2,100 of them GNOME Shell's), asks each node about ten questions over D-Bus, and runs
`libreoffice --version` first, every time: 2.4 s a step. jev reads one application of that tree
(`a11y.py`). This walk finds that application as `a11y.active_app` does and walks it alone, asking
each node only what `a11y.py` reads:

- every node: its role, name, states, and children;
- a node that is visible and showing: its extents, which OSWorld writes for no other node;
- a node with no name, a focused node, and an entry: its text, the label, value, or URL, but never
  a password's;
- a focused node: its attributes, for the placeholder;
- a showing node whose role is no control: its actions' names, which say whether it takes a click.

It walks every node of that application, not only the showing ones: in Chrome a showing node can
sit under one that is not, as the options of an open `<select>` do under their hidden popup, and a
focused field under a section of no size.

Chrome builds its tree only once an assistive technology shows itself, and what shows one is a
question for the attributes of the application or its window, which OSWorld's fetch asks of every
node and this walk otherwise never would: without it, Chrome's window stays empty, step after step.
So each walk first asks each browser for its attributes, and a browser in front whose active window
is still empty is walked again, a quarter second apart, until the window fills or `warm_seconds`
pass.

When no window is active, the application in front is the first to hold a focused element other
than a top-level window, and then the first with a focused top-level window, as in `a11y.active_app`.
Finding it takes walking the applications one by one, GNOME Shell last. When the application in
front is not the browser, the browser's windows and address bar come too, so `a11y.browser_url`
reads the same URL as from OSWorld's tree.

It prints the tree as OSWorld's server writes it: the tag is the role with its spaces hyphenated,
`name` the name, `st:<state>="true"` each state, `cp:screencoord` and `cp:size` the extents, the
text the element's text, and `act:<action>_desc` each action (with no description), so `a11y.py`
stays the one parser. OSWorld's depth and width limits hold here too. A spreadsheet, whose cells
OSWorld's walk picks out by hand, and a tree past the node cap are left to OSWorld's fetch.

`tree.py` sends this file to the VM through OSWorld's `/run_python` endpoint with a call to `main`
appended, and falls back to OSWorld's own fetch when it fails. It runs on the VM's own Python (3.8
or later) with the standard library and PyGObject's Atspi, which OSWorld's server uses itself. On
the host only tests run it, over a fake desktop.
"""

from __future__ import annotations

import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from contextlib import suppress
from itertools import islice

NAMESPACES = {  # the prefixes and namespaces of OSWorld's Ubuntu tree
    "st": "https://accessibility.ubuntu.example.org/ns/state",
    "attr": "https://accessibility.ubuntu.example.org/ns/attributes",
    "cp": "https://accessibility.ubuntu.example.org/ns/component",
    "act": "https://accessibility.ubuntu.example.org/ns/action",
}
ST, ATTR, CP, ACT = ("{" + NAMESPACES[prefix] + "}" for prefix in ("st", "attr", "cp", "act"))
MAX_DEPTH = 50  # OSWorld's: a node this deep is written without its children
MAX_WIDTH = 1024  # OSWorld's: the children past this many are left out
STATE_PREFIX = "ATSPI_STATE_"  # Atspi's name for a state, before the one OSWorld writes
DROPPED = ("\ufffc", "\ufffd")  # characters OSWorld drops from text: an embedded object, and a bad byte
SPREADSHEET = "document spreadsheet"  # the role whose cells OSWorld's walk picks out by hand
WARM_POLL = 0.25  # seconds between two walks of a browser whose tree is not built yet
NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")  # no XML 1.0 document holds these


class Declined(RuntimeError):
    """A tree this walk leaves to OSWorld's fetch."""


def clean(value: str) -> str:
    """Text as XML can hold it."""
    return NOT_XML.sub("", value)


class Walk:
    """One walk of the desktop. `api` answers the questions a node is asked (see `AtspiApi`), and
    `settings` holds the names `a11y.py` reads, which `tree.settings` builds from it."""

    def __init__(self, api, settings: dict) -> None:
        self.api = api
        self.windows = set(settings["window_roles"])
        self.controls = set(settings["control_roles"])
        self.text_roles = set(settings["text_roles"])
        self.no_text_roles = set(settings["no_text_roles"])
        self.browsers = list(settings["browser_apps"])
        self.address_bar = settings["address_bar"]
        self.last = set(settings["last_apps"])
        self.cap = int(settings["node_cap"])
        self.nodes = 0
        self.front: ET.Element | None = None  # the application in front, once `tree` found it

    # ----- one node --------------------------------------------------------------------------

    def tag(self, node) -> str:
        role = self.api.role(node).strip()
        if role == SPREADSHEET:
            raise Declined("a spreadsheet")
        return (role or "unknown").replace(" ", "-")

    def count(self) -> None:
        self.nodes += 1
        if self.nodes > self.cap:
            raise Declined(f"more than {self.cap} nodes")

    def states(self, node) -> list:
        return list(self.api.states(node))

    def node(self, node, depth: int, children: bool = True) -> ET.Element:
        """`node` as OSWorld writes it, with what `a11y.py` reads of it, and its subtree."""
        self.count()
        api = self.api
        tag = self.tag(node)
        name = api.name(node) or ""
        states = self.states(node)
        attrib = {"name": clean(name)}
        for state in states:
            attrib[ST + state] = "true"
        focused = "focused" in states
        showing = "visible" in states and "showing" in states
        if focused:
            for key, value in (api.attributes(node) or {}).items():
                if key:
                    attrib[ATTR + key] = clean(str(value))
        if showing:
            box = api.extents(node)
            if box is not None:
                attrib[CP + "screencoord"] = str(tuple(box[:2]))
                attrib[CP + "size"] = str(tuple(box[2:]))
            if tag not in self.controls:
                for action in api.actions(node):
                    attrib[ACT + action.replace(" ", "-") + "_desc"] = ""
        element = ET.Element(tag, attrib)
        if tag not in self.no_text_roles and (not name.split() or focused or tag in self.text_roles):
            text = api.text(node) or ""
            for dropped in DROPPED:
                text = text.replace(dropped, "")
            if text:
                element.text = clean(text)
        if children and depth < MAX_DEPTH:
            try:  # as OSWorld's walk does, a child that fails keeps the ones before it
                for index, child in enumerate(api.children(node)):
                    if index == MAX_WIDTH:
                        break
                    element.append(self.node(child, depth + 1))
            except Declined:
                raise
            except Exception:
                pass
        return element

    # ----- the desktop -----------------------------------------------------------------------

    def tree(self, apps: list) -> ET.Element:
        """The desktop: the application in front, whole, and then the browser's address bar when
        that application is not the browser."""
        root = ET.Element("desktop-frame")
        tops = {id(app): self.top_level(app) for app in apps}
        for app in apps:
            if self.api.name(app) in self.browsers:
                self.wake(app, [window for window, _, _ in tops[id(app)]])
        walked: dict[int, ET.Element] = {}
        front = next((app for app in apps if any("active" in states for _, _, states in tops[id(app)])), None)
        if front is None:
            front = self.holding_focus(apps, tops, walked)
        if front is not None:
            self.front = walked.get(id(front)) if id(front) in walked else self.node(front, 1)
            root.append(self.front)
        for app in apps:
            if app is not front and self.api.name(app) in self.browsers:
                root.append(walked.get(id(app)) if id(app) in walked else self.address_bar_of(app, tops[id(app)]))
        return root

    def wake(self, app, windows: list) -> None:
        """Ask a browser and its windows for their attributes: to Chrome, that is an assistive
        technology asking, and it builds its tree for one."""
        for node in [app, *windows]:
            with suppress(Exception):
                self.api.attributes(node)

    def cold(self) -> bool:
        """Whether the application in front is a browser whose active window holds nothing yet."""
        front = self.front
        if front is None or front.get("name") not in self.browsers:
            return False
        return any(w.tag in self.windows and w.get(ST + "active") == "true" and len(w) == 0 for w in front)

    def top_level(self, app) -> list:
        """The application's top-level windows: each one's node, tag, and states. An application
        that fails to say has none."""
        out = []
        try:
            for child in self.api.children(app):
                tag = self.tag(child)
                if tag in self.windows:
                    out.append((child, tag, self.states(child)))
        except Declined:
            raise
        except Exception:
            pass
        return out

    def holding_focus(self, apps: list, tops: dict, walked: dict):
        """With no window active: the first application holding a focused element that is not one of
        its top-level windows, or else the first with a focused top-level window. Each application
        walked on the way is kept in `walked`, so none is walked twice."""
        order = sorted(apps, key=lambda app: self.api.name(app) in self.last)
        for app in order:
            element = walked[id(app)] = self.node(app, 1)
            windows = [child for child in element if child.tag in self.windows]
            if any(e.get(ST + "focused") == "true" and e not in windows for e in element.iter()):
                return app
        return next((app for app in apps if any("focused" in states for _, _, states in tops[id(app)])), None)

    def address_bar_of(self, app, windows: list) -> ET.Element:
        """A browser that is not in front: the application, its windows, and each window's address
        bar, searched for breadth first."""
        element = self.node(app, 1, children=False)
        for window, _, _ in windows:
            top = self.node(window, 2, children=False)
            element.append(top)
            queue = deque([(window, 2)])
            while queue:
                node, depth = queue.popleft()
                self.count()
                try:  # a node that fails is passed over, and so is what lies under it
                    if depth > 2 and self.tag(node) == "entry" and (self.api.name(node) or "") == self.address_bar:
                        top.append(self.node(node, depth, children=False))
                        break
                    if depth < MAX_DEPTH:
                        queue.extend((child, depth + 1) for child in islice(self.api.children(node), MAX_WIDTH))
                except Declined:
                    raise
                except Exception:
                    pass
        return element


class AtspiApi:
    """The questions `Walk` asks, put to AT-SPI through PyGObject, as OSWorld's server asks them
    through pyatspi, which wraps the same calls."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        self.atspi = Atspi

    def apps(self) -> list:
        desktop = self.atspi.get_desktop(0)
        return [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]

    def role(self, node) -> str:
        return node.get_role_name() or ""

    def name(self, node) -> str:
        return node.get_name() or ""

    def states(self, node) -> list:
        return [state.value_name[len(STATE_PREFIX) :].lower() for state in node.get_state_set().get_states()]

    def children(self, node):
        for i in range(node.get_child_count()):
            yield node.get_child_at_index(i)

    def extents(self, node):
        if node.get_component_iface() is None:
            return None
        box = self.atspi.Component.get_extents(node, self.atspi.CoordType.SCREEN)
        return (box.x, box.y, box.width, box.height)

    def text(self, node):
        if node.get_text_iface() is None:
            return None
        return self.atspi.Text.get_text(node, 0, self.atspi.Text.get_character_count(node))

    def actions(self, node) -> list:
        if node.get_action_iface() is None:
            return []
        return [self.atspi.Action.get_action_name(node, i) for i in range(self.atspi.Action.get_n_actions(node))]

    def attributes(self, node) -> dict:
        return dict(node.get_attributes() or {})


def render(root: ET.Element) -> str:
    """The tree as XML in ASCII, with OSWorld's prefixes, so no locale on the way can garble it."""
    for prefix, uri in NAMESPACES.items():
        ET.register_namespace(prefix, uri)
    return ET.tostring(root, encoding="unicode").encode("ascii", "xmlcharrefreplace").decode("ascii")


def warm_tree(api, settings: dict, clock=time.monotonic, sleep=time.sleep) -> ET.Element:
    """The desktop's tree, walked again while the browser in front has not built its own yet, for
    `warm_seconds` at most."""
    deadline = clock() + float(settings["warm_seconds"])
    while True:
        walk = Walk(api, settings)
        root = walk.tree(api.apps())
        if not walk.cold() or clock() >= deadline:
            return root
        sleep(WARM_POLL)


def main(settings: dict) -> None:
    """Print the VM's tree."""
    sys.stdout.write(render(warm_tree(AtspiApi(), settings)))
