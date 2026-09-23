"""The TypeSafe side: state, criteria, and the one multi-Choice request."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from typesafe_sdk import Choice, ChoiceAnswer, Noul, TypeSafeClient

from .apps import same_app
from .config import CLIPBOARD_CHARS, KEY_ACTIONS, MAX_OPTIONS, SITES
from .dates import date_hints, now_context
from .models import AxNode, Field, Guidance, Item, MenuItem, Screen, WindowRef
from .writer import credential_field

STOP_KINDS = ("done", "none")
CLICK_KINDS = ("click_item", "double_click_item", "right_click_item")  # each takes its target from the item question
OFFSCREEN_PREFIX = "offscreen:"
DOUBLE_PREFIX = "double:"
RIGHT_PREFIX = "right:"
PRESS_OFFSCREEN = (
    "Activate a labelled control that the app exposes but that is not currently visible on screen "
    "(chosen in the offscreen question). Use when the needed control is known to exist but is "
    "scrolled out of view or not yet shown."
)

OPEN_APP = (
    "Open another application, or switch to it when it is already running (chosen in the app question). "
    "This is the only way to reach an app that is not on screen: nothing on screen points at it. For a "
    "website, use use_browser instead."
)
PRESS_MENU = (
    "Run a command from the frontmost app's menu bar (chosen in the menu question). The menu holds what "
    "the app can do, commands with no button on screen among them, and runs without being opened."
)
FOCUS_WINDOW = (
    "Bring another of the frontmost app's own windows to the front (chosen in the window question). Use it "
    "when the work belongs in a window of this app that is behind the one in front."
)
PRESS_KEY = (
    "Press a keyboard shortcut (chosen in the key question): save, undo, copy, paste, find, and the arrows "
    "among them. Use it for what a key does better than a click, and undo when the last action went wrong."
)
DOUBLE_CLICK = (
    "Double-click one of the on-screen items (chosen in the item question): to open a file or folder from a "
    "list, or to select a word."
)
RIGHT_CLICK = (
    "Right-click one of the on-screen items (chosen in the item question) to open its context menu, whose "
    "entries are items on the next step."
)
# Never offered from a menu, whatever the app: they fill in saved credentials, a path that types a password.
MENU_WITHHELD = ("autofill", "password", "passkey")

# Said only while a focus is set, so a run the writer never steered asks the question it always asked.
FOCUS_RULE = (
    " The current focus is the next step on the way to the goal, set by a reviewer that read the "
    "screen when you last stopped: work toward it. 'done' still means the goal itself, not the focus."
)


def describe_field(field: Field) -> str:
    """The focused field in words, so a typing action says what it would type into."""
    name = field.label.strip() or field.placeholder.strip()
    return f"the focused text field, {name!r}" if name else "the focused text field"


def typable(field: Field | None) -> bool:
    """Whether typing could run this step: a text field has the focus, and it asks for no credential."""
    return field is not None and field.is_text and not credential_field(field)


def fixed_actions(browser: str, email: str | None, field: Field | None = None) -> dict[str, str]:
    """Deterministic actions offered alongside click_item. Keep them mutually exclusive.

    Typing is offered only when it could run. An action that cannot is worse than one that is
    missing: it takes probability with it, and the confidence left over may fall under the floor
    and stop a run that had a good move. Measured on a live run, a type_text offered with no field
    focused held the winning click to 0.38; without it the same step picked the click at 0.73.
    """
    actions = {
        "use_browser": (
            f"Work in {browser}: bring it to the front, and open a website there if one is needed. The "
            "site question says which website, or says that the page already open there is the one to "
            "continue with. This is the only way to reach a website: never click the address bar, a URL, "
            "or a search box to get there. Works from any app, including this one."
        ),
        "press_enter": "Press Return to submit the focused form or field.",
        "press_escape": "Press Escape to dismiss a dialog, menu, or popup.",
        "go_back": (
            "Go back to the previous page or screen, as the browser's Back button does. Use when the last "
            "click led somewhere that does not help and the page before it did."
        ),
        "scroll_down": "Scroll down to reveal more of the page.",
        "scroll_up": "Scroll up.",
        "wait": "Nothing to do yet; the screen is still loading or changing.",
        "done": "The goal is already achieved.",
        "none": "Nothing on screen or in this list helps with the goal.",
    }
    if typable(field):
        where = describe_field(field)
        actions["type_text"] = (
            f"Type free text into {where}. A writing model composes the text from the goal and the field's label."
        )
        if email:
            actions["type_email"] = (
                f"Type the user's email address into {where}. Use this, not type_text, whenever the field "
                "wants an email or username."
            )
    return actions


def kind_criteria(
    browser: str,
    email: str | None,
    offscreen: bool = False,
    field: Field | None = None,
    items: bool = True,
    apps: bool = False,
    menu: bool = False,
    windows: bool = False,
) -> dict[str, str]:
    """Every action on offer this step. One that has nothing to act on is not offered at all."""
    clicks: dict[str, str] = {}
    if items:
        clicks["click_item"] = "Click one of the on-screen text items (chosen in the item question)."
        clicks["double_click_item"] = DOUBLE_CLICK
        clicks["right_click_item"] = RIGHT_CLICK
    if offscreen:
        clicks["press_offscreen"] = PRESS_OFFSCREEN
    if apps:
        clicks["open_app"] = OPEN_APP
    if menu:
        clicks["press_menu"] = PRESS_MENU
    if windows:
        clicks["focus_window"] = FOCUS_WINDOW
    return {**clicks, "press_key": PRESS_KEY, **fixed_actions(browser, email, field)}


def key_criteria(field: Field | None, clipboard_shared: bool = False) -> dict[str, str]:
    """The named keys that could do something now. Paste never reaches a credential field, and
    backspace needs a field that could be typed into."""
    keys = {name: action.description for name, action in KEY_ACTIONS.items()}
    if field is not None and credential_field(field):
        del keys["paste"]
    if not typable(field):
        del keys["backspace"]
    if clipboard_shared:
        keys["copy"] += " The clipboard's text is in the state on the next step, which is how text is carried between apps."
    return keys


# The chords a named key or a fixed action already sends. A menu command behind one of them is the
# same move offered twice, which splits the vote, so the menu leaves it to the key.
TAKEN_CHORDS = {(action.key, action.command, action.shift) for action in KEY_ACTIONS.values() if len(action.key) == 1} | {
    ("[", True, False)  # go_back
}


def menu_offered(screen: Screen) -> list[tuple[int, MenuItem]]:
    """The menu commands worth offering, with their position in `screen.menu`: not one a named key
    already runs, never one that fills in a saved credential, and no paste into a credential field."""
    credential = screen.field is not None and credential_field(screen.field)
    out = []
    for i, item in enumerate(screen.menu):
        title = item.path.casefold()
        if item.chord in TAKEN_CHORDS or any(word in title for word in MENU_WITHHELD):
            continue
        if credential and "paste" in title:
            continue
        out.append((i, item))
    return out


def menu_criteria(screen: Screen) -> dict[str, str]:
    """Each command as its menu path, keyed by position, since two menus can share an item name."""
    return {str(i): item.path for i, item in menu_offered(screen)}


def windows_offered(screen: Screen) -> list[tuple[int, WindowRef]]:
    """The app's other windows, with their position: the one already in front is nothing to switch to."""
    return [(i, window) for i, window in enumerate(screen.windows) if not window.main]


