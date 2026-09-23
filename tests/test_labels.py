"""Naming icon-only controls with a model that reads images, once per layout, as the writer's work."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from typesafe_computer_use import labels, perception, runner
from typesafe_computer_use.actions import Context
from typesafe_computer_use.calls import WRITER, Calls, MeteredWriter
from typesafe_computer_use.labels import Labeller
from typesafe_computer_use.models import AxNode
from typesafe_computer_use.platform_adapter import desktop


def icon(x: float, y: float, w: float = 16.0, h: float = 16.0) -> AxNode:
    return AxNode(role="AXButton", label="", x=x, y=y, w=w, h=h, pressable=True, ref=f"icon@{x},{y}")


class FakeVision:
    """A writer client with eyes: records each request and answers with names, or fails."""

    def __init__(self, names=None, fails: bool = False):
        self.requests: list[dict] = []
        self.names = names if names is not None else [{"box": 0, "name": "close window"}]
        self.fails = fails
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        import anthropic
        import httpx

        self.requests.append(request)
        if self.fails:
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1/"))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"names": self.names}))])

    def packet(self, n: int = 0) -> dict:
        return json.loads(self.requests[n]["messages"][0]["content"][-1]["text"])

    def image_sent(self, n: int = 0) -> bool:
        return self.requests[n]["messages"][0]["content"][0]["type"] == "image"


def test_the_crop_holds_the_controls_and_stays_big_enough_to_read(screen):
    left, top, right, bottom = labels.bounds(screen, [icon(8.0, 41.0), icon(54.0, 41.0)])

    assert left < 16 and top < 82  # both icons, in capture pixels, are inside
    assert right - left >= labels.MIN_EDGE_PX and bottom - top >= labels.MIN_EDGE_PX


def test_controls_scattered_over_the_screen_are_sent_whole(screen):
    assert labels.bounds(screen, [icon(4.0, 4.0), icon(740.0, 580.0)]) == (0, 0, screen.image.width, screen.image.height)


def test_each_control_is_boxed_and_numbered_on_the_image_that_is_sent(screen):
    drawn = labels.draw(screen, [icon(8.0, 41.0), icon(54.0, 41.0)])

    colours = drawn.getcolors(maxcolors=1 << 20) or []
    assert any(r > 200 and g < 80 and b < 80 for _, (r, g, b) in colours)  # the red boxes


def test_a_named_control_comes_back_carrying_its_name(screen):
    vision = FakeVision([{"box": 0, "name": "close window"}, {"box": 1, "name": ""}])

    named = Labeller()(vision, screen, [icon(8.0, 41.0), icon(54.0, 41.0)])

    assert [(n.label, n.pressable, n.ref) for n in named] == [
        ("close window", True, "icon@8.0,41.0")
    ]  # the nameless one is dropped
    assert vision.image_sent()
    assert vision.packet()["boxes"] == [{"box": 0, "shape": "button"}, {"box": 1, "shape": "button"}]


def test_a_layout_is_named_once_however_many_steps_look_at_it(screen):
    vision, labeller = FakeVision(), Labeller()

    for _ in range(4):
        assert len(labeller(vision, screen, [icon(8.0, 41.0)])) == 1

    assert len(vision.requests) == 1


def test_controls_that_move_are_named_again(screen):
    vision, labeller = FakeVision(), Labeller()

    labeller(vision, screen, [icon(8.0, 41.0)])
    labeller(vision, screen, [icon(8.0, 300.0)])

    assert len(vision.requests) == 2


def test_a_model_that_fails_costs_the_names_and_not_the_step(screen):
    vision, labeller = FakeVision(fails=True), Labeller()

    assert labeller(vision, screen, [icon(8.0, 41.0)]) == []
    assert labeller(vision, screen, [icon(8.0, 41.0)]) == []  # and it is not asked again for the same layout
    assert labeller.failures == 1 and len(vision.requests) == 1


def test_a_name_for_a_box_that_was_never_drawn_is_ignored(screen):
    assert Labeller()(FakeVision([{"box": 9, "name": "somewhere else"}]), screen, [icon(8.0, 41.0)]) == []


# ------------------------------------------------------------------ in the loop


def test_named_icons_join_the_items_and_can_be_pressed(screen, monkeypatch):
    live = replace(screen, pid=42, window=None)
    monkeypatch.setattr(perception, "ocr", lambda *a, **k: [])
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: ([], [], False))
    monkeypatch.setattr(desktop, "nameless_elements", lambda pid, w, h: [icon(8.0, 41.0)])

    items = perception.perceive(live, 255, "close it", name_icons=lambda s, nodes: Labeller()(FakeVision(), s, nodes))

    assert [(it.text, it.role) for it in items] == [("close window", "button")]
    assert live.ax_refs[items[0].index] == "icon@8.0,41.0"


def test_without_a_namer_the_unlabelled_controls_are_never_even_looked_for(screen, monkeypatch):
    live = replace(screen, pid=42, window=None)
    monkeypatch.setattr(perception, "ocr", lambda *a, **k: [])
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: ([], [], False))
    monkeypatch.setattr(desktop, "nameless_elements", lambda pid, w, h: pytest.fail("a second walk nobody asked for"))

    assert perception.perceive(live, 255, "close it") == []


def test_each_naming_is_counted_as_the_writers_work(screen):
    calls = Calls()
    vision = FakeVision()
    ctx = Context(
        goal="g",
        browser="Safari",
        email=None,
        typesafe=None,
        writer=None,
        history=[],
        answerer=MeteredWriter(vision, calls),
        labeller=Labeller(),
    )

    runner.icon_namer(ctx)(screen, [icon(8.0, 41.0)])

    assert calls.count[WRITER] == 1


def test_no_labeller_no_namer():
    ctx = Context(goal="g", browser="Safari", email=None, typesafe=None, writer=FakeVision(), history=[])
    assert runner.icon_namer(ctx) is None
