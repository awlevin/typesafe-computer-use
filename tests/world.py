"""A deterministic simulated computer, so the real step loop can be driven end to end.

The package is one loop over four seams: capture the screen, perceive items, ask the classifier,
act on the machine. Every unit test here covers one seam; nothing covered the loop itself, where
the stop rules live. So this module fakes the two outer seams -- the screen and the machine -- and
leaves `runner.run`, `decide`, and `actions` exactly as they ship. A scenario then reads as a page
graph plus a policy, and a failure means the architecture, not the harness.

The screen is a page of rows: item N occupies the pixel box (100, 100+40N, 600, 130+40N) on a
2000x1200 capture at scale 2.0, which is what `tests/conftest.py`'s `screen` fixture describes.
Clicks come back as screen points, so the world halves them to find the row they hit.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from PIL import Image
from typesafe_sdk import Noul

from typesafe_computer_use import actions, runner
from typesafe_computer_use.actions import Context
from typesafe_computer_use.decide import CLICK_KINDS
from typesafe_computer_use.goal import LiveGoal
from typesafe_computer_use.models import AxNode, Field, Item, MenuItem, Screen, WindowRef
from typesafe_computer_use.platform_adapter import desktop
from typesafe_computer_use.runner import RunConfig, RunState, run

CAPTURE = (2000, 1200)
SCALE = 2.0
ROW_TOP = 100.0  # first row's top edge, in capture pixels
ROW_HEIGHT = 40.0
ROW_TEXT_HEIGHT = 30.0  # a row's text is shorter than its pitch, so there is a gap between rows
ROW_X1, ROW_X2 = 100.0, 600.0
LOADING = "Loading..."

# An off-screen control is parked far above the display, the way Chromium parks a scrolled-out link.
OFFSCREEN_Y = -4200.0

Step = tuple  # (kind, target) or (kind, target, confidence)
Policy = Callable[[dict, dict], Step]
Rows = list  # of str, or (text, role)


COLUMN_GAP = 10.0  # the gutter between two columns of one row, so their boxes do not touch


def row_box(n: int) -> tuple[float, float, float, float]:
    """The pixel box of the Nth row, top to bottom."""
    top = ROW_TOP + ROW_HEIGHT * n
    return (ROW_X1, top, ROW_X2, top + ROW_TEXT_HEIGHT)


def cell_box(n: int, k: int, columns: int) -> tuple[float, float, float, float]:
    """The pixel box of column K of N in the Nth row: the row's width, split evenly, minus a gutter.

    A row of one column keeps the whole width, so a page of plain rows lays out exactly as before.
    """
    if columns == 1:
        return row_box(n)
    top = ROW_TOP + ROW_HEIGHT * n
    width = ROW_X2 - ROW_X1
    return (ROW_X1 + width * k / columns, top, ROW_X1 + width * (k + 1) / columns - COLUMN_GAP, top + ROW_TEXT_HEIGHT)


@dataclass(frozen=True)
class Ref:
    """An accessibility element handle, the opaque object an action is sent to.

    The real one is a PyObjC element; here it only has to be the same object every capture and to
    say what it stands for. An element the loop presses is named by the action pressing it applies
    ("click:Tickets"); a text field is named by its label. The world keeps one Ref per name and
    tells the two kinds apart by which cache holds it, so a stray ref reads as neither.
    """

    name: str


@dataclass(frozen=True)
class Cell:
    """One item of the current page: its text, its role, the row it sits in, and its pixel box."""

    text: str
    role: str
    row: int
    box: tuple[float, float, float, float]


@dataclass
class Page:
    """One screen of the simulated computer, and what each action does to it.

    `items` are rows of text, or (text, role) for a control the app declares through accessibility.
    A row that is a list holds several items side by side, laid out left to right across the row:
    ["Bruno Mars", "Sep 25", "Buy"] is one line of a listing. A page whose rows change on their own
    -- a clock, a ticker -- passes a callable(world) instead, which is asked again on every capture.
    `on` maps an action as the world names it ("click:Tickets", "enter", "type:hello", "open:<url>")
    to the next page's name, or to a callable(world) returning a name or None to stay. Text that
    appears more than once on the page cannot name one item, so those clicks are keyed by row
    instead: "click:Buy@1" is the Buy of the second row. An action
    with no entry leaves the page alone, which is the "nothing happened" case the runner must cope
    with. The other gestures read the same way: "double-click:Report.pdf", "right-click:Report.pdf",
    "cmd+s" for a chord, "menu:<path>", "window:<title>", and "activate:<app>" for an app brought up
    by name.
    """

    name: str
    items: Rows | Callable[[World], Rows] = dataclasses.field(default_factory=list)
    url: str | None = None  # browser pages have one; app pages do not
    app: str = "Google Chrome"
    field: str | None = None  # label of the text field focused on this page, if any
    offscreen: tuple[str, ...] = ()  # labels the app exposes without showing
    loads_in: int = 0  # steps of "wait" before the items appear
    no_ax_value: bool = False  # the field refuses to have its value set, so text has to be typed in
    covered_by: str | None = None  # an overlay eating every mouse click: the text of what is really hit
    menu: tuple = ()  # the app's menu bar: paths, or (path, chord) for a command with a shortcut
    windows: tuple[str, ...] = ()  # the app's titled windows, the one in front first
    secure: bool = False  # the focused field hides what is typed, as a password field does
    on: dict[str, str | Callable[[World], str | None]] = dataclasses.field(default_factory=dict)


class World:
    """The pages, which one is showing, what was typed, and every action the world received.

    `log` holds the world's own action names in order. Mouse clicks also land in `mouse` as the raw
    point, because a press through accessibility and a click on the pixel under the item both read
    as "click:<text>" here, and a scenario needs to tell the two apart.

    `typed` is a plain dict a scenario may seed before the run, for a field that already holds
    something when the loop first sees it.
    """

    def __init__(self, pages: list[Page], start: str | None = None):
        self.pages = {p.name: p for p in pages}
        self.page = self.pages[start or pages[0].name]
        self.typed: dict[str, str] = {}
        self.running: set[str] = set()  # the apps open, as the adapter's running_apps reports them
        # Called before each capture with the world, the way speech lands between two steps of a
        # dictated run: a scenario grows or finishes a LiveGoal here, keyed off `ticks`.
        self.between: Callable[[World], None] | None = None
        self.log: list[str] = []
        self.mouse: list[tuple[float, float]] = []
        self.fake: FakeTypeSafe | None = None  # the classifier `drive` built, for fake.states
        self.asked: list[str] = []  # the questions the writer put to the user
        self.loading = {p.name: p.loads_in for p in pages}
        self.ticks = 0  # captures taken so far, so a page can show something that moves on its own
        self._refs: dict[str, Ref] = {}  # action -> the element, so a ref stays the same object
        self._fields: dict[str, Ref] = {}  # label -> the field's element

    # ----- the world's own state ------------------------------------------------------------

    @property
    def loading_now(self) -> bool:
        return self.loading[self.page.name] > 0

    def cells(self) -> list[Cell]:
        """Every item of the current page in reading order. A page still loading shows one line."""
        if self.loading_now:
            return [Cell(LOADING, "", 0, row_box(0))]
        items = self.page.items(self) if callable(self.page.items) else self.page.items
        out = []
        for n, entry in enumerate(items):
            columns = entry if isinstance(entry, list) else [entry]
            for k, cell in enumerate(columns):
                text, role = (cell, "") if isinstance(cell, str) else cell
                out.append(Cell(text, role, n, cell_box(n, k, len(columns))))
        return out

    def click_action(self, cell: Cell, cells: list[Cell]) -> str:
        """The action clicking this cell applies: by text, or by row when the text is not unique."""
        repeated = sum(1 for other in cells if other.text == cell.text) > 1
        return f"click:{cell.text}@{cell.row}" if repeated else f"click:{cell.text}"

    def apply(self, action: str, label: str | None = None, append: bool = False) -> None:
        """Receive one action: record it, then follow the current page's transition for it.

        A page that is still loading answers nothing but `wait`: the tick is spent, the action is
        dropped. That is what a real app does to a click on a spinner.
        """
        self.log.append(action)
        if self.loading_now:
            if action == "wait":
                self.loading[self.page.name] -= 1
            return
        if action.startswith("type:"):
            key = label or self.page.field or ""
            text = action[len("type:") :]
            # Keystrokes land after whatever the field already holds; setting a value replaces it.
            self.typed[key] = self.typed.get(key, "") + text if append else text
        nxt = self.page.on.get(action)
        if callable(nxt):
            nxt = nxt(self)
        if nxt is not None:
            self.page = self.pages[nxt]

    def _ref(self, action: str) -> Ref:
        """The element that applies `action` when pressed, the same object every capture."""
        return self._refs.setdefault(action, Ref(action))

    def _field_ref(self, label: str) -> Ref:
        return self._fields.setdefault(label, Ref(label))

    def _label(self, ref: object) -> str | None:
        """The field this ref stands for, or None when it is not one of this world's fields."""
        return ref.name if isinstance(ref, Ref) and self._fields.get(ref.name) is ref else None

    # ----- the screen ----------------------------------------------------------------------

    def capture(self) -> Screen:
        """What `perception.capture` would return for the page now showing.

        Each capture is a tick, so a page whose rows are a callable can move between steps without
        moving inside one: every other read of the screen in the same step sees the same rows.
        """
        if self.between is not None:
            self.between(self)
        nodes = [
            AxNode(role="AXLink", label=lbl, x=0.0, y=OFFSCREEN_Y, w=120.0, h=32.0, pressable=True, ref=self._ref(f"press:{lbl}"))
            for lbl in self.page.offscreen
        ]
        menu = []
        for entry in self.page.menu:
            path, chord = (entry, None) if isinstance(entry, str) else entry
            menu.append(MenuItem(path=path, chord=chord, ref=self._ref(f"menu:{path}")))
        windows = [
            WindowRef(title=title, main=i == 0, ref=self._ref(f"window:{title}")) for i, title in enumerate(self.page.windows)
        ]
        screen = Screen(
            image=Image.new("RGB", CAPTURE),
            scale=SCALE,
            app=self.page.app,
            field=self.focused_field(),
            url=self.page.url,
            pid=1,
            window=None,
            offscreen=nodes,
            menu=menu,
            windows=windows,
            running=frozenset(self.running),
        )
        self.ticks += 1
        return screen

    def perceive(self, screen: Screen) -> list[Item]:
        """The rows as items, filling `screen.ax_refs` for the ones the app declared."""
        screen.ax_refs.clear()
        items = []
        cells = self.cells()
        for n, cell in enumerate(cells):
            items.append(Item(n, cell.text, 1.0, *cell.box, role=cell.role, source="ax" if cell.role else "ocr"))
            if cell.role:
                screen.ax_refs[n] = self._ref(self.click_action(cell, cells))
        return items

    def focused_field(self) -> Field | None:
        """The page's text field, at the row that carries its label.

        A field is placed by the row that reads as its label, so a page that names a field it does
        not show is a mistake in the scenario rather than a case the world has to invent a box for.
        """
        label = self.page.field
        if label is None:
            return None
        labelled = next((c for c in self.cells() if c.text == label), None)
        if labelled is None:
            raise AssertionError(f"page {self.page.name!r} has a field {label!r} but no row reads it")
        x1, top, x2, _ = labelled.box
        return Field(
            role="AXTextField",
            label=label,
            placeholder="",
            value=self.typed.get(label, ""),
            x=x1 / SCALE,
            y=top / SCALE,
            w=(x2 - x1) / SCALE,
            h=ROW_TEXT_HEIGHT / SCALE,
            ref=self._field_ref(label),
            secure=self.page.secure,
        )

    # ----- the machine ---------------------------------------------------------------------

    def click_at(self, point: tuple[float, float], clicks: int = 1, right: bool = False) -> None:
        """A synthetic click, in screen points: find the cell it lands on and press that item.

        A page with a `covered_by` overlay takes every mouse click itself, wherever it was aimed:
        that is a cookie banner over the content. A press through accessibility still reaches the
        element under it, which is the whole point of pressing rather than clicking. A double or a
        right click reads as the plain click's action with "double-" or "right-" in front.
        """
        self.mouse.append(point)
        gesture = "right-" if right else "double-" if clicks == 2 else ""
        if self.page.covered_by is not None:
            self.apply(f"{gesture}click:{self.page.covered_by}")
            return
        px, py = point[0] * SCALE, point[1] * SCALE
        cells = self.cells()
        hit = next((c for c in cells if c.box[0] <= px <= c.box[2] and c.box[1] <= py <= c.box[3]), None)
        self.apply(gesture + (self.click_action(hit, cells) if hit is not None else "click:nothing"))

    def ax_press(self, ref: object) -> bool:
        if not isinstance(ref, Ref) or self._refs.get(ref.name) is not ref:
            return False
        self.apply(ref.name)
        return True

    def press(self, name: str, command: bool = False, shift: bool = False) -> None:
        if command and name == "[":
            self.apply("back")
            return
        if command or shift:
            self.apply(f"{'shift+' if shift else ''}{'cmd+' if command else ''}{name}")
            return
        self.apply("enter" if name == "return" else name)

    def raise_window(self, ref: object) -> bool:
        if not isinstance(ref, Ref) or self._refs.get(ref.name) is not ref:
            return False
        self.apply(ref.name)
        return True

    def scroll(self, lines: int) -> None:
        self.apply("scroll_down" if lines < 0 else "scroll_up")

    def type_text(self, text: str) -> None:
        self.apply(f"type:{text}", append=True)

    def ax_set_value(self, ref: object, text: str) -> bool:
        """Set the field's value, unless the page is one of those that quietly refuse to take one."""
        if self.page.no_ax_value:
            return False
        self.apply(f"type:{text}", label=self._label(ref))
        return True

    def ax_value(self, ref: object) -> str | None:
        return self.typed.get(self._label(ref) or "", "")

    def clear_field(self) -> None:
        self.apply("clear_field")
        self.typed.pop(self.page.field or "", None)

    def activate(self, app: str) -> bool:
        """Bring an app up. A page that says where that app leads names it; otherwise it is the browser coming forward."""
        self.apply(f"activate:{app}" if f"activate:{app}" in self.page.on else "activate")
        self.running.add(app)
        return True

    def open_url(self, browser: str, url: str) -> bool:
        self.apply(f"open:{url}")
        return True

    # ----- wiring --------------------------------------------------------------------------

    def install(self, monkeypatch) -> None:
        """Replace the two outer seams: the screen the loop reads and the machine it drives."""
        monkeypatch.setattr(runner, "capture", lambda *a, **k: self.capture())
        monkeypatch.setattr(runner, "perceive", lambda screen, *a, **k: self.perceive(screen))
        monkeypatch.setattr(desktop, "check_abort", lambda: None)
        monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: None)
        monkeypatch.setattr(desktop, "click_at", self.click_at)
        monkeypatch.setattr(desktop, "ax_press", self.ax_press)
        monkeypatch.setattr(desktop, "press", self.press)
        monkeypatch.setattr(desktop, "scroll", self.scroll)
        monkeypatch.setattr(desktop, "type_text", self.type_text)
        monkeypatch.setattr(desktop, "ax_focus", lambda ref: True)
        monkeypatch.setattr(desktop, "ax_set_value", self.ax_set_value)
        monkeypatch.setattr(desktop, "ax_value", self.ax_value)
        monkeypatch.setattr(desktop, "clear_field", self.clear_field)
        monkeypatch.setattr(desktop, "focused_field", self.focused_field)
        monkeypatch.setattr(desktop, "activate", self.activate)
        monkeypatch.setattr(desktop, "open_url", self.open_url)
        monkeypatch.setattr(desktop, "raise_window", self.raise_window)
        # `wait` is the one action that touches no machine call, so the only way the world hears
        # about it is the handler itself. Without this a loading page would never finish loading.
        monkeypatch.setitem(actions._HANDLERS, "wait", lambda decision, screen, items, ctx: (self.apply("wait"), "waited")[1])


