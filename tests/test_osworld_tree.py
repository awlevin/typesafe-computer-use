"""jev's own accessibility tree of an OSWorld VM, offline.

The light walk runs over fake desktops made of trees OSWorld's own fetch sent (tests/fixtures/osworld),
answering each question from the XML, and what jev reads of its tree must be what it reads of
OSWorld's. The fetch, its fallback, and its check run over a fake controller, and the adapter's
fetch per observation over a fake fetch. Nothing here reaches a VM or this machine.
"""

from __future__ import annotations

import ast
import copy
import logging
import threading
import time
from collections import Counter
from functools import partial
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from typesafe_computer_use.osworld import a11y, light_walk, tree
from typesafe_computer_use.osworld.desktop import OSWorldDesktop

FIXTURES = Path(__file__).parent / "fixtures" / "osworld"
TREES = sorted(FIXTURES.glob("ubuntu-*.xml"))
CAPTURED = FIXTURES / "ubuntu-chrome-captured.xml"
NO_ACTIVE_WINDOW = FIXTURES / "ubuntu-chrome-no-active-window-captured.xml"
SELECT_OPEN = FIXTURES / "ubuntu-chrome-select-open-captured.xml"
ST, ACT, CP = (f"{{{ns}}}" for ns in (a11y.NS_STATE, a11y.NS_ACTION, a11y.NS_COMPONENT))
WAIT = 10.0  # every wait on a thread here is bounded


class FakeDesktop:
    """The questions the walk asks, answered from a tree OSWorld sent, each one counted per node."""

    def __init__(self, root, fail_on: set | None = None) -> None:
        self.root = root
        self.asked: Counter = Counter()  # (question, node) -> times
        self.fail_on = fail_on or set()  # nodes whose role cannot be read, as a defunct one's cannot

    def apps(self) -> list:
        return list(self.root)

    def _ask(self, question: str, node) -> None:
        self.asked[question, node] += 1

    def role(self, node) -> str:
        self._ask("role", node)
        if node in self.fail_on:
            raise RuntimeError("the object is gone")
        return node.tag.replace("-", " ")

    def name(self, node) -> str:
        self._ask("name", node)
        return node.get("name") or ""

    def states(self, node) -> list:
        self._ask("states", node)
        return [key[len(ST) :] for key in node.attrib if key.startswith(ST)]

    def children(self, node):
        self._ask("children", node)
        yield from list(node)

    def extents(self, node):
        self._ask("extents", node)
        box = a11y.frame(node)
        return None if box is None else tuple(int(v) for v in box)

    def text(self, node):
        self._ask("text", node)
        return node.text

    def actions(self, node) -> list:
        self._ask("actions", node)
        return [key[len(ACT) : -len("_desc")] for key in node.attrib if key.startswith(ACT) and key.endswith("_desc")]

    def attributes(self, node) -> dict:
        self._ask("attributes", node)
        prefix = f"{{{a11y.NS_ATTRIBUTES}}}"
        return {key[len(prefix) :]: value for key, value in node.attrib.items() if key.startswith(prefix)}

    def asked_of(self, question: str) -> set:
        return {node for (q, node), _ in self.asked.items() if q == question}


def walked(root, **kwargs):
    """The light tree of a fake desktop made of `root`, as the VM would print it, parsed back."""
    desktop = FakeDesktop(root, **kwargs)
    walk = light_walk.Walk(desktop, tree.settings())
    return a11y.parse(light_walk.render(walk.tree(desktop.apps()))), desktop, walk


def load(path: Path):
    return a11y.parse(path.read_text(encoding="utf-8"))


# ----- what jev reads of the light tree ----------------------------------------------------------


@pytest.mark.parametrize("path", TREES, ids=lambda path: path.stem)
def test_jev_reads_the_light_tree_as_it_reads_osworlds(path):
    """The app, its window, the focused field, the URL, and every control with its box: the same off
    both trees, on every tree OSWorld sent that the fixtures hold."""
    full = load(path)
    light, _, _ = walked(full)
    assert tree.view(light) == tree.view(full)


