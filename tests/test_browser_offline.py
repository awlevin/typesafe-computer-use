"""Offline tests for the browser backend. No Chrome, no network, no API key.

A stub session replays canned JS results, so the perception parsing, the action
set filtering, change detection and the step loop are all testable in CI.
"""

from __future__ import annotations

from typesafe_computer_use.browser import act
from typesafe_computer_use.browser.decide import available_actions
from typesafe_computer_use.browser.perceive import INTERACTIVE_JS, Element, perceive


def page_dict(*, items=(), scroll_y=0, can_scroll=True, history_len=1, fields=0):
    return {
        "url": "https://example.test/",
        "title": "Example",
        "vw": 1200,
        "vh": 800,
        "count": len(items),
        "items": list(items),
        "scroll_y": scroll_y,
        "scroll_max": 4000,
        "candidates": 900,
        "below_fold": 800,
        "can_scroll": can_scroll,
        "history_len": history_len,
        "fields": fields,
    }


def element_dict(index=0, name="Sign in", tag="a", **kwargs):
    base = {
        "index": index,
        "tag": tag,
        "role": "",
        "name": name,
        "x": 10,
        "y": 20,
        "w": 80,
        "h": 24,
        "in_view": True,
        "covered": False,
        "href": "",
    }
    base.update(kwargs)
    return base


class StubSession:
    """Replays a queue of JS results. Counts calls so tests can assert on traffic."""

    def __init__(self, results):
        self.results = list(results)
        self.evaluates = 0
        self.calls = []

    def evaluate(self, expression, **kwargs):
        assert expression is INTERACTIVE_JS or isinstance(expression, str)
        self.evaluates += 1
        return self.results.pop(0) if self.results else None

    def call(self, method, params=None):
        self.calls.append((method, params))
        return {}


# ------------------------------------------------------------------ parsing
def test_perceive_parses_page_facts():
    session = StubSession([page_dict(items=[element_dict(0), element_dict(1, "Search", "button")], fields=1)])
    page = perceive(session)
    assert [e.name for e in page.items] == ["Sign in", "Search"]
    assert page.field_count == 1 and page.has_field
    assert page.can_scroll and page.history_len == 1
    assert page.candidates == 900 and page.below_fold == 800


def test_perceive_honours_budget():
    items = [element_dict(i, f"item {i}") for i in range(50)]
    session = StubSession([page_dict(items=items)])
    assert len(perceive(session, budget=10).items) == 10


def test_element_label_carries_the_context_that_matters():
    e = Element(0, "a", "", "Docs", 5, 5, 40, 20, True, True, "https://x.test/docs")
    label = e.label()
    assert "<a>" in label and "'Docs'" in label
    assert "x.test/docs" in label
    assert "covered by an overlay" in label

    off = Element(1, "button", "", "More", 5, 900, 40, 20, False, False, "")
    assert "off-screen" in off.label()


def test_perceive_handles_active_element_absent():
    assert perceive(StubSession([None])).items == []


# ------------------------------------------------------- action availability
def test_type_text_not_offered_without_a_field():
    page = perceive(StubSession([page_dict(items=[element_dict()], fields=0)]))
    actions = available_actions(page, can_write=True)
    assert "type_text" not in actions
    assert "press_enter" not in actions
    assert "click" in actions


def test_free_text_actions_need_a_writer():
    """Typed text and an address to open only come from the writer, so without one neither
    action is offered: an action the caller cannot execute is a guaranteed stall."""
    page = perceive(StubSession([page_dict(items=[element_dict()], fields=1)]))
    without = available_actions(page, can_write=False)
    assert "type_text" not in without and "navigate" not in without
    with_writer = available_actions(page, can_write=True)
    assert "type_text" in with_writer and "navigate" in with_writer


def test_scroll_and_back_are_offered_only_when_they_can_do_something():
    flat_start = perceive(StubSession([page_dict(items=[element_dict()], can_scroll=False, history_len=1)]))
    actions = available_actions(flat_start)
    assert "scroll_down" not in actions and "scroll_up" not in actions
    assert "back" not in actions

    rich = perceive(StubSession([page_dict(items=[element_dict()], can_scroll=True, history_len=4)]))
    actions = available_actions(rich)
    assert "scroll_down" in actions and "back" in actions


def test_no_click_when_there_is_nothing_to_click():
    empty = perceive(StubSession([page_dict(items=[])]))
    assert "click" not in available_actions(empty)


def test_action_terminators_always_present():
    empty = perceive(StubSession([page_dict(items=[])]))
    actions = available_actions(empty)
    for key in ("done", "none", "wait"):
        assert key in actions


def test_option_count_stays_under_the_api_limit():
    items = [element_dict(i, f"link {i}") for i in range(120)]
    page = perceive(StubSession([page_dict(items=items, fields=1)]))
    assert len(page.items) + len(available_actions(page)) < 255


# ------------------------------------------------------------ change detection
def test_fingerprint_tracks_scroll_position():
    a = perceive(StubSession([page_dict(items=[element_dict()], scroll_y=0)]))
    b = perceive(StubSession([page_dict(items=[element_dict()], scroll_y=600)]))
    assert act.fingerprint(a) != act.fingerprint(b)


def test_observe_until_changed_returns_as_soon_as_the_page_moves():
    """First poll shows the new page, so the wait costs one round trip."""
    session = StubSession([page_dict(items=[element_dict(0, "after")])])
    fps = act.fingerprint(perceive(StubSession([page_dict(items=[element_dict(0, "before")])])))
    page, ms, changed = act.observe_until_changed(session, fps, timeout_ms=200, poll_ms=1)
    assert changed and page.items[0].name == "after"
    assert session.evaluates == 1
    assert ms < 200


def test_observe_until_changed_times_out_when_nothing_moves():
    same = page_dict(items=[element_dict(0, "same")])
    session = StubSession([page_dict(items=[element_dict(0, "same")]) for _ in range(80)])
    fps = act.fingerprint(perceive(StubSession([same])))
    _, ms, changed = act.observe_until_changed(session, fps, timeout_ms=60, poll_ms=5)
    assert not changed
    assert 55 <= ms < 400


def test_observe_with_no_baseline_just_perceives():
    session = StubSession([page_dict(items=[element_dict()])])
    page, _, changed = act.observe_until_changed(session, None)
    assert changed and page.items