DEFAULT_CONFIDENCE = 0.9  # comfortably over the runner's 0.4 floor, so a scenario stops for a reason


class FakeTypeSafe:
    """The classifier, replaced by a policy over the state the real `decide` builds.

    A policy returns (kind, target) and this maps the target to whatever key the question wants:
    the item's index for click_item, the off-screen control's key for press_offscreen, the site key
    for use_browser. So a scenario never writes an index, and renumbering the page cannot break it.

    Every decision state lands in `states`, so a test can assert what the model was shown. The
    `verify_typed` Noul that `actions._type_text` makes is a different question about a different
    state, so it is answered without being recorded and `states` stays one entry per step.
    """

    def __init__(self, policy: Policy, noul: float = 0.95):
        self.policy = policy
        self.noul = noul
        self.states: list[dict] = []
        self.asked: list[dict] = []  # the questions each decision was asked, so a test can read the criteria offered

    def __enter__(self) -> FakeTypeSafe:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def system_one(self, state: dict, questions: dict) -> SimpleNamespace:
        if any(isinstance(q, Noul) for q in questions.values()):
            return SimpleNamespace(answers={name: SimpleNamespace(noul=self.noul) for name in questions})
        self.states.append(state)
        self.asked.append(questions)
        step = self.policy(state, questions)
        kind, target = step[0], step[1]
        confidence = step[2] if len(step) > 2 else DEFAULT_CONFIDENCE
        return SimpleNamespace(answers=self._answers(state, questions, kind, target, confidence))

    def _answers(self, state: dict, questions: dict, kind: str, target, confidence: float) -> dict:
        answers = {"kind": _answer(kind, confidence), "site": _answer(target if kind == "use_browser" else "none", confidence)}
        if "item" in questions:
            answers["item"] = _answer(self._item_key(state, target) if kind in CLICK_KINDS else "0", confidence)
        if "offscreen" in questions:
            key = self._offscreen_key(state, target) if kind == "press_offscreen" else "0"
            answers["offscreen"] = _answer(key, confidence)
        # A key, menu, window or app is named by what it reads as: a key's name, a menu path, a window
        # title, an app. Whatever the kind does not use gets the first thing on offer.
        for name, owner in (("key", "press_key"), ("menu", "press_menu"), ("window", "focus_window"), ("app", "open_app")):
            if name in questions:
                criteria = questions[name].criteria
                key = self._target_key(criteria, target) if kind == owner else next(iter(criteria))
                answers[name] = _answer(key, confidence)
        return answers

    @staticmethod
    def _target_key(criteria: dict, target: str) -> str:
        """The key the policy's target stands for: itself, or the key whose criterion reads as it."""
        if target in criteria:
            return target
        for key, text in criteria.items():
            if text in (target, repr(target)):
                return key
        raise AssertionError(f"{target!r} is not on offer: {list(criteria.values())}")

    @staticmethod
    def _item_key(state: dict, text: str | int) -> str:
        """The item's key: a policy names it by text, or by index when several items read the same."""
        if isinstance(text, int):
            return str(text)
        for it in state["screen_items_in_reading_order"]:
            if it["text"] == text:
                return str(it["i"])
        raise AssertionError(
            f"no item reads {text!r}: the screen shows {[it['text'] for it in state['screen_items_in_reading_order']]}"
        )

    @staticmethod
    def _offscreen_key(state: dict, label: str) -> str:
        for node in state.get("offscreen_controls", []):
            if node["label"] == label:
                return str(node["k"])
        raise AssertionError(f"no off-screen control is labelled {label!r}")