def window_criteria(screen: Screen) -> dict[str, str]:
    return {str(i): repr(window.title) for i, window in windows_offered(screen)}


def apps_offered(apps: Sequence[str], screen: Screen, browser: str) -> list[str]:
    """The apps open_app could bring up now, running ones first, so a Mac with more apps than a
    Choice holds keeps the likeliest. Not the browser, which is use_browser's, and not the app
    already in front, which choosing would leave exactly as it is."""
    candidates = [name for name in apps if not same_app(name, browser) and not same_app(screen.app, name)]
    running = [name for name in candidates if name in screen.running]
    return [*running, *(name for name in candidates if name not in screen.running)][:MAX_OPTIONS]


def app_criteria(apps: Sequence[str], screen: Screen, browser: str) -> dict[str, str]:
    """Each app, saying what choosing it would do."""
    return {
        name: f"{name}. "
        + (
            "Already running: this switches to it, its windows as they were left."
            if name in screen.running
            else "Not running: this launches it."
        )
        for name in apps_offered(apps, screen, browser)
    }


ROW_MATES = 3  # how many neighbours name a duplicated item's row in a criterion; the history line takes them all


def row_mates(items: list[Item], limit: int | None = ROW_MATES) -> dict[int, list[str]]:
    """Item index -> the texts sharing its row, left to right, for every item whose text another item repeats.

    Three rows of events each end in a 'Buy'. The label says nothing about which; the row does,
    and the row is a fact the layout holds, so the code reads it and hands it over. `limit`
    keeps a criterion short; None takes the whole row, for a line that has to identify it.
    """
    counts = Counter(it.text for it in items)
    out: dict[int, list[str]] = {}
    for it in items:
        if counts[it.text] < 2:
            continue
        cy, half = it.center[1], max(1.0, it.y2 - it.y1) / 2
        mates = sorted((o for o in items if o is not it and abs(o.center[1] - cy) < half), key=lambda o: o.x1)
        if mates:
            out[it.index] = [o.text for o in mates[:limit]]
    return out