def test_the_light_tree_is_the_app_in_front_alone():
    full = load(CAPTURED)
    light, desktop, walk = walked(full)
    assert [a11y.name(app) for app in light] == ["Google Chrome"]
    shell = next(app for app in full if a11y.name(app) == "gnome-shell")
    assert not desktop.asked_of("extents") & set(shell.iter()), "GNOME Shell is never walked"
    assert walk.nodes == sum(1 for _ in a11y.active_app(full).iter())


def test_the_options_of_an_open_select_come_though_their_popup_is_hidden():
    """Chrome's popup for an open `<select>` is neither visible nor showing, and its options are both:
    a walk of the showing nodes alone would lose the very item to click."""
    full = load(SELECT_OPEN)
    popup = next(e for e in full.iter("menu") if any(a11y.name(item) == "All time" for item in e))
    assert not a11y.has_state(popup, "showing") and not a11y.has_state(popup, "visible")
    light, _, _ = walked(full)
    items = {(n.role, n.label) for n in a11y.walk(a11y.active_app(light), *tree.CHECK_DISPLAY)[0]}
    assert ("AXMenuItem", "All time") in items


def test_with_no_window_active_the_app_holding_the_focus_is_in_front_and_the_shell_is_left_alone():
    """chrome/2ae9ba84: no window active, and Chrome's Name field focused under a section of no size,
    which is not showing."""
    full = load(NO_ACTIVE_WINDOW)
    light, desktop, _ = walked(full)
    assert a11y.name(a11y.active_app(light)) == "Google Chrome"
    assert a11y.focused_field(light).label == "Name"
    shell = next(app for app in full if a11y.name(app) == "gnome-shell")
    assert not desktop.asked_of("extents") & set(shell.iter()), "the shell is tried last, and never was"


def test_with_nothing_else_focused_the_shells_focused_window_is_in_front():
    full = load(NO_ACTIVE_WINDOW)
    (entry,) = [e for e in full.iter("entry") if a11y.has_state(e, "focused")]
    del entry.attrib[ST + "focused"]
    light, _, _ = walked(full)
    assert tree.view(light) == tree.view(full)
    assert a11y.name(a11y.active_app(light)) == "gnome-shell"
    assert a11y.browser_url(light) == a11y.browser_url(full), "the browser behind still gives its URL"


def test_a_browser_behind_another_app_still_gives_its_url_and_nothing_more():
    full = load(CAPTURED)
    chrome = next(app for app in full if a11y.name(app) == "Google Chrome")
    shell = next(app for app in full if a11y.name(app) == "gnome-shell")
    for window in chrome:
        window.attrib.pop(ST + "active", None)
    next(iter(shell)).set(ST + "active", "true")
    (address,) = [e for e in chrome.iter("entry") if a11y.name(e) == a11y.ADDRESS_BAR]
    address.text = "airbnb.com"
    light, _, _ = walked(full)
    assert a11y.name(a11y.active_app(light)) == "gnome-shell"
    assert a11y.browser_url(light, "Google Chrome") == a11y.browser_url(full, "Google Chrome") == "airbnb.com"
    assert tree.view(light) == tree.view(full)
    (behind,) = [app for app in light if a11y.name(app) == "Google Chrome"]
    assert [e.tag for e in behind.iter()] == ["application", "frame", "entry", "alert"], "its windows and address bar"


def test_each_node_is_asked_only_what_a11y_reads():
    full = load(FIXTURES / "ubuntu-chrome.xml")
    _, desktop, _ = walked(full)
    chrome = [e for e in a11y.active_app(full).iter()]
    showing = {e for e in chrome if a11y.has_state(e, "visible") and a11y.has_state(e, "showing")}
    assert desktop.asked_of("extents") == showing, "extents only where OSWorld writes them"
    browser = a11y.active_app(full)
    woken = {browser, *(w for w in browser if w.tag in a11y.WINDOW_ROLES)}  # asked, so Chrome builds its tree
    assert desktop.asked_of("attributes") == {e for e in chrome if a11y.has_state(e, "focused")} | woken
    asked_text = desktop.asked_of("text")
    assert not any(e.tag == "password-text" for e in asked_text), "a password's text never leaves the VM"
    assert all(not a11y.name(e) or a11y.has_state(e, "focused") or e.tag == "entry" for e in asked_text)
    controls = set(tree.settings()["control_roles"])
    assert not any(e.tag in controls for e in desktop.asked_of("actions")), "a control's actions change nothing"
    assert desktop.asked_of("actions") <= showing


