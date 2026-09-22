"""The TypeSafe side: state, criteria, and the one multi-Choice request."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from typesafe_sdk import Choice, ChoiceAnswer, Noul, TypeSafeClient

from .config import SITES
from .dates import date_hints, now_context
from .models import AxNode, Field, Guidance, Item, Screen

STOP_KINDS = ("done", "none")
OFFSCREEN_PREFIX = "offscreen:"
PRESS_OFFSCREEN = (
    "Activate a labelled control that the app exposes but that is not currently visible on screen "
    "(chosen in the offscreen question). Use when the needed control is known to exist but is "
    "scrolled out of view or not yet shown."
)

# Said only while a focus is set, so a run the writer never steered asks the question it always asked.
FOCUS_RULE = (
    " The current focus is the next step on the way to the goal, set by a reviewer that read the "
    "screen when you last stopped: work toward it. 'done' still means the goal itself, not the focus."
)


def fixed_actions(browser: str, email: str | None) -> dict[str, str]:
    """Deterministic actions offered alongside click_item. Keep them mutually exclusive."""
    actions = {
        "use_browser": (
            f"Work in {browser}: bring it to the front, and open a website there if one is needed. The "
            "site question says which website, or says that the page already open there is the one to "
            "continue with. This is the only way to reach a website: never click the address bar, a URL, "
            "or a search box to get there. Works from any app, including this one."
        ),
        "type_text": (
            "Type free text into the focused text field. A writing model composes the text from the "
            "goal and the field's label. Only valid when a text field is focused and needs content."
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
    if email:
        actions["type_email"] = (
            "Type the user's email address into the focused text field. Use this, not type_text, "
            "whenever the field wants an email or username."
        )
    return actions


def kind_criteria(browser: str, email: str | None, offscreen: bool = False) -> dict[str, str]:
    clicks = {"click_item": "Click one of the on-screen text items (chosen in the item question)."}
    if offscreen:
        clicks["press_offscreen"] = PRESS_OFFSCREEN
    return {**clicks, **fixed_actions(browser, email)}


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
    }


@dataclass(frozen=True)
class Decision:
    kind: ChoiceAnswer
    item: ChoiceAnswer | None
    site: ChoiceAnswer
    offscreen: ChoiceAnswer | None = None

    @property
    def clicking(self) -> bool:
        return self.kind.choice == "click_item" and self.item is not None

    @property
    def pressing_offscreen(self) -> bool:
        return self.kind.choice == "press_offscreen" and self.offscreen is not None

    @property
    def chosen(self) -> str:
        if self.clicking:
            return self.item.choice
        if self.pressing_offscreen:
            return f"{OFFSCREEN_PREFIX}{self.offscreen.choice}"
        return self.kind.choice

    @property
    def confidence(self) -> float:
        # Only the answers that name a target lower the confidence: a click or a press lands
        # somewhere, and the wrong somewhere is not undone. use_browser reads the site answer too,
        # but every outcome of it is a page the next step can leave, so a split there must not
        # stop the run.
        if self.clicking:
            return min(self.kind.confidence, self.item.confidence)
        if self.pressing_offscreen:
            return min(self.kind.confidence, self.offscreen.confidence)
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
) -> Decision:
    questions = {
        "kind": Choice(
            instructions=(
                "You are driving this computer one action at a time. Which kind of action "
                "makes the most progress toward the goal right now? Do not repeat an action "
                "that was just taken unless the screen changed, and never one listed as already "
                "tried on this screen: each of those led straight back here."
                + (FOCUS_RULE if guidance and guidance.focus else "")
            ),
            criteria=kind_criteria(browser, email, bool(screen.offscreen)),
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
    answers = client.system_one(state=base_state(goal, screen, items, history, tried, guidance), questions=questions).answers
    return Decision(kind=answers["kind"], item=answers.get("item"), site=answers["site"], offscreen=answers.get("offscreen"))


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