def item_criteria(screen: Screen, items: list[Item]) -> dict[str, str]:
    """Each item as one line. A role prefix marks the ones the app itself declared, and a
    duplicated label carries its row."""
    hints = date_hints(items, screen)
    mates = row_mates(items)
    return {
        str(it.index): (
            f"{it.role + ' ' if it.from_ax and it.role else ''}{it.text!r} "
            f"({screen.region(it)}"
            f"{'; ' + hints[it.index] if it.index in hints else ''}"
            f"{'; in the row of ' + ', '.join(repr(t) for t in mates[it.index]) if it.index in mates else ''})"
        )
        for it in items
    }


def offscreen_criteria(nodes: list[AxNode]) -> dict[str, str]:
    """Each off-screen control as one line, keyed by its position in `screen.offscreen`."""
    return {str(i): f"{node.role_word} {node.label!r} (not visible)" for i, node in enumerate(nodes)}


def offscreen_records(nodes: list[AxNode]) -> list[dict]:
    """The same controls as state, with the key the offscreen question answers with."""
    return [{"k": i, "role": node.role_word, "label": node.label} for i, node in enumerate(nodes)]


def site_criteria() -> dict[str, str]:
    """Which website use_browser opens. The catalog, plus one key for anything else and one for nothing."""
    return {
        **SITES,
        "other": "A website is needed to progress the goal, but it is not one of the sites named in this list.",
        "none": "No website needs to be opened: the page already open in the browser is the one to continue with.",
    }


def target_criteria(screen: Screen, browser: str, apps: Sequence[str] = (), clipboard_shared: bool = False) -> dict[str, dict]:
    """The criteria of the questions that name what a key, menu, window or app action acts on, for
    the ones that have anything to offer this step. The key question always has."""
    out = {
        "key": key_criteria(screen.field, clipboard_shared),
        "menu": menu_criteria(screen),
        "window": window_criteria(screen),
        "app": app_criteria(apps, screen, browser),
    }
    return {name: criteria for name, criteria in out.items() if criteria}


def screen_kind_criteria(
    browser: str, email: str | None, screen: Screen, items: list[Item], targets: dict[str, dict]
) -> dict[str, str]:
    """kind_criteria for this screen: every kind whose target is there to act on."""
    return kind_criteria(
        browser,
        email,
        bool(screen.offscreen),
        screen.field,
        items=bool(items),
        apps="app" in targets,
        menu="menu" in targets,
        windows="window" in targets,
    )


TARGET_INSTRUCTIONS = {
    "key": "If pressing a keyboard shortcut is the right move, which one? Undo, when the last action made things worse.",
    "menu": (
        "If running a command from the app's menu bar is the right move, which command? These are the frontmost "
        "app's commands, by their menu path."
    ),
    "window": "If another window of this app is where the work belongs, which window?",
    "app": (
        "If opening or switching to another application is the right move, which one? Prefer one already running "
        "when it suits the goal, since its windows are as the user left them."
    ),
}


def base_state(
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    tried: list[str] | None = None,
    guidance: Guidance | None = None,
) -> dict:
    """The facts the classifier reads. `tried` lists the actions already taken on this same screen
    earlier in the run, each of which led back here: a fact the code knows and the model cannot.
    `guidance` is what the writer and the user added to the goal when the classifier last stopped."""
    hints = date_hints(items, screen)
    mates = row_mates(items)
    return {
        "goal": goal,
        **(guidance.state() if guidance else {}),
        "now": now_context(),
        "frontmost_app": screen.app,
        "browser_active_tab_url": screen.url,
        "focused_field": screen.field.summary() if screen.field else None,
        "previous_actions": history[-8:],
        "already_tried_on_this_screen": list(tried or []),
        "screen_items_in_reading_order": [
            {
                "i": it.index,
                "text": it.text,
                "where": screen.region(it),
                **({"role": it.role} if it.role else {}),
                **({"when": hints[it.index]} if it.index in hints else {}),
                **({"beside": mates[it.index]} if it.index in mates else {}),
            }
            for it in items
        ],
        **({"offscreen_controls": offscreen_records(screen.offscreen)} if screen.offscreen else {}),
        **({"clipboard": screen.clipboard[:CLIPBOARD_CHARS]} if screen.clipboard else {}),
        **({"other_windows_of_this_app": [w.title for _, w in windows_offered(screen)]} if windows_offered(screen) else {}),
    }


