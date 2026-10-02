"""The VM's accessibility tree, fetched by jev itself rather than handed over with each observation.

OSWorld's observation can carry the tree, but its fetch walks every application on the desktop and
takes about 2.4 s of the 4.9 s each OSWorld step takes, most of it on GNOME Shell's nodes, which jev
never reads. So jev's
runner asks OSWorld for the screenshot alone, and jev fetches the tree when the observation comes:
`light_walk.py` runs in the VM through OSWorld's `/run_python` endpoint and walks the application in
front alone, asking each node only what `a11y.py` reads. It prints the XML OSWorld's own fetch
writes, so `a11y.py` reads either one.

When the light walk fails, times out, or declines (a spreadsheet, a tree past its node cap), the
tree comes from OSWorld's own fetch instead, and the fallback is logged and counted in the run's
`run.json`. Neither may hold up the run: OSWorld's controller asks for its tree with no timeout at
all, and on chrome/2ad9387a, after a click on Chrome's menu, that request never came back. So each
call on the controller runs on a thread of its own, with a deadline, and when both miss theirs the
step has no tree, which `a11y.py` reads as nothing known, and OCR still reads the screen.

`JEV_OSWORLD_TREE_CHECK=1` also fetches OSWorld's tree after each light one, and the light one
again after that, and records whether what jev reads of the two trees matches: the app,
the window, the focused field, the URL, and every control with its box. The second light walk says
whether the screen held still in between, which a check is worth nothing without. It makes each
step slower, so it is for a check run, not a benchmark.

Nothing here reaches this machine: the controller is OSWorld's, and it talks to the VM.
"""

from __future__ import annotations

import functools
import logging
import os
import statistics
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from ..ax_walk import AX_ACTIONABLE_ROLES
from . import a11y, light_walk

LIGHT = "jev-light"  # the trees came from jev's walk, or from OSWorld's fetch where that one failed
LIGHT_CHECKED = "jev-light-checked"  # the same, each checked against OSWorld's: its times are no benchmark
OSWORLD = "osworld"  # the trees came with OSWorld's observations
# How long the light walk may take before OSWorld's fetch stands in: 5,000 nodes take 2 s. It is the
# request's own timeout, and the deadline too: the controller tries a dropped connection again, for as
# long as it takes.
LIGHT_SECONDS = 10.0
FULL_SECONDS = 20.0  # how long OSWorld's own fetch may take, which asks with no timeout, before the step goes without
WARM_SECONDS = 5.0  # how long the walk waits for a fresh Chrome to build its tree; the light walk's deadline holds it
NODE_CAP = 20_000  # past this many nodes the walk stops, and OSWorld's fetch stands in: a tree that big is a runaway
CHECK = "JEV_OSWORLD_TREE_CHECK"  # "1" checks each light tree against OSWorld's on the same screen
CHECK_DISPLAY = (1920.0, 1080.0)  # the display a check walks for controls; both trees are walked on the same one
SHELL = "gnome-shell"  # the application the walk tries last when no window is active: its tree is the largest

log = logging.getLogger(__name__)


class Controller(Protocol):
    """The two calls jev makes on OSWorld's `PythonController`."""

    def run_python_script(self, script: str, timeout: float = ...) -> dict | None: ...

    def get_accessibility_tree(self) -> str | None: ...


@dataclass(frozen=True)
class Fetched:
    """One observation's tree, and how it was had."""

    xml: str | None  # as the VM sent it; None when neither fetch brought one
    root: ET.Element | None  # parsed here, on the fetching thread
    seconds: float  # from the start of the fetch to the tree, a fallback included
    light: bool  # whether jev's walk made it
    fallback: str | None = None  # why OSWorld's fetch stood in, when it did
    full: str | None = None  # OSWorld's tree of the same screen, fetched for a check
    check: dict | None = None  # what the check found

    @property
    def nodes(self) -> int:
        return 0 if self.root is None else sum(1 for _ in self.root.iter())


