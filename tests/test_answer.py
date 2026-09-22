import base64
import io
import json
from types import SimpleNamespace

import anthropic
import pytest
from PIL import Image

from typesafe_computer_use import runner, writer
from typesafe_computer_use.actions import Context
from typesafe_computer_use.models import Guidance
from typesafe_computer_use.runner import RunConfig, RunState, hand_off, resolve
from typesafe_computer_use.writer import ANSWER_IMAGE_EDGE, Answer, compose_answer

GOAL = "find the next upcoming bruno mars concert"


class FakeWriter:
    """Stands in for the Anthropic client: records each request and replies with one JSON text block."""

    def __init__(self, reply: dict):
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)
        self._reply = {"focus": "", "question": "", **reply} if "answer" in reply else reply

    def _create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self._reply))])


def context(writer=None) -> Context:
    return Context(goal=GOAL, browser="Google Chrome", email=None, typesafe=None, writer=writer, history=[])


def logged() -> tuple[list[str], runner.Log]:
    lines: list[str] = []
    return lines, lambda msg="", echo=True: lines.append(msg)


def test_the_answer_request_carries_the_capture_and_the_run(screen, make_item, monkeypatch):
    monkeypatch.setenv("CLICKER_ANSWER_MODEL", "answer-model")
    fake = FakeWriter({"achieved": True, "answer": " Sep 19, 2026 in Miami. "})

    answer = compose_answer(fake, GOAL, screen, [make_item(0, "SEP 19, 2026")], ["clicked 'TOUR'"], "the goal is achieved")

    assert answer == Answer(text="Sep 19, 2026 in Miami.", achieved=True)
    request = fake.requests[0]
    assert request["model"] == "answer-model"
    image, text = request["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    packet = json.loads(text["text"])
    assert packet["goal"] == GOAL
    assert packet["why_the_run_stopped"] == "the goal is achieved"
    assert packet["actions_taken"] == ["clicked 'TOUR'"]
    assert packet["screen_text_in_reading_order"] == ["SEP 19, 2026"]


def test_the_answer_request_carries_what_the_run_has_learned_and_offers_both_ways_on(screen, make_item):
    fake = FakeWriter({"achieved": False, "answer": "Two sizes.", "focus": " Click '15 inch' ", "question": ""})
    guided = Guidance().heard("13 or 15 inch?", "15").focused("Open the size menu")
    stops = [{"after_action": 1, "why": "the last actions changed nothing", "focus_given": "Open the size menu"}]

    answer = compose_answer(fake, GOAL, screen, [], [], "stopped", guidance=guided, earlier_stops=stops, can_ask=True)

    assert answer == Answer(text="Two sizes.", achieved=False, focus="Click '15 inch'", question="")
    request = fake.requests[0]
    assert set(request["output_config"]["format"]["schema"]["required"]) == {"achieved", "answer", "focus", "question"}
    packet = json.loads(request["messages"][0]["content"][1]["text"])
    assert packet["current_focus"] == "Open the size menu"
    assert packet["user_said"] == [{"asked": "13 or 15 inch?", "replied": "15"}]
    assert packet["earlier_stops"] == stops and packet["user_can_be_asked"] is True


def test_the_typed_text_and_the_proposed_url_are_composed_under_the_guidance(screen):
    guided = Guidance().heard("which artist?", "bruno mars")
    fake = FakeWriter({"fill": True, "text": "bruno mars", "ok": True, "url": "https://www.brunomars.com"})

    writer.compose_text(fake, GOAL, screen, [], [], guided)
    writer.compose_url(fake, GOAL, [], guided)

    for request in fake.requests:
        assert json.loads(request["messages"][0]["content"][0]["text"])["user_said"] == [
            {"asked": "which artist?", "replied": "bruno mars"}
        ]


def test_the_capture_is_shrunk_to_the_edge_the_model_reads_and_left_intact():
    capture = Image.new("RGBA", (3456, 2234))

    block = writer._image_block(capture)

    sent = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert sent.format == "PNG"
    assert max(sent.size) == ANSWER_IMAGE_EDGE
    assert capture.size == (3456, 2234)


def test_requests_without_a_capture_stay_text_only_on_the_writer_model(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_MODEL", "writer-model")
    fake = FakeWriter({"ok": True, "url": "https://www.brunomars.com"})

    assert writer.compose_url(fake, GOAL, []) == "https://www.brunomars.com"

    request = fake.requests[0]
    assert request["model"] == "writer-model"
    assert [block["type"] for block in request["messages"][0]["content"]] == ["text"]


@pytest.mark.parametrize("outcome", ["dry run", "aborted (Ctrl-C)", "crashed"])
def test_a_run_that_has_nothing_to_report_asks_for_no_answer(outcome, tmp_path, screen):
    fake = FakeWriter({"achieved": True, "answer": "unused"})
    state = RunState(outcome=outcome, view=(screen, []))
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(fake), state, 1, log)

    assert state.answer is None and not fake.requests and not lines


def test_without_a_writer_the_run_says_why_there_is_no_answer(tmp_path, screen):
    state = RunState(outcome="done", view=(screen, []))
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(), state, 1, log)

    assert state.answer is None
    assert "no answer" in lines[0] and "ANTHROPIC_API_KEY" in lines[0]