def _answer(choice: str, confidence: float) -> SimpleNamespace:
    """One ChoiceAnswer. The distribution names only real keys, since the runner logs them by key."""
    return SimpleNamespace(choice=choice, confidence=confidence, probabilities={choice: confidence})


def scripted(*steps: Step) -> Policy:
    """A policy that replays the steps in order, one per decision, and says `done` once spent."""
    taken = []

    def policy(state: dict, questions: dict) -> Step:
        taken.append(state)
        return steps[len(taken) - 1] if len(taken) <= len(steps) else ("done", None)

    return policy


class FakeWriter:
    """Stands in for the Anthropic client, answering by which properties the request asks for.

    The three writer calls are told apart by their schemas, exactly as `writer.py` builds them:
    a field fill, a proposed URL, and the answer. The answer is the text of the screen the
    run stopped on, plus the text of any earlier screens the packet carries, so a scenario can
    assert through the answer both where the run ended and what it read on the way.

    `reviews` scripts what the writer makes of each stop, one entry per answer asked for: a dict
    of the reply's fields, or a callable(packet) returning one. {"focus": ...} sends the classifier
    back, {"question": ...} asks the user, and either says the goal is not reached yet. Once the
    script is spent, every stop is the goal achieved, as it is with no script at all.
    """

    def __init__(self, text: str = "", url: str = "", reviews: list | None = None):
        self.requests: list[dict] = []
        self.text = text
        self.url = url
        self.reviews = list(reviews or [])
        self.packets: list[dict] = []  # the packet of every answer asked for, in order
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        asked = set(request["output_config"]["format"]["schema"]["properties"])
        packet = json.loads(request["messages"][0]["content"][-1]["text"])
        if asked == {"fill", "text", "reason"}:
            reply = {"fill": bool(self.text), "text": self.text, "reason": "the goal names what to type"}
        elif asked == {"ok", "url", "reason"}:
            reply = {"ok": bool(self.url), "url": self.url, "reason": "the goal names the site"}
        elif asked == {"achieved", "answer", "focus", "question"}:
            self.packets.append(packet)
            earlier = [text for screen in packet.get("earlier_screens", []) for text in screen["text"]]
            scripted = self.reviews.pop(0) if self.reviews else {}
            scripted = scripted(packet) if callable(scripted) else scripted
            reply = {
                "achieved": not scripted,
                "answer": " ".join(packet["screen_text_in_reading_order"] + earlier),
                "focus": "",
                "question": "",
                **scripted,
            }
        else:
            raise AssertionError(f"the writer was asked for {sorted(asked)}")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))])