def test_a_child_that_fails_keeps_the_ones_before_it_as_osworlds_walk_does():
    full = load(FIXTURES / "ubuntu-chrome.xml")
    section = next(full.iter("section"))
    first, second = list(section)
    light, _, _ = walked(full, fail_on={second})
    (kept,) = [e for e in light.iter("section")]
    assert [a11y.name(e) for e in kept] == [a11y.name(first)]


def test_a_spreadsheet_and_a_tree_past_the_cap_are_left_to_osworlds_fetch():
    full = load(FIXTURES / "ubuntu-chrome.xml")
    next(full.iter("document-web")).tag = "document-spreadsheet"
    with pytest.raises(light_walk.Declined, match="spreadsheet"):
        walked(full)
    small = dict(tree.settings(), node_cap=5)
    desktop = FakeDesktop(load(CAPTURED))
    with pytest.raises(light_walk.Declined, match="more than 5 nodes"):
        light_walk.Walk(desktop, small).tree(desktop.apps())


class Cold(FakeDesktop):
    """A fresh Chrome, as OSWorld's VM starts it: its active window holds nothing until something asks
    it or its application for their attributes, as an assistive technology does, and then for the
    next `building` looks, while it builds its tree. `waking` lists the questions that wake it."""

    def __init__(self, root, building: int = 0, waking: tuple = ("attributes",)) -> None:
        super().__init__(root)
        self.chrome = next(app for app in root if a11y.name(app) in a11y.BROWSER_APPS)
        self.frame = next(w for w in self.chrome if a11y.has_state(w, "active"))
        self.building = building
        self.waking = waking
        self.awake = False

    def attributes(self, node) -> dict:
        if node in (self.chrome, self.frame) and "attributes" in self.waking:
            self.awake = True
        return super().attributes(node)

    def children(self, node):
        if node is self.frame and (not self.awake or self.building > 0):
            self.building -= self.awake
            return iter(())
        return super().children(node)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_a_fresh_chrome_is_asked_for_its_attributes_and_walked_again_until_it_has_built_its_tree():
    """Every task of the first two runs on the VM: with OSWorld's fetch gone, nothing asked Chrome for
    attributes, so its window stayed empty step after step, and jev chose from OCR alone. A fresh
    Chromium in the sandbox builds its tree on that question alone, of all OSWorld's fetch asks."""
    full = load(CAPTURED)
    clock = Clock()
    root = light_walk.warm_tree(Cold(full, building=2), tree.settings(), clock=lambda: clock.now, sleep=clock.sleep)
    assert tree.view(a11y.parse(light_walk.render(root))) == tree.view(full)
    assert clock.slept == [light_walk.WARM_POLL] * 2


def test_a_chrome_that_never_builds_its_tree_is_left_as_it_is_after_the_wait():
    clock = Clock()
    desktop = Cold(load(CAPTURED), waking=())
    root = light_walk.warm_tree(desktop, tree.settings(), clock=lambda: clock.now, sleep=clock.sleep)
    assert clock.now == tree.WARM_SECONDS
    (frame,) = [w for app in root for w in app if a11y.has_state(w, "active")]
    assert len(frame) == 0


def test_only_a_browser_in_front_is_waited_for():
    full = load(CAPTURED)
    desktop = Cold(full, waking=())
    for app in full:
        if a11y.name(app) == "Google Chrome":
            app.set("name", "Files")
    clock = Clock()
    light_walk.warm_tree(desktop, tree.settings(), clock=lambda: clock.now, sleep=clock.sleep)
    assert clock.slept == []


def test_the_tree_is_printed_in_ascii_as_xml_can_hold_it():
    element = light_walk.ET.Element("desktop-frame")
    element.append(light_walk.ET.Element("label", {"name": light_walk.clean("Café \x0b menu")}))
    element[0].text = light_walk.clean("naïve\x01 text")
    xml = light_walk.render(element)
    assert xml.isascii()
    (label,) = a11y.parse(xml)
    assert (label.get("name"), label.text) == ("Café  menu", "naïve text")