def test_the_last_capture_is_answered_from_when_nothing_acted_after_it(tmp_path, screen, make_item, monkeypatch):
    monkeypatch.setattr(runner, "capture", lambda *a, **k: pytest.fail("captured again"))
    fake = FakeWriter({"achieved": True, "answer": "Sep 19, 2026 in Miami."})
    state = RunState(outcome="done", view=(screen, [make_item(0, "SEP 19, 2026")]))
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(fake), state, 1, log)

    assert state.answer == Answer(text="Sep 19, 2026 in Miami.", achieved=True)
    assert "goal achieved" in lines[0] and "Sep 19, 2026 in Miami." in lines[0]
    assert not (tmp_path / "answer-raw.png").exists()
    assert "already achieved" in json.loads(fake.requests[0]["messages"][0]["content"][1]["text"])["why_the_run_stopped"]


def test_the_screen_is_captured_again_when_an_action_made_the_last_capture_stale(tmp_path, screen, make_item, monkeypatch):
    monkeypatch.setattr(runner.desktop, "check_abort", lambda: None)
    monkeypatch.setattr(runner, "capture", lambda *a, **k: screen)
    monkeypatch.setattr(runner, "perceive", lambda *a, **k: [make_item(0, "TICKETS")])
    fake = FakeWriter({"achieved": False, "answer": "No dates on screen."})
    state = RunState(outcome="step limit", view=None)
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(fake), state, 1, log)

    assert state.answer == Answer(text="No dates on screen.", achieved=False)
    assert "goal not achieved" in lines[0]
    assert (tmp_path / "answer-raw.png").exists()
    assert json.loads(fake.requests[0]["messages"][0]["content"][1]["text"])["screen_text_in_reading_order"] == ["TICKETS"]


def test_a_writer_that_fails_costs_the_answer_and_not_the_run(tmp_path, screen):
    def refuse(**request):
        raise anthropic.APIConnectionError(request=SimpleNamespace())  # the error only carries the request along

    fake = FakeWriter({})
    fake.messages.create = refuse
    state = RunState(outcome="done", view=(screen, []))
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(fake), state, 1, log)

    assert state.answer is None
    assert "no answer: the writer failed" in lines[0]


def decision(kind: str, confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(kind=SimpleNamespace(choice=kind), stops=kind in ("done", "none"), confidence=confidence, chosen=kind)


@pytest.mark.parametrize(
    ("kind", "confidence", "outcome"),
    [
        ("done", 0.9, "done"),
        ("none", 0.9, "nothing helps"),
        ("scroll_down", 0.2, "low confidence"),
        ("scroll_down", 0.9, "dry run"),
    ],
)
def test_every_stop_names_its_outcome(kind, confidence, outcome, tmp_path, screen):
    state = RunState()
    _, log = logged()

    keep_going = resolve(RunConfig(goal=GOAL, out=tmp_path), context(), state, screen, [], decision(kind, confidence), {}, log)

    assert not keep_going
    assert state.outcome == outcome
    assert (outcome in runner.STOPPED) == (outcome != "dry run")


def test_an_action_makes_the_last_capture_stale(tmp_path, screen, monkeypatch):
    monkeypatch.setattr(runner, "perform", lambda *a: "scrolled down")
    state = RunState(view=(screen, []))
    _, log = logged()

    keep_going = resolve(
        RunConfig(goal=GOAL, out=tmp_path, act=True), context(), state, screen, [], decision("scroll_down"), {}, log
    )

    assert keep_going
    assert state.view is None
