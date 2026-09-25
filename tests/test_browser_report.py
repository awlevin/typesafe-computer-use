"""Offline tests for the run folder, replay fidelity, and the writer contract.

No Chrome, no network, no API key: the writer is stubbed at the Anthropic
boundary, exactly where `_structured` calls it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from typesafe_computer_use.browser.decide import serialize_answers
from typesafe_computer_use.browser.perceive import Element, Page, TextBlock
from typesafe_computer_use.browser.report import RunFolder, load_step, render_answers, render_payload
from typesafe_computer_use.browser.runner import resolve_text
from typesafe_computer_use.writer import CREDENTIAL_HINTS, compose_browser_text, looks_credential


def make_page(**kwargs) -> Page:
    items = kwargs.pop(
        "items",
        [
            Element(0, "input", "search", "Search invoices", 10, 10, 200, 30, True, False, "", field=True),
            Element(1, "button", "submit", "Search", 220, 10, 80, 30, True, False, ""),
            Element(2, "a", "", "Quarterly Report Q4", 10, 60, 200, 20, True, False, ""),
        ],
    )
    base = dict(
        url="https://example.test/dash",
        title="Dashboard",
        vw=1200,
        vh=800,
        items=items,
        elapsed_ms=3.2,
        raw_count=len(items),
        can_scroll=True,
        history_len=2,
        field_count=1,
        scroll_y=0,
        candidates=90,
        below_fold=60,
    )
    base.update(kwargs)
    return Page(**base)


# ------------------------------------------------------------------ answers
def test_serialize_answers_rebuilds_the_wire_shape():
    payload = {
        "kind": {"type": "choice", "choice": "click", "probabilities": {"click": 0.7, "wait": 0.3}, "confidence": 0.7},
        "satisfied": {"type": "noul", "noul": 0.12},
        "severity": {
            "type": "score",
            "score": 1.5,
            "confidence": 0.8,
            "legend": {"0": "none", "1": "minor"},
            "probabilities": {"0": 0.5, "1": 0.5},
        },
    }
    from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer

    answers = {
        "kind": ChoiceAnswer(choice="click", probabilities={"click": 0.7, "wait": 0.3}, confidence=0.7),
        "satisfied": NoulAnswer(noul=0.12),
        "severity": ScoreAnswer(
            score=1.5, confidence=0.8, legend={"0": "none", "1": "minor"}, probabilities={"0": 0.5, "1": 0.5}
        ),
    }
    out = serialize_answers(answers)
    for key, expected in payload.items():
        assert out[key] == expected, key


def test_serialize_answers_does_not_crash_on_unexpected_types():
    out = serialize_answers({"odd": object()})
    assert out["odd"]["type"] == "object" and "repr" in out["odd"]


def test_render_answers_is_readable_and_ranks_probabilities():
    text = render_answers(
        {
            "kind": {
                "type": "choice",
                "choice": "click",
                "confidence": 0.52,
                "probabilities": {"click": 0.57, "press_enter": 0.42, "none": 0.01},
            },
            "satisfied": {"type": "noul", "noul": 0.07},
        }
    )
    assert "choice=click" in text and "confidence=0.520" in text
    assert text.index("click=0.570") < text.index("press_enter=0.420")  # ranked
    assert "satisfied: noul=0.07" in text


# --------------------------------------------------------------- run folder
def test_run_folder_writes_every_artefact(tmp_path: Path):
    folder = RunFolder.create(tmp_path)
    page = make_page()
    folder.step_history(1, ["type_text: 'x'"])
    folder.step_state(1, {"goal": "g", "text_to_type": "x"})
    folder.step_answers(1, {"kind": {"type": "choice", "choice": "click", "probabilities": {}, "confidence": 0.5}})
    folder.step_elements(1, page, can_write=True)
    folder.step_payload(
        1, render_payload(goal="g", page=page, history=[], state={"goal": "g"}, actions={"click": "c"}, elements={"0": "e"})
    )
    folder.finish({"goal": "g", "outcome": "done"})
    folder.log("hello")

    for name in (
        "run.log",
        "run.json",
        "step-01-history.json",
        "step-01-state.json",
        "step-01-answers.json",
        "step-01-elements.json",
        "step-01-payload.txt",
    ):
        assert (folder.root / name).exists(), name


def test_load_step_round_trips_a_step(tmp_path: Path):
    folder = RunFolder.create(tmp_path)
    page = make_page()
    folder.step_history(1, ["click: [1] 'Search'"])
    folder.step_state(1, {"goal": "g", "previous_actions": ["click: [1] 'Search'"]})
    folder.step_answers(1, {"kind": {"type": "choice", "choice": "click", "probabilities": {"click": 1.0}, "confidence": 0.9}})
    folder.step_elements(1, page, can_write=True)
    folder.finish({"goal": "find the invoice", "outcome": "done"})

    step = load_step(folder.root, 1)
    assert step["goal"] == "find the invoice"
    assert step["history"] == ["click: [1] 'Search'"]
    assert step["state"]["previous_actions"] == ["click: [1] 'Search'"]
    assert step["can_write"] is True
    assert step["answers"]["kind"]["choice"] == "click"

    rebuilt = step["page"]
    assert rebuilt.url == page.url and rebuilt.title == page.title
    assert [(e.index, e.name, e.x, e.y, e.tag) for e in rebuilt.items] == [(e.index, e.name, e.x, e.y, e.tag) for e in page.items]
    assert rebuilt.field_count == 1  # derived from the items, not stored
    assert rebuilt.items == page.items
    assert rebuilt.can_scroll and rebuilt.scroll_y == 0
    assert rebuilt.text == []  # an old run folder, written before page text, still loads


def test_load_step_round_trips_page_text(tmp_path: Path):
    """The evidence blocks come back with the elements, so a replayed step rebuilds the
    exact state the classifier saw, page_text included."""
    folder = RunFolder.create(tmp_path)
    page = make_page(
        text=[
            TextBlock("t0", "Invoice #1042 total $42.10", 10, 300, 300, 20),
            TextBlock("t1", "Payment failed: card declined", 10, 330, 300, 20),
        ]
    )
    folder.step_elements(1, page, can_write=False)
    folder.finish({"goal": "g", "outcome": "done"})

    rebuilt = load_step(folder.root, 1)["page"]
    assert rebuilt.text == page.text
    assert [tb.evidence_id for tb in rebuilt.text] == ["t0", "t1"]


def test_render_payload_shows_page_text_as_evidence():
    page = make_page(text=[TextBlock("t0", "Next concert SEP 19", 10, 300, 300, 20)])
    text = render_payload(goal="g", page=page, history=[], state={"goal": "g"}, actions={}, elements={})
    assert "PAGE TEXT" in text
    assert "never click targets" in text
    assert "Next concert SEP 19" in text


def test_missing_step_raises(tmp_path: Path):
    folder = RunFolder.create(tmp_path)
    folder.finish({"goal": "g"})
    with pytest.raises(FileNotFoundError):
        load_step(folder.root, 9)


def test_render_payload_shows_state_criteria_and_elements():
    text = render_payload(
        goal="g",
        page=make_page(),
        history=[],
        state={"goal": "g", "page": {"url": "u"}},
        actions={"click": "Click an element"},
        elements={"0": "<input> 'Search invoices'"},
    )
    assert "sent as `state`" in text
    assert "QUESTION kind" in text and "QUESTION element" in text
    assert "3 of 90 candidates" in text
    assert "[  0]" in text and "Search invoices" in text


# ----------------------------------------------------------------- writer
@pytest.mark.parametrize(
    "label",
    ["Password", "Enter your one-time code", "CVV", "Card number", "API key", "PIN", "seed phrase"],
)
def test_credential_guard_catches_dangerous_fields(label):
    assert looks_credential(label)


@pytest.mark.parametrize("label", ["Search invoices", "Customer name", "PO number", "Email address"])
def test_credential_guard_allows_ordinary_fields(label):
    assert not looks_credential(label)


def test_credential_hints_cover_the_obvious_cases():
    for must in ("password", "otp", "cvv", "token"):
        assert any(must in hint for hint in CREDENTIAL_HINTS)


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Messages:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return type("R", (), {"content": [_Block(json.dumps(self.payload))]})()


class StubWriter:
    def __init__(self, payload):
        self.messages = _Messages(payload)
        self.api_key = "stub"


def test_writer_refuses_credential_fields_without_calling_the_model():
    writer = StubWriter({"fill": True, "text": "hunter2", "reason": "no"})
    assert (
        compose_browser_text(writer, "log in", field_label="Password", page_title="t", url="u", nearby_text=[], history=[]) == ""
    )
    assert writer.messages.calls == 0


def test_writer_returns_composed_text():
    writer = StubWriter({"fill": True, "text": "invoice automation", "reason": "from the goal"})
    got = compose_browser_text(
        writer,
        "search for invoice automation",
        field_label="Search invoices",
        page_title="Dashboard",
        url="u",
        nearby_text=["Search"],
        history=[],
    )
    assert got == "invoice automation"
    assert writer.messages.calls == 1


def test_writer_declines_when_fill_is_false():
    writer = StubWriter({"fill": False, "text": "should not be used", "reason": "not my business"})
    assert compose_browser_text(writer, "g", field_label="Search", page_title="t", url="u", nearby_text=[], history=[]) == ""


def test_writer_output_that_looks_like_a_secret_is_dropped():
    writer = StubWriter({"fill": True, "text": "my api key is abc123", "reason": "oops"})
    assert compose_browser_text(writer, "g", field_label="Notes", page_title="t", url="u", nearby_text=[], history=[]) == ""


# ------------------------------------------------------- text provenance
def test_resolve_text_streams_from_the_writer_when_available():
    writer = StubWriter({"fill": True, "text": "invoice automation", "reason": "ok"})
    text, source = resolve_text(writer, "search for 'invoice automation'", make_page(), make_page().items[0], [])
    assert (text, source) == ("invoice automation", "writer")


def test_resolve_text_reports_a_writer_decline_distinctly():
    writer = StubWriter({"fill": False, "text": "", "reason": "no"})
    text, source = resolve_text(writer, "search for 'x'", make_page(), make_page().items[0], [])
    assert text == "" and source == "writer_declined"


def test_resolve_text_survives_a_writer_failure():
    class Boom:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("upstream 500")

    text, source = resolve_text(Boom, "search for 'x'", make_page(), make_page().items[0], [])
    assert text == "" and source == "writer_error(RuntimeError)"


def test_resolve_text_without_a_writer_types_nothing():
    """Free text only comes from the writer: there is no fallback that reads it out of the goal."""
    text, source = resolve_text(None, "search for 'invoice automation'", make_page(), make_page().items[0], [])
    assert text == "" and source == "no_writer"