# ----- the script the VM runs --------------------------------------------------------------------


def test_the_script_runs_on_the_vms_python_and_calls_main_with_the_settings():
    source = tree.script()
    module = ast.parse(source, feature_version=(3, 8))  # Ubuntu's own Python, which OSWorld's server runs
    call = module.body[-1]
    assert ast.unparse(call.value.func) == "main"
    assert ast.literal_eval(call.value.args[0]) == tree.settings()
    assert "pyautogui" not in source, "OSWorld's controller patches a script that names pyautogui"


def test_the_settings_are_a11ys_own():
    settings = tree.settings()
    assert set(settings["window_roles"]) == a11y.WINDOW_ROLES
    assert {"push-button", "link", "entry", "menu-item", "password-text"} <= set(settings["control_roles"])
    assert not {"section", "static", "label", "heading"} & set(settings["control_roles"])
    assert settings["browser_apps"] == list(a11y.BROWSER_APPS) and settings["address_bar"] == a11y.ADDRESS_BAR
    assert light_walk.NAMESPACES["st"] == a11y.NS_STATE and light_walk.NAMESPACES["cp"] == a11y.NS_COMPONENT
    assert light_walk.NAMESPACES["attr"] == a11y.NS_ATTRIBUTES and light_walk.NAMESPACES["act"] == a11y.NS_ACTION


# ----- the fetch ---------------------------------------------------------------------------------

LIGHT_XML = light_walk.render(walked(load(CAPTURED))[0])
FULL_XML = CAPTURED.read_text(encoding="utf-8")


class FakeController:
    """OSWorld's controller: `/run_python` answers with `replies` in turn, `/accessibility` with the
    full tree."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.scripts: list[tuple[str, float]] = []
        self.full_fetches = 0

    def run_python_script(self, script: str, timeout: float = 90) -> dict | None:
        self.scripts.append((script, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get_accessibility_tree(self) -> str | None:
        self.full_fetches += 1
        return FULL_XML


def ok(xml: str = LIGHT_XML) -> dict:
    return {"status": "success", "output": xml, "error": ""}


def test_the_fetch_runs_the_light_walk_in_the_vm_and_nothing_more():
    controller = FakeController(ok())
    fetched = tree.fetch(controller)
    assert fetched.light and fetched.fallback is None and controller.full_fetches == 0
    assert controller.scripts == [(tree.script(), tree.LIGHT_SECONDS)]
    assert fetched.xml == LIGHT_XML and a11y.name(a11y.active_app(fetched.root)) == "Google Chrome"
    assert fetched.nodes == sum(1 for _ in a11y.parse(LIGHT_XML).iter())


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        ({"status": "error", "message": "Failed", "output": "", "error": "Read timeout after 10 seconds"}, "Read timeout"),
        ({"status": "error", "output": "", "error": "Traceback ...\nImportError: No module named gi"}, "No module named gi"),
        ({"status": "success", "output": "", "error": ""}, "printed no tree"),
        (ConnectionError("the VM is gone"), "the VM is gone"),
        (None, "the reply was None"),
    ],
    ids=["timeout", "no-atspi", "no-output", "raised", "no-reply"],
)
def test_a_failed_walk_falls_back_to_osworlds_fetch_and_says_why(reply, why, caplog):
    controller = FakeController(reply)
    with caplog.at_level(logging.WARNING, logger=tree.__name__):
        fetched = tree.fetch(controller)
    assert not fetched.light and why in fetched.fallback
    assert controller.full_fetches == 1 and fetched.xml == FULL_XML and fetched.root is not None
    assert any("OSWorld's full fetch stands in" in record.getMessage() for record in caplog.records)


class Hung:
    """OSWorld's controller over a VM that stops answering: a call held here returns only when the test
    lets it go, whatever timeout it was given, as OSWorld's tree request did on chrome/2ad9387a."""

    def __init__(self, light: bool, full: bool) -> None:
        self.light, self.full = light, full
        self.release = threading.Event()

    def run_python_script(self, script: str, timeout: float = 90) -> dict:
        if self.light:
            self.release.wait(WAIT)
            return {"status": "error", "output": "", "error": "too late"}
        return {"status": "error", "output": "", "error": "Traceback ...\nRuntimeError: no desktop"}

    def get_accessibility_tree(self) -> str:
        if self.full:
            self.release.wait(WAIT)
        return FULL_XML


