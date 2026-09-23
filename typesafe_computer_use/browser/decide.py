"""The TypeSafe side of the browser backend: one request, three questions.

Same discipline as the original — split the decision so screen noise cannot leak
into the action choice — but the option sets are browser-only, so they are
smaller and more mutually exclusive, which is what the original's author found
mattered most ("every stall came from two options that meant the same thing").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer, ScoreAnswer, TypeSafeClient

from .perceive import Page

STOP_KINDS = ("done", "none")
ENTER_KINDS = ("done", "none", "scroll_down", "scroll_up", "wait")

# Mutually exclusive by construction. No two options share a purpose.
BROWSER_ACTIONS: dict[str, str] = {
    "click": (
        "Click one of the on-screen elements. This is how you follow a link, press a button, "
        "open a menu, or select a tab. Choose the element in the element question."
    ),
    "type_text": (
        "Type free text into a text field — a search box, a form input, a username. Name the field "
        "in the element question. Only valid when a text field needs content."
    ),
    "navigate": (
        "Open the website the goal is about in this tab, by address. Not for clicking an on-screen link: use click for that."
    ),
    "press_enter": "Press Return to submit the form or field that currently has focus.",
    "press_escape": "Press Escape to dismiss a dialog, popup, or dropdown.",
    "scroll_down": "Scroll down to reveal content below the fold.",
    "scroll_up": "Scroll up.",
    "back": "Go back to the previous page in this tab's history.",
    "wait": "Nothing to do yet; the page is still loading or settling.",
    "done": "The goal is already achieved on this page.",
    "none": "Nothing on this page can make progress toward the goal.",
}


@dataclass(frozen=True)
class Decision:
    kind: ChoiceAnswer
    element: ChoiceAnswer | None
    satisfied: NoulAnswer
    # Everything needed to replay this step offline: the exact state and
    # criteria that were sent, and every probability that came back.
    state: dict = field(default_factory=dict)
    questions: dict = field(default_factory=dict)
    answers: dict = field(default_factory=dict)

    @property
    def clicking(self) -> bool:
        return self.kind.choice == "click" and self.element is not None

    @property
    def chosen_element(self) -> int | None:
        if self.element is None:
            return None
        try:
            return int(self.element.choice)
        except (TypeError, ValueError):
            return None

    @property
    def confidence(self) -> float:
        if self.clicking and self.element is not None:
            return min(self.kind.confidence, self.element.confidence)
        return self.kind.confidence

    @property
    def stops(self) -> bool:
        return self.kind.choice in STOP_KINDS


def element_criteria(page: Page) -> dict[str, str]:
    return {str(it.index): it.label() for it in page.items}


def available_actions(page: Page, *, allow_type: bool = True, can_write: bool = False) -> dict[str, str]:
    """Only offer actions the page can actually carry out.

    This is the one place the browser backend can beat the original's design:
    a screen has fixed affordances, so a text field either exists or it does not.
    Offering `type_text` on a page with no input guarantees a stall, and a
    stalled step reads as model doubt even though the model was never at fault.

    Typed text and a URL to open are free text, and free text only comes from the
    writer. With no writer (`can_write` false) neither action is offered.
    """
    actions: dict[str, str] = {}
    if page.items:
        actions["click"] = BROWSER_ACTIONS["click"]
    # An action the caller cannot execute is a guaranteed stall, and the model will
    # pick it at low confidence, which then reads as doubt.
    if allow_type and page.has_field and can_write:
        actions["type_text"] = BROWSER_ACTIONS["type_text"]
    if page.has_field:
        actions["press_enter"] = BROWSER_ACTIONS["press_enter"]
    if can_write:
        actions["navigate"] = BROWSER_ACTIONS["navigate"]
    actions["press_escape"] = BROWSER_ACTIONS["press_escape"]
    if page.can_scroll:
        actions["scroll_down"] = BROWSER_ACTIONS["scroll_down"]
        actions["scroll_up"] = BROWSER_ACTIONS["scroll_up"]
    if page.history_len > 1:
        actions["back"] = BROWSER_ACTIONS["back"]
    actions["wait"] = BROWSER_ACTIONS["wait"]
    actions["done"] = BROWSER_ACTIONS["done"]
    actions["none"] = BROWSER_ACTIONS["none"]
    return actions


def base_state(
    goal: str,
    page: Page,
    history: list[str],
    *,
    url_catalog: dict[str, str] | None,
) -> dict:
    return {
        "goal": goal,
        "page": {"url": page.url, "title": page.title, "viewport": f"{page.vw}x{page.vh}"},
        "previous_actions": history[-8:],
        "elements": [
            {
                "i": it.index,
                "tag": it.tag,
                "text": it.name,
                "href": it.href[:120] or None,
                "visible": it.in_view,
                "covered": it.covered,
                "credential_field": it.secret or None,
            }
            for it in page.items
        ],
        "known_sites": url_catalog or None,
    }


def decide(
    client: TypeSafeClient,
    goal: str,
    page: Page,
    history: list[str],
    *,
    url_catalog: dict[str, str] | None = None,
    allow_type: bool = True,
    can_write: bool = False,
    model: str | None = None,
) -> Decision:
    actions = available_actions(page, allow_type=allow_type, can_write=can_write)

    questions: dict[str, Any] = {
        "kind": Choice(
            instructions=(
                "You are driving a web browser one action at a time. Which single action makes the "
                "most progress toward the goal right now? Do not repeat the action just taken unless "
                "the page changed. If the goal is already achieved, choose done."
            ),
            criteria=actions,
        ),
        "satisfied": Noul(
            instructions="Is the goal already achieved on the page as it currently stands?",
            criteria={"true": "Yes, the goal is visibly complete", "false": "No, more work is needed"},
        ),
    }
    if page.items:
        questions["element"] = Choice(
            instructions=(
                "If the right next move is to click an element or type into a field, which element? "
                "Prefer an element that is on screen and not covered by an overlay."
            ),
            criteria=element_criteria(page),
        )

    state = base_state(goal, page, history, url_catalog=url_catalog)
    response = client.system_one(state=state, questions=questions, model=model)
    answers = response.answers

    element = answers.get("element")
    satisfied = answers["satisfied"]
    return Decision(
        kind=answers["kind"],
        element=element if isinstance(element, ChoiceAnswer) else None,
        satisfied=satisfied if isinstance(satisfied, NoulAnswer) else NoulAnswer(noul=0.0),
        state=state,
        questions=dict(questions),
        answers=serialize_answers(response.answers),
    )


def verify_typed(
    client: TypeSafeClient, goal: str, element_label: str, typed: str, value_now: str | None, *, model: str | None = None
) -> float:
    """Probability the field now holds a sensible value. Same guard as the original."""
    state = {
        "goal": goal,
        "field": element_label,
        "text_typed": typed,
        "field_value_now": (value_now or "")[:300],
    }
    question = Noul(
        instructions=(
            "Did the typing succeed: does the field now contain the typed text, and is that text a "
            "sensible value for what this field asks for, given the goal?"
        )
    )
    return float(client.system_one(state=state, questions={"ok": question}, model=model).answers["ok"].noul)


def field_context(page: Page, index: int, *, span: int = 6) -> list[str]:
    """Element text around a field, in reading order. The browser analogue of
    `Screen.near_field`, which the writer packet uses on macOS."""
    try:
        target = next(i for i, e in enumerate(page.items) if e.index == index)
    except StopIteration:
        return []
    lo = max(0, target - span)
    hi = min(len(page.items), target + span + 1)
    return [e.name for e in page.items[lo:hi] if e.index != index]


def serialize_answers(answers: dict) -> dict[str, dict]:
    """The SDK returns frozen msgspec structs with no `raw` payload.

    Rebuild the wire shape so a run folder holds exactly what the API returned,
    without depending on the SDK's internal decoding.
    """
    out: dict[str, dict] = {}
    for key, ans in (answers or {}).items():
        if isinstance(ans, ChoiceAnswer):
            out[key] = {
                "type": "choice",
                "choice": ans.choice,
                "probabilities": dict(ans.probabilities),
                "confidence": ans.confidence,
            }
        elif isinstance(ans, NoulAnswer):
            out[key] = {"type": "noul", "noul": ans.noul}
        elif isinstance(ans, ScoreAnswer):
            out[key] = {
                "type": "score",
                "score": ans.score,
                "confidence": ans.confidence,
                "legend": dict(getattr(ans, "legend", {}) or {}),
                "probabilities": dict(ans.probabilities),
            }
        else:
            out[key] = {"type": type(ans).__name__, "repr": repr(ans)}
    return out