@dataclass(frozen=True)
class Decision:
    kind: ChoiceAnswer
    item: ChoiceAnswer | None
    site: ChoiceAnswer
    offscreen: ChoiceAnswer | None = None
    app: ChoiceAnswer | None = None
    key: ChoiceAnswer | None = None
    menu: ChoiceAnswer | None = None
    window: ChoiceAnswer | None = None

    @property
    def clicking(self) -> bool:
        return self.kind.choice in CLICK_KINDS and self.item is not None

    @property
    def target(self) -> ChoiceAnswer | None:
        """The answer that names what a key, menu or window action acts on, when this is one."""
        return {"press_key": self.key, "press_menu": self.menu, "focus_window": self.window}.get(self.kind.choice)

    @property
    def pressing_offscreen(self) -> bool:
        return self.kind.choice == "press_offscreen" and self.offscreen is not None

    @property
    def chosen(self) -> str:
        if self.clicking:
            prefix = {"double_click_item": DOUBLE_PREFIX, "right_click_item": RIGHT_PREFIX}.get(self.kind.choice, "")
            return f"{prefix}{self.item.choice}"
        if self.pressing_offscreen:
            return f"{OFFSCREEN_PREFIX}{self.offscreen.choice}"
        return self.kind.choice

    @property
    def confidence(self) -> float:
        # Only the answers that name a target lower the confidence: a click or a press lands
        # somewhere, and the wrong somewhere is not undone. use_browser reads the site answer too,
        # but every outcome of it is a page the next step can leave, so a split there must not
        # stop the run. open_app is the same: the wrong app is left by opening another, and over a
        # hundred and fifty of them the mass spreads so thin that gating on it would stop nearly
        # every run that wanted one. A key, a menu command and a window each do something to what
        # is in front, so they gate like a click.
        if self.clicking:
            return min(self.kind.confidence, self.item.confidence)
        if self.pressing_offscreen:
            return min(self.kind.confidence, self.offscreen.confidence)
        if self.target is not None:
            return min(self.kind.confidence, self.target.confidence)
        return self.kind.confidence

    @property
    def stops(self) -> bool:
        return self.kind.choice in STOP_KINDS


def decide(
    client: TypeSafeClient,
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    browser: str,
    email: str | None,
    tried: list[str] | None = None,
    guidance: Guidance | None = None,
    apps: Sequence[str] = (),
    clipboard_shared: bool = False,
) -> Decision:
    targets = target_criteria(screen, browser, apps, clipboard_shared)
    questions = {
        "kind": Choice(
            instructions=(
                "You are driving this computer one action at a time. Which kind of action "
                "makes the most progress toward the goal right now? Do not repeat an action "
                "that was just taken unless the screen changed, and never one listed as already "
                "tried on this screen: each of those led straight back here."
                + (FOCUS_RULE if guidance and guidance.focus else "")
            ),
            criteria=screen_kind_criteria(browser, email, screen, items, targets),
        ),
        "site": Choice(
            instructions=(
                "If the browser is used this step, which website should it show? Name a site from the "
                "list when the goal calls for that one, 'other' when the goal calls for a site the list "
                "does not name, and 'none' to stay on the page that is already open in the browser."
            ),
            criteria=site_criteria(),
        ),
    }
    if items:
        questions["item"] = Choice(
            instructions=(
                "If clicking an on-screen item is the right move, which item? Items marked with a "
                "role come from the app's accessibility tree and are real controls; plain items are "
                "text read from the screen."
            ),
            criteria=item_criteria(screen, items),
        )
    if screen.offscreen:
        questions["offscreen"] = Choice(
            instructions=(
                "If activating a control that is not on screen is the right move, which control? "
                "These are real controls of the app, reachable without the mouse, but nothing on "
                "the capture points at them."
            ),
            criteria=offscreen_criteria(screen.offscreen),
        )
    for name, criteria in targets.items():
        questions[name] = Choice(instructions=TARGET_INSTRUCTIONS[name], criteria=criteria)
    answers = client.system_one(state=base_state(goal, screen, items, history, tried, guidance), questions=questions).answers
    return Decision(
        kind=answers["kind"],
        item=answers.get("item"),
        site=answers["site"],
        offscreen=answers.get("offscreen"),
        app=answers.get("app"),
        key=answers.get("key"),
        menu=answers.get("menu"),
        window=answers.get("window"),
    )


def verify_typed(client: TypeSafeClient, goal: str, field_before: Field, typed: str, field_after: Field | None) -> float:
    """Probability that the field now holds a sensible value for its purpose."""
    state = {
        "goal": goal,
        "field": field_before.summary(),
        "text_typed": typed,
        "field_value_now": field_after.value[:300] if field_after else None,
        "field_still_focused": bool(
            field_after and field_after.role == field_before.role and field_after.label == field_before.label
        ),
    }
    question = Noul(
        instructions=(
            "Did the typing succeed: does the field now contain the typed text, and is that "
            "text a sensible value for what this field asks for, given the goal?"
        )
    )
    return client.system_one(state=state, questions={"ok": question}).answers["ok"].noul