@pytest.fixture
def hung():
    made: list[Hung] = []

    def make(*, light: bool, full: bool) -> Hung:
        made.append(Hung(light, full))
        return made[-1]

    yield make
    for controller in made:
        controller.release.set()


def timed_fetch(controller) -> tuple[tree.Fetched, float]:
    started = time.perf_counter()
    fetched = tree.fetch(controller, timeout=0.2, full_timeout=0.3)
    return fetched, time.perf_counter() - started


def test_a_light_walk_that_never_answers_gives_way_to_osworlds_fetch_in_time(hung):
    fetched, seconds = timed_fetch(hung(light=True, full=False))
    assert seconds < 2.0
    assert not fetched.light and fetched.fallback == "the light walk gave no answer in 0.2 s"
    assert fetched.xml == FULL_XML and fetched.root is not None


def test_osworlds_fetch_that_never_answers_leaves_the_step_without_a_tree_in_time(hung):
    """The light walk failed, and OSWorld's fetch, which asks with no timeout, never came back: the step
    goes on with no tree rather than waiting for it."""
    fetched, seconds = timed_fetch(hung(light=False, full=True))
    assert seconds < 2.0
    assert fetched.xml is None and fetched.root is None and not fetched.light
    assert "no desktop" in fetched.fallback and "OSWorld's fetch brought none either" in fetched.fallback
    assert tree.summary([fetched])["tree_missing"] == 1


def test_with_both_hung_a_read_of_the_tree_waits_no_longer_than_both_deadlines(hung):
    controller = hung(light=True, full=True)
    fetch = partial(tree.fetch, controller, timeout=0.2, full_timeout=0.3)
    desktop = OSWorldDesktop(observation(), lambda image: [], lambda actions: observation(), fetch_tree=fetch)
    started = time.perf_counter()
    assert desktop.frontmost_app_and_pid() == ("", 1), "no tree: the app is unknown, and OCR still reads the screen"
    assert desktop.focused_field() is None and desktop.actionable_elements(1, 1920, 1080) == ([], [], False)
    assert time.perf_counter() - started < 2.0
    (fetched,) = desktop.fetched
    assert fetched.root is None


def test_a_check_compares_the_light_tree_with_osworlds_on_a_screen_that_held_still():
    controller = FakeController(ok(), ok())
    fetched = tree.fetch(controller, check=True)
    assert controller.full_fetches == 1 and len(controller.scripts) == 2
    assert fetched.full == FULL_XML
    assert fetched.check["still"] and fetched.check["match"] and fetched.check["differences"] == {}
    assert fetched.check["full_nodes"] > fetched.check["light_nodes"] > 0


def test_a_check_says_when_the_screen_moved_and_where_the_trees_part():
    moved = LIGHT_XML.replace('name="Restore"', 'name="Maximise"', 1)
    assert moved != LIGHT_XML
    controller = FakeController(ok(moved), ok())
    check = tree.fetch(controller, check=True).check
    assert not check["still"] and not check["match"]
    assert [label for _, label, *_ in check["differences"]["items"]["light only"]] == ["Maximise"]


def test_a_runs_record_says_what_its_trees_cost_and_how_many_fell_back():
    fetched = [
        tree.fetch(FakeController(ok())),
        tree.fetch(FakeController({"status": "error", "error": "Read timeout"})),
        tree.fetch(FakeController(ok(), ok()), check=True),
    ]
    summary = tree.summary(fetched)
    assert (summary["tree_fetches"], summary["tree_fallbacks"]) == (3, 1)
    assert set(summary["tree_seconds"]) == {"mean", "max"} and summary["tree_nodes"]["max"] == fetched[1].nodes
    assert summary["tree_fallback_reasons"] == ["the walk failed: Read timeout"]
    assert summary["tree_check"]["compared"] == summary["tree_check"]["matched"] == 1
    assert tree.summary([]) == {}