def settings() -> dict:
    """What `light_walk` needs to know of `a11y.py`: which roles are windows, whose actions it never
    reads (a control's, a menu item's, and a password's), which entries hold the URL, and the
    browsers. A password's text is never fetched: `a11y.py` never reads it."""
    controls = {tag for tag in a11y.ROLES if a11y.ax_role(tag) in AX_ACTIONABLE_ROLES}
    return {
        "window_roles": sorted(a11y.WINDOW_ROLES),
        "control_roles": sorted(controls | a11y.CLICK_ROLES | {"password-text"}),
        "text_roles": ["entry"],
        "no_text_roles": ["password-text"],
        "browser_apps": list(a11y.BROWSER_APPS),
        "address_bar": a11y.ADDRESS_BAR,
        "last_apps": [SHELL],
        "node_cap": NODE_CAP,
        "warm_seconds": WARM_SECONDS,
    }


@functools.cache
def script() -> str:
    """The Python the VM runs: `light_walk.py` as it stands, and a call to its `main`."""
    source = Path(light_walk.__file__).read_text(encoding="utf-8")
    return f"{source}\n\nmain({settings()!r})\n"


class Missed(RuntimeError):
    """The light walk brought no tree; the message says why."""


class Later:
    """A value made on a daemon thread of its own, or one given at once. `result()` waits for it and
    raises what making it raised; `wait()` only waits, for `seconds` at most when given, and says
    whether it came. A daemon, so a call that never returns holds up no exit."""

    def __init__(self, make: Callable[[], object] | None = None, *, value: object = None) -> None:
        self._done = threading.Event()
        self._value, self._error = value, None
        if make is None:
            self._done.set()
        else:
            threading.Thread(target=self._make, args=(make,), name="jev-osworld-tree", daemon=True).start()

    def _make(self, make: Callable[[], object]) -> None:
        try:
            self._value = make()
        except BaseException as error:
            self._error = error
        finally:
            self._done.set()

    def wait(self, seconds: float | None = None) -> bool:
        return self._done.wait(seconds)

    def result(self):
        self._done.wait()
        if self._error is not None:
            raise self._error
        return self._value


def within(seconds: float, call: Callable[[], object], what: str):
    """`call()`, or TimeoutError once `seconds` pass without it. The call goes on, unheeded, on a daemon
    thread: nothing can stop a request in flight, but nothing need wait for it either."""
    later = Later(call)
    if not later.wait(seconds):
        raise TimeoutError(f"{what} gave no answer in {seconds:g} s")
    return later.result()


def fetch(
    controller: Controller, *, timeout: float | None = None, full_timeout: float | None = None, check: bool = False
) -> Fetched:
    """The tree of the screen as it is now: jev's light walk, or OSWorld's fetch when that fails, or
    none, when that fails too. `timeout` and `full_timeout` default to `LIGHT_SECONDS` and
    `FULL_SECONDS` as they stand when the fetch starts."""
    timeout = LIGHT_SECONDS if timeout is None else timeout
    full_timeout = FULL_SECONDS if full_timeout is None else full_timeout
    started = time.perf_counter()
    try:
        xml, root = light(controller, timeout)
    except Missed as miss:
        log.warning("jev's light tree walk failed (%s); OSWorld's full fetch stands in", miss)
        full = full_tree(controller, full_timeout)
        fallback = str(miss) if full is not None else f"{miss}; and OSWorld's fetch brought none either"
        return Fetched(full, a11y.parse(full), time.perf_counter() - started, light=False, fallback=fallback)
    fetched = Fetched(xml, root, time.perf_counter() - started, light=True)
    return compare(controller, fetched, timeout, full_timeout) if check else fetched


def light(controller: Controller, timeout: float = LIGHT_SECONDS) -> tuple[str, ET.Element]:
    """The light walk's XML, and its root. Raises `Missed` when there is none, and within `timeout`,
    whatever the controller does meanwhile."""
    try:
        reply = within(timeout, lambda: controller.run_python_script(script(), timeout=timeout), "the light walk")
    except TimeoutError as error:
        raise Missed(str(error)) from error
    except Exception as error:  # the controller retries a dropped connection itself; past that, it is a miss
        raise Missed(f"the request raised {error!r}") from error
    if not isinstance(reply, dict):
        raise Missed(f"the reply was {reply!r}")
    output = reply.get("output") or ""
    if reply.get("status") != "success":
        detail = reply.get("error") or reply.get("message") or "no detail"
        raise Missed(f"the walk failed: {' '.join(str(detail).split())[-300:]}")
    root = a11y.parse(output)
    if root is None:
        raise Missed(f"the walk printed no tree: {output[:120]!r}")
    return output, root


