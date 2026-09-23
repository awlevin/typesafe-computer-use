"""The actions past a plain click: keys, menus, windows, apps, double and right clicks, and what each is offered for."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from typesafe_computer_use import actions, macos, perception, windows
from typesafe_computer_use.config import KEY_ACTIONS
from typesafe_computer_use.decide import (
    Decision,
    apps_offered,
    base_state,
    key_criteria,
    menu_criteria,
    screen_kind_criteria,
    target_criteria,
    window_criteria,
)
from typesafe_computer_use.models import Field, Item, MenuItem, Missed, Screen, WindowRef
from typesafe_computer_use.platform_adapter import desktop


def answer(choice, confidence=0.9):
    return SimpleNamespace(choice=choice, confidence=confidence, probabilities={choice: confidence})


def decision(kind, **targets):
    return Decision(kind=answer(kind), item=targets.pop("item", None), site=answer("none"), **targets)


def context(**overrides) -> actions.Context:
    base = actions.Context(goal="g", browser="Google Chrome", email=None, typesafe=None, writer=None, history=[])
    return replace(base, **overrides)


TEXT = Field(role="AXTextField", label="Note", placeholder="", value="", x=0, y=0, w=100, h=20)
SECURE = replace(TEXT, label="Password", secure=True)


# ------------------------------------------------------------------ keys


@pytest.fixture
def presses(monkeypatch):
    log = []
    monkeypatch.setattr(desktop, "press", lambda key, command=False, shift=False: log.append((key, command, shift)))
    return log


def test_a_shortcut_is_sent_as_its_keys(screen, presses):
    assert actions.perform(decision("press_key", key=answer("redo")), screen, [], context()) == "pressed redo"
    assert presses == [("z", True, True)]


def test_every_shortcut_names_keys_both_adapters_press():
    for name, action in KEY_ACTIONS.items():
        assert action.key in macos.KEYCODES and action.key in windows.VK, name


def test_a_shortcut_outside_the_catalog_is_refused_rather_than_improvised(screen, presses):
    assert "is not a shortcut" in actions.perform(decision("press_key", key=answer("format_disk")), screen, [], context())
    assert presses == []


def test_undo_is_offered_because_a_run_needs_a_way_back():
    assert "undo" in key_criteria(None)


def test_no_key_offered_duplicates_a_fixed_action():
    keys = key_criteria(TEXT)
    assert not {"back", "page_up", "page_down", "return", "escape"} & set(keys)


def test_paste_is_neither_offered_nor_pressed_into_a_credential_field(screen, presses):
    assert "paste" in key_criteria(TEXT) and "paste" not in key_criteria(SECURE)
    assert "paste" not in key_criteria(replace(TEXT, placeholder="one-time code"))

    locked = replace(screen, field=SECURE)
    refusal = actions.perform(decision("press_key", key=answer("paste")), locked, [], context())

    assert refusal == "press_key refused: paste into a field that asks for a credential"
    assert presses == []


def test_backspace_needs_a_field_to_delete_in(screen, presses):
    assert "backspace" not in key_criteria(None) and "backspace" in key_criteria(TEXT)
    assert "refused" in actions.perform(decision("press_key", key=answer("backspace")), screen, [], context())
    assert presses == []


def test_the_clipboard_is_mentioned_to_the_classifier_only_when_it_is_shared():
    assert "state on the next step" not in key_criteria(None)["copy"]
    assert "state on the next step" in key_criteria(None, clipboard_shared=True)["copy"]


# ------------------------------------------------------------------ the menu bar


def menu_screen(screen, *entries, field=None) -> Screen:
    menu = [MenuItem(path=path, chord=chord, ref=f"ref:{path}") for path, chord in entries]
    return replace(screen, menu=menu, field=field)


def test_a_menu_command_is_pressed_through_accessibility(screen, monkeypatch):
    pressed = []
    monkeypatch.setattr(desktop, "ax_press", lambda ref: pressed.append(ref) or True)
    live = menu_screen(screen, ("File ▸ Export as PDF…", None))

    what = actions.perform(decision("press_menu", menu=answer("0")), live, [], context())

    assert what == "ran the menu command 'File ▸ Export as PDF…'" and pressed == ["ref:File ▸ Export as PDF…"]


def test_a_menu_command_that_refuses_the_press_is_a_no_op(screen, monkeypatch):
    monkeypatch.setattr(desktop, "ax_press", lambda ref: False)
    live = menu_screen(screen, ("File ▸ Export as PDF…", None))
    assert "did not accept the press" in actions.perform(decision("press_menu", menu=answer("0")), live, [], context())


def test_a_command_a_named_key_already_runs_is_left_to_the_key(screen):
    live = menu_screen(
        screen,
        ("File ▸ Save", ("s", True, False)),
        ("Edit ▸ Redo", ("z", True, True)),
        ("History ▸ Back", ("[", True, False)),  # go_back
        ("Format ▸ Bold", ("b", True, False)),  # no named key sends Command-B
    )
    assert list(menu_criteria(live).values()) == ["Format ▸ Bold"]


def test_a_command_that_is_not_on_offer_is_refused_even_if_chosen(screen, monkeypatch):
    monkeypatch.setattr(desktop, "ax_press", lambda ref: pytest.fail("a withheld command must not be pressed"))
    live = menu_screen(screen, ("File ▸ Save", ("s", True, False)), ("Edit ▸ AutoFill ▸ Passwords…", None))
    for key in ("0", "1", "7"):
        assert "no menu command" in actions.perform(decision("press_menu", menu=answer(key)), live, [], context())


def test_paste_from_the_menu_is_withheld_only_from_a_credential_field(screen):
    entries = (("Edit ▸ Paste and Match Style", None),)
    assert menu_criteria(menu_screen(screen, *entries, field=TEXT))
    assert not menu_criteria(menu_screen(screen, *entries, field=SECURE))


# ------------------------------------------------------------------ windows


def window_screen(screen) -> Screen:
    return replace(screen, windows=[WindowRef("Draft", main=True, ref="draft"), WindowRef("Inbox", ref="inbox")])


def test_another_window_of_the_app_is_brought_forward(screen, monkeypatch):
    raised = []
    monkeypatch.setattr(desktop, "raise_window", lambda ref: raised.append(ref) or True)

    what = actions.perform(decision("focus_window", window=answer("1")), window_screen(screen), [], context())

    assert what == "brought the window 'Inbox' to the front" and raised == ["inbox"]


def test_the_window_in_front_is_neither_offered_nor_raised(screen, monkeypatch):
    monkeypatch.setattr(desktop, "raise_window", lambda ref: pytest.fail("the front window is already in front"))
    live = window_screen(screen)
    assert window_criteria(live) == {"1": "'Inbox'"}
    assert "no other window" in actions.perform(decision("focus_window", window=answer("0")), live, [], context())


# ------------------------------------------------------------------ apps

APPS = ("Finder", "Google Chrome", "Notes", "Visual Studio Code")


def test_running_apps_come_first_and_the_browser_and_the_front_app_are_left_out(screen):
    live = replace(screen, app="Code", running=frozenset({"Finder"}))
    assert apps_offered(APPS, live, "Google Chrome") == ["Finder", "Notes"]  # Code is Visual Studio Code, in front


def test_choosing_an_app_brings_it_up(screen, monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "activate", lambda name: opened.append(name) or True)
    assert actions.perform(decision("open_app", app=answer("Notes")), screen, [], context(apps=APPS)) == "opened Notes"
    assert opened == ["Notes"]


def test_an_app_that_does_not_come_forward_is_a_no_op(screen, monkeypatch):
    monkeypatch.setattr(desktop, "activate", lambda name: False)
    assert "did not come to the front" in actions.perform(
        decision("open_app", app=answer("Notes")), screen, [], context(apps=APPS)
    )


@pytest.mark.parametrize("name", ["Photoshop", "Google Chrome", "", None])
def test_an_app_not_on_offer_is_refused_rather_than_guessed_at(screen, monkeypatch, name):
    monkeypatch.setattr(desktop, "activate", lambda app: pytest.fail("nothing should be launched"))
    chosen = decision("open_app", app=answer(name) if name is not None else None)
    assert "open_app refused" in actions.perform(chosen, screen, [], context(apps=APPS))


def test_an_app_is_left_by_opening_another_so_it_does_not_gate_the_run():
    d = Decision(kind=answer("open_app", 0.8), item=None, site=answer("none"), app=answer("Notes", 0.05))
    assert d.confidence == 0.8


def test_a_key_a_menu_command_and_a_window_gate_like_a_click():
    for kind, target in (("press_key", "key"), ("press_menu", "menu"), ("focus_window", "window")):
        d = Decision(kind=answer(kind, 0.9), item=None, site=answer("none"), **{target: answer("0", 0.3)})
        assert d.confidence == 0.3


# ------------------------------------------------------------------ double and right clicks

ITEM = Item(0, "Report.pdf", 1.0, 100.0, 100.0, 300.0, 140.0, role="cell", source="ax")


@pytest.fixture
def clicks(monkeypatch):
    log = []
    monkeypatch.setattr(desktop, "click_at", lambda point, clicks=1, right=False: log.append((point, clicks, right)))
    monkeypatch.setattr(desktop, "ax_press", lambda ref: pytest.fail("a double or right click is never a press"))
    return log


def test_a_double_click_is_two_clicks_on_the_pixel(screen, clicks):
    live = replace(screen, ax_refs={0: object()})
    assert (
        actions.perform(decision("double_click_item", item=answer("0")), live, [ITEM], context()) == "double-clicked 'Report.pdf'"
    )
    assert clicks == [((100.0, 60.0), 2, False)]


def test_a_right_click_opens_the_context_menu(screen, clicks):
    assert (
        actions.perform(decision("right_click_item", item=answer("0")), screen, [ITEM], context()) == "right-clicked 'Report.pdf'"
    )
    assert clicks == [((100.0, 60.0), 1, True)]


def test_a_click_variant_still_gates_on_the_item_it_would_land_on():
    d = Decision(kind=answer("double_click_item", 0.9), item=answer("3", 0.2), site=answer("none"))
    assert d.clicking and d.confidence == 0.2 and d.chosen == "double:3"


def test_a_double_click_that_could_not_be_aimed_is_refused(screen, monkeypatch):
    def miss(point, clicks=1, right=False):
        raise Missed("the cursor went elsewhere")

    monkeypatch.setattr(desktop, "click_at", miss)
    what = actions.perform(decision("double_click_item", item=answer("0")), screen, [ITEM], context())
    assert what.startswith("click refused: 'Report.pdf' was not double-clicked")


# ------------------------------------------------------------------ nothing offered that cannot run


def test_an_action_is_not_offered_when_it_has_no_target(screen):
    targets = target_criteria(screen, "Google Chrome")
    kinds = screen_kind_criteria("Google Chrome", None, screen, [], targets)
    assert not {"click_item", "double_click_item", "right_click_item", "open_app", "press_menu", "focus_window"} & set(kinds)
    assert set(targets) == {"key"}  # a key can always be pressed


def test_every_kind_that_can_be_offered_has_a_handler(screen):
    busy = replace(
        window_screen(screen),
        menu=[MenuItem("Format ▸ Bold")],
        field=TEXT,
        offscreen=[SimpleNamespace(label="x", role_word="link")],
    )
    targets = target_criteria(busy, "Google Chrome", APPS)
    kinds = screen_kind_criteria("Google Chrome", "me@example.com", busy, [ITEM], targets)
    handled = set(actions._HANDLERS) | {"click_item", "double_click_item", "right_click_item", "press_offscreen", "done", "none"}
    assert set(kinds) <= handled
    assert {"open_app", "press_menu", "focus_window", "press_key", "double_click_item", "right_click_item"} <= set(kinds)


# ------------------------------------------------------------------ what the capture reads


@pytest.fixture
def machine(monkeypatch):
    """Every desktop read the capture makes, answered here: a window on a second display to the left."""
    read = []
    monkeypatch.setattr(desktop, "frontmost_app_and_pid", lambda: ("Notes", 42))
    monkeypatch.setattr(desktop, "frontmost_window_bounds", lambda pid=None: (-1000.0, 100.0, 600.0, 400.0))
    monkeypatch.setattr(desktop, "active_displays", lambda: [(0.0, 0.0, 1512.0, 982.0), (-1280.0, 0.0, 1280.0, 800.0)])
    monkeypatch.setattr(
        desktop, "screenshot", lambda display=1: read.append(("screenshot", display)) or Image.new("RGB", (2560, 1600))
    )
    monkeypatch.setattr(desktop, "display_scale", lambda image, bounds=None: image.width / bounds[2])
    monkeypatch.setattr(desktop, "focused_field", lambda: None)
    monkeypatch.setattr(desktop, "browser_url", lambda browser: None)
    monkeypatch.setattr(desktop, "menu_items", lambda pid, limit: [MenuItem("Format ▸ Bold")])
    monkeypatch.setattr(desktop, "app_windows", lambda pid, limit=12: [WindowRef("Groceries", main=True)])
    monkeypatch.setattr(desktop, "running_apps", lambda: {"Notes", "Finder"})
    monkeypatch.setattr(desktop, "clipboard_text", lambda: read.append(("clipboard",)) or "copied text")
    return read


def test_the_display_holding_the_front_window_is_the_one_captured(machine):
    screen = perception.capture(browser="Safari")

    assert machine == [("screenshot", 2)]
    assert screen.origin == (-1280.0, 0.0) and screen.scale == 2.0
    assert screen.menu[0].path == "Format ▸ Bold" and screen.running == {"Notes", "Finder"}


def test_the_clipboard_is_not_read_unless_the_user_shares_it(machine):
    assert perception.capture(browser="Safari").clipboard == ""
    assert ("clipboard",) not in machine

    assert perception.capture(browser="Safari", clipboard=True).clipboard == "copied text"


def test_a_shared_clipboard_reaches_the_state_and_an_unshared_one_does_not(screen):
    assert "clipboard" not in base_state("g", screen, [], [])
    assert base_state("g", replace(screen, clipboard="x" * 900), [], [])["clipboard"] == "x" * 500


def test_a_point_on_no_display_falls_back_to_the_main_one():
    displays = [(0.0, 0.0, 1512.0, 982.0), (-1280.0, 0.0, 1280.0, 800.0)]
    assert perception.display_holding((-10.0, 10.0), displays) == 2
    assert perception.display_holding((9999.0, 9999.0), displays) == 1
    assert perception.display_holding(None, displays) == 1


def test_a_control_on_a_second_display_lands_on_its_capture(screen):
    from typesafe_computer_use.models import AxNode

    left = replace(screen, origin=(-1000.0, 0.0))
    node = AxNode(role="AXButton", label="Share", x=-900.0, y=50.0, w=40.0, h=20.0, pressable=True)
    [item] = perception.to_ax_items([node], left)
    assert (item.x1, item.y1, item.x2, item.y2) == (200.0, 100.0, 280.0, 140.0)
