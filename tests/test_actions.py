import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from typesafe_computer_use import actions, macos
from typesafe_computer_use.actions import click_item, fill_field, press_offscreen
from typesafe_computer_use.models import AxNode, Field, Item


@pytest.fixture
def calls(monkeypatch):
    """Every trip to the machine, recorded instead of made."""
    log: list[tuple] = []
    monkeypatch.setattr(macos, "click_at", lambda point: log.append(("click", point)))
    monkeypatch.setattr(macos, "type_text", lambda text: log.append(("type", text)))
    monkeypatch.setattr(macos, "ax_focus", lambda ref: log.append(("focus", ref)) or True)
    monkeypatch.setattr(macos, "clear_field", lambda: log.append(("clear",)))
    return log


def field(ref=None, value="") -> Field:
    return Field(role="AXTextField", label="Email", placeholder="", value=value, x=10, y=20, w=200, h=30, ref=ref)


def test_an_item_from_the_accessibility_tree_is_pressed(screen, calls, monkeypatch):
    ref = object()
    pressed = []
    monkeypatch.setattr(macos, "ax_press", lambda r: pressed.append(r) or True)
    item = Item(3, "Register Now", 1.0, 100.0, 100.0, 300.0, 140.0, role="link", source="ax")
    live = replace(screen, ax_refs={3: ref})
    assert click_item(item, live) == "pressed 'Register Now' via accessibility"
    assert pressed == [ref] and calls == []