# ----- the adapter: one fetch per observation, on a thread of its own ---------------------------


def png() -> bytes:
    out = BytesIO()
    Image.new("RGB", (1920, 1080), "white").save(out, format="PNG")
    return out.getvalue()


SCREENSHOT = png()


def observation() -> dict:
    """An observation as OSWorld sends one when asked for no tree."""
    return {"screenshot": SCREENSHOT, "accessibility_tree": None}


class Gated:
    """A fetch that holds until the test lets it go, and logs when it starts and ends."""

    def __init__(self, events: list) -> None:
        self.events = events
        self.go = threading.Event()
        self.started = threading.Event()

    def __call__(self) -> tree.Fetched:
        self.events.append("fetch")
        self.started.set()
        assert self.go.wait(WAIT)
        self.events.append("fetched")
        return tree.Fetched(LIGHT_XML, a11y.parse(LIGHT_XML), 0.1, light=True)


def test_only_a_read_of_the_tree_waits_for_the_fetch_and_the_screenshot_does_not():
    fetch = Gated([])
    desktop = OSWorldDesktop(observation(), lambda image: [], lambda actions: observation(), fetch_tree=fetch)
    assert fetch.started.wait(WAIT), "the observation's fetch starts as it arrives"
    assert desktop.screenshot().size == (1920, 1080), "the screenshot is there while the tree is not"
    found = []
    reader = threading.Thread(target=lambda: found.append(desktop.frontmost_app_and_pid()), daemon=True)
    reader.start()
    reader.join(0.2)
    assert reader.is_alive() and not found, "a read of the tree waits for it"
    fetch.go.set()
    reader.join(WAIT)
    assert found == [("Google Chrome", 1)]
    assert [f.light for f in desktop.fetched] == [True]


def test_the_actions_go_to_osworld_only_once_the_fetch_before_them_is_over():
    events: list = []
    fetch = Gated(events)

    def next_obs(actions):
        events.append(("step", actions))
        return observation()

    desktop = OSWorldDesktop(observation(), lambda image: [], next_obs, fetch_tree=fetch)
    desktop.click_at((10.0, 20.0))
    stepping = threading.Thread(target=desktop.screenshot, daemon=True)
    stepping.start()
    stepping.join(0.2)
    assert events == ["fetch"], "the click waits for the fetch of the screen it was decided on"
    fetch.go.set()
    stepping.join(WAIT)
    assert events[:3] == ["fetch", "fetched", ("step", ["pyautogui.click(10, 20)"])]
    desktop.focused_field()
    assert events[3:] == ["fetch", "fetched"], "the next fetch starts only with the observation after the step"


def test_each_fetched_tree_is_saved_with_osworlds_beside_it_in_a_check(tmp_path):
    checked = tree.fetch(FakeController(ok(), ok()), check=True)
    desktop = OSWorldDesktop(
        observation(), lambda image: [], lambda actions: observation(), fetch_tree=lambda: checked, save_a11y=tmp_path
    )
    desktop.focused_field()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["obs-000-a11y-full.xml", "obs-000-a11y.xml"]
    assert (tmp_path / "obs-000-a11y.xml").read_text(encoding="utf-8") == LIGHT_XML
    assert (tmp_path / "obs-000-a11y-full.xml").read_text(encoding="utf-8") == FULL_XML


def test_a_fetch_that_raises_raises_in_the_read_that_waits_for_it():
    def broken() -> tree.Fetched:
        raise RuntimeError("a bug in the fetch")

    desktop = OSWorldDesktop(observation(), lambda image: [], lambda actions: observation(), fetch_tree=broken)
    with pytest.raises(RuntimeError, match="a bug in the fetch"):
        desktop.focused_field()


def test_osworlds_own_tree_needs_no_fetch():
    full = copy.deepcopy(observation())
    full["accessibility_tree"] = FULL_XML
    desktop = OSWorldDesktop(full, lambda image: [], lambda actions: observation())
    assert desktop.frontmost_app_and_pid() == ("Google Chrome", 1) and desktop.fetched == []