def full_tree(controller: Controller, timeout: float = FULL_SECONDS) -> str | None:
    """OSWorld's own fetch of the whole desktop, or None when it fails or takes past `timeout`. It
    retries by itself, and asks with no timeout of its own."""
    try:
        return within(timeout, controller.get_accessibility_tree, "OSWorld's tree fetch")
    except Exception as error:
        log.warning("OSWorld's tree fetch failed: %s", error)
        return None


# ----- the check -------------------------------------------------------------------------------


def view(root: ET.Element | None) -> dict:
    """What jev reads of a tree: the app in front, its window, the focused field, the URL, and every
    control the walk finds, with its box."""
    app = a11y.active_app(root)
    field = a11y.focused_field(root)
    found, _, capped = a11y.walk(app, *CHECK_DISPLAY)
    return {
        "app": a11y.name(app),
        "window": a11y.frame(a11y.active_window(root)),
        "field": asdict(field) if field is not None else None,
        "url": a11y.browser_url(root),
        "items": [(node.role, node.label, node.x, node.y, node.w, node.h) for node in found],
        "capped": capped,
    }


def differences(light_view: dict, full_view: dict) -> dict:
    """Where two views part, in a few words each: a key's two values, or the controls only one has."""
    out: dict = {}
    for key in light_view:
        if light_view[key] == full_view[key]:
            continue
        if key == "items":
            ours, theirs = set(light_view[key]), set(full_view[key])
            out[key] = {"light only": sorted(ours - theirs)[:10], "full only": sorted(theirs - ours)[:10]}
        else:
            out[key] = {"light": light_view[key], "full": full_view[key]}
    return out


def compare(controller: Controller, fetched: Fetched, timeout: float, full_timeout: float = FULL_SECONDS) -> Fetched:
    """`fetched` with OSWorld's tree of the same screen, and whether jev reads the two alike. A second
    light walk after OSWorld's fetch says whether the screen held still across it."""
    started = time.perf_counter()
    full = full_tree(controller, full_timeout)
    full_seconds = time.perf_counter() - started
    full_root = a11y.parse(full)
    ours, theirs = view(fetched.root), view(full_root)
    try:
        still = view(light(controller, timeout)[1]) == ours
    except Missed:
        still = False
    check = {
        "light_seconds": round(fetched.seconds, 3),
        "full_seconds": round(full_seconds, 3),
        "light_nodes": fetched.nodes,
        "full_nodes": 0 if full_root is None else sum(1 for _ in full_root.iter()),
        "still": still,
        "match": ours == theirs,
        "differences": differences(ours, theirs),
    }
    return Fetched(fetched.xml, fetched.root, fetched.seconds, light=True, full=full, check=check)


# ----- the run's record ------------------------------------------------------------------------


def _spread(values: list[float]) -> dict:
    return {"mean": round(statistics.fmean(values), 3), "max": round(max(values), 3)} if values else {}


def summary(fetched: list[Fetched]) -> dict:
    """What a run's trees cost and where they came from, for its `run.json`; nothing when the trees
    came with OSWorld's observations."""
    if not fetched:
        return {}
    fallbacks = [f.fallback for f in fetched if not f.light]
    out: dict = {
        "tree_fetches": len(fetched),
        "tree_fallbacks": len(fallbacks),
        "tree_missing": sum(f.root is None for f in fetched),  # steps that went without a tree
        "tree_seconds": _spread([f.seconds for f in fetched]),
        "tree_nodes": _spread([f.nodes for f in fetched]),
    }
    if fallbacks:
        out["tree_fallback_reasons"] = sorted(set(map(str, fallbacks)))[:5]
    checks = [f.check for f in fetched if f.check is not None]
    if checks:
        still = [c for c in checks if c["still"]]
        out["tree_check"] = {
            "compared": len(checks),
            "still": len(still),  # only these count: the screen may have changed under the others
            "matched": sum(c["match"] for c in still),
            "light_seconds": _spread([c["light_seconds"] for c in checks]),
            "full_seconds": _spread([c["full_seconds"] for c in checks]),
            "light_nodes": _spread([c["light_nodes"] for c in checks]),
            "full_nodes": _spread([c["full_nodes"] for c in checks]),
            "mismatches": [c["differences"] for c in still if not c["match"]][:10],
        }
    return out


def checking() -> bool:
    return os.environ.get(CHECK) == "1"