def drive(
    world: World,
    policy: Policy,
    goal: str = "do the thing",
    steps: int = 20,
    monkeypatch=None,
    tmp_path=None,
    writer: FakeWriter | None = None,
    email: str | None = None,
    noul: float = 0.95,
    replies: list[str] | None = None,
    handoffs: int | None = None,
    apps: tuple[str, ...] = (),
    live: LiveGoal | None = None,
) -> RunState:
    """Run the real loop against the world until it stops itself. `world.fake` holds the classifier.

    `replies` are what the user types when the writer asks, in order; with none, nobody is at the
    terminal and the writer is told so. `world.asked` collects the questions that were put.
    """
    world.install(monkeypatch)
    fake = FakeTypeSafe(policy, noul)
    world.fake = fake
    monkeypatch.setattr(runner, "TypeSafeClient", lambda: fake)
    cfg = RunConfig(goal=goal, out=tmp_path / "run", act=True, steps=steps, delay=0)
    if handoffs is not None:
        cfg.handoffs = handoffs
    client = writer or FakeWriter()
    left = list(replies or [])

    def ask(question: str) -> str:
        world.asked.append(question)
        return left.pop(0) if left else ""

    return run(
        cfg,
        lambda typesafe, history: Context(
            goal=goal,
            browser="Google Chrome",
            email=email,
            typesafe=typesafe,
            writer=client,
            history=history,
            ask=ask if replies is not None else None,
            apps=apps,
        ),
        live=live,
    )