def test_a_refused_press_falls_back_to_the_mouse(screen, calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_press", lambda ref: False)
    item = Item(3, "Register Now", 1.0, 100.0, 100.0, 300.0, 140.0, role="link", source="ax")
    live = replace(screen, ax_refs={3: object()})
    assert click_item(item, live) == "clicked 'Register Now' (accessibility press did not take)"
    assert calls == [("click", (100.0, 60.0))]


def test_an_ocr_only_item_is_clicked_without_asking_accessibility(screen, calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_press", lambda ref: pytest.fail("no element to press"))
    item = Item(3, "Register Now", 0.9, 100.0, 100.0, 300.0, 140.0)
    assert click_item(item, screen) == "clicked 'Register Now'"
    assert calls == [("click", (100.0, 60.0))]


def test_an_off_screen_control_is_pressed_through_accessibility(screen, calls, monkeypatch):
    ref = object()
    pressed = []
    monkeypatch.setattr(macos, "ax_press", lambda r: pressed.append(r) or True)
    node = AxNode(role="AXLink", label="Register Now", x=0.0, y=-4200.0, w=120.0, h=32.0, pressable=True, ref=ref)
    live = replace(screen, offscreen=[node])
    assert press_offscreen("0", live) == "pressed 'Register Now' (off-screen control) via accessibility"
    assert pressed == [ref] and calls == []


def test_a_refused_off_screen_press_is_a_no_op_with_nothing_to_click(screen, calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_press", lambda ref: False)
    node = AxNode(role="AXLink", label="Register Now", x=0.0, y=-4200.0, w=120.0, h=32.0, pressable=True, ref=object())
    refusal = press_offscreen("0", replace(screen, offscreen=[node]))
    assert refusal == "press_offscreen refused: 'Register Now' did not accept the press"
    assert calls == []


def test_an_offscreen_key_that_names_nothing_is_refused(screen, calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_press", lambda ref: pytest.fail("no element to press"))
    refusal = press_offscreen("4", screen)
    assert refusal == "press_offscreen refused: there is no off-screen control '4'"
    assert calls == []


def test_perform_routes_an_offscreen_key_to_the_press(screen, calls, monkeypatch):
    pressed = []
    monkeypatch.setattr(macos, "ax_press", lambda r: pressed.append(r) or True)
    ref = object()
    node = AxNode(role="AXRow", label="Note 900", x=0.0, y=42718.0, w=280.0, h=68.0, pressable=True, ref=ref)
    live = replace(screen, offscreen=[node])
    decision = SimpleNamespace(chosen="offscreen:0")
    assert actions.perform(decision, live, [], None) == "pressed 'Note 900' (off-screen control) via accessibility"
    assert pressed == [ref]


def context(writer=None) -> actions.Context:
    return actions.Context(
        goal="find the next upcoming bruno mars concert",
        browser="Google Chrome",
        email=None,
        typesafe=None,
        writer=writer,
        history=[],
    )


def browsing(site: str) -> SimpleNamespace:
    return SimpleNamespace(chosen="use_browser", site=SimpleNamespace(choice=site))


@pytest.fixture
def browser(monkeypatch):
    """The trips use_browser makes, recorded, with both of them reporting success."""
    log: list[tuple] = []
    monkeypatch.setattr(macos, "activate", lambda app: log.append(("activate", app)) or True)
    monkeypatch.setattr(macos, "open_url", lambda app, url: log.append(("open", app, url)) or True)
    return log


def test_use_browser_with_no_site_only_brings_the_browser_forward(screen, browser):
    assert actions.perform(browsing("none"), screen, [], context()) == "activated Google Chrome"
    assert browser == [("activate", "Google Chrome")]


def test_use_browser_opens_a_catalog_site_by_its_url(screen, browser, monkeypatch):
    monkeypatch.setattr(actions, "compose_url", lambda *a: pytest.fail("the catalog already names this site"))
    assert actions.perform(browsing("github"), screen, [], context()) == "opened https://github.com/"
    assert browser == [("open", "Google Chrome", "https://github.com/")]


def test_use_browser_asks_the_writer_for_a_site_outside_the_catalog(screen, browser, monkeypatch):
    writer = object()
    asked = []
    monkeypatch.setattr(
        actions,
        "compose_url",
        lambda w, goal, history, guidance: asked.append((w, goal)) or "https://www.songkick.com/",
    )
    assert actions.perform(browsing("other"), screen, [], context(writer)) == "opened https://www.songkick.com/"
    assert asked == [(writer, "find the next upcoming bruno mars concert")]
    assert browser == [("open", "Google Chrome", "https://www.songkick.com/")]


def test_use_browser_without_a_writer_refuses_a_site_outside_the_catalog(screen, browser):
    refusal = actions.perform(browsing("other"), screen, [], context())
    assert refusal == "use_browser refused: the site is outside the catalog and no writer is available to propose a URL"
    assert browser == []


def test_use_browser_refuses_when_the_writer_proposes_nothing(screen, browser, monkeypatch):
    monkeypatch.setattr(actions, "compose_url", lambda writer, goal, history, guidance: "")
    refusal = actions.perform(browsing("other"), screen, [], context(object()))
    assert refusal == "use_browser refused: the writer proposed no usable URL for this goal"
    assert browser == []


def test_a_browser_that_does_not_come_to_the_front_is_a_no_op(screen, monkeypatch):
    monkeypatch.setattr(macos, "activate", lambda app: False)
    failure = actions.perform(browsing("none"), screen, [], context())
    assert failure == "use_browser failed: Google Chrome did not come to the front"


def test_typing_sets_the_value_when_the_field_reads_it_back(calls, monkeypatch):
    written = []
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: written.append(text) or True)
    monkeypatch.setattr(macos, "ax_value", lambda ref: written[-1])
    ref = object()
    assert fill_field(field(ref=ref), "user@example.com") == "via accessibility"
    assert written == ["user@example.com"] and calls == [("focus", ref)]


def test_typing_accepts_a_read_back_that_ends_with_the_text(calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: True)
    monkeypatch.setattr(macos, "ax_value", lambda ref: "mailto:user@example.com")
    assert fill_field(field(ref=object()), "user@example.com") == "via accessibility"
    assert ("type", "user@example.com") not in calls


def test_typing_falls_back_to_keystrokes_when_the_value_does_not_stick(calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: True)
    monkeypatch.setattr(macos, "ax_value", lambda ref: "user@exam")  # the element took part of it and reads back the rest
    assert fill_field(field(ref=object()), "user@example.com") == "via keystrokes"
    assert calls[-2:] == [("clear",), ("type", "user@example.com")]  # emptied first, whatever the capture said the value was


def test_typing_falls_back_to_keystrokes_when_the_element_refuses(calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: False)
    monkeypatch.setattr(macos, "ax_value", lambda ref: pytest.fail("nothing was written"))
    assert fill_field(field(ref=object()), "hello") == "via keystrokes"
    assert calls[-1] == ("type", "hello")


def test_typing_uses_keystrokes_when_there_is_no_element(calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: pytest.fail("no element to write to"))
    assert fill_field(field(), "hello") == "via keystrokes"
    assert calls == [("clear",), ("type", "hello")]


def test_the_field_record_leaves_the_element_out_so_a_run_can_be_written():
    record = field(ref=object(), value="hello").record()
    assert "ref" not in record and json.loads(json.dumps(record))["value"] == "hello"


def test_a_wait_gives_the_page_time_before_the_step_delay(monkeypatch):
    slept = []
    monkeypatch.setattr(macos, "sleep_watching", slept.append)
    assert actions._HANDLERS["wait"](None, None, [], None) == "waited"
    assert slept == [actions.WAIT_SECONDS]


def test_clicking_a_duplicated_label_says_which_row(screen, calls):
    items = [
        Item(0, "Coldplay", 1.0, 100, 200, 300, 230),
        Item(1, "Buy", 1.0, 420, 200, 480, 230),
        Item(2, "Adele", 1.0, 100, 260, 300, 290),
        Item(3, "Buy", 1.0, 420, 260, 480, 290),
        Item(4, "Terms", 1.0, 100, 320, 300, 350),
    ]

    def clicking(key: str):
        return SimpleNamespace(chosen=key)

    assert actions.perform(clicking("3"), screen, items, None) == "clicked 'Buy' beside 'Adele'"
    assert actions.perform(clicking("4"), screen, items, None) == "clicked 'Terms'"
    assert [point for kind, point in calls if kind == "click"] == [(225.0, 137.5), (100.0, 167.5)]
