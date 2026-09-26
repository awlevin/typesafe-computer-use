from dataclasses import replace

import pytest

from typesafe_computer_use import perception
from typesafe_computer_use.ax_walk import AxAttrs, walk_actionable
from typesafe_computer_use.models import AxNode, Item
from typesafe_computer_use.platform_adapter import desktop

DISPLAY = (1728.0, 1117.0)


def node(role, label="", frame=(10.0, 10.0, 100.0, 20.0), press=False, children=()):
    return {"role": role, "label": label, "frame": frame, "press": press, "children": list(children)}


def walk(root, **kwargs):
    return walk_actionable(
        root,
        lambda n: n["children"],
        lambda n: AxAttrs(n["role"], n["label"], n["frame"]),
        lambda n: ["AXPress"] if n["press"] else [],
        *DISPLAY,
        **kwargs,
    )


def labels(root, **kwargs):
    found, _offscreen, capped = walk(root, **kwargs)
    return [n.label for n in found], capped


def offscreen(root, **kwargs):
    return [(n.role, n.label) for n in walk(root, **kwargs)[1]]


def app(*children):
    """An application element reports a zero-size frame at the bottom of the display, not a real one."""
    return node("AXApplication", label="Finder", frame=(0.0, 1117.0, 0.0, 0.0), children=children)


def test_keeps_labelled_controls_and_reports_no_cap():
    found, _offscreen, capped = walk(app(node("AXButton", "Share"), node("AXLink", "Pricing", frame=(0.0, 40.0, 60.0, 16.0))))
    assert [(n.role, n.label, n.x, n.w) for n in found] == [("AXButton", "Share", 10.0, 100.0), ("AXLink", "Pricing", 0.0, 60.0)]
    assert capped is False


def test_a_frameless_root_does_not_prune_the_whole_tree():
    assert labels(node("AXApplication", "Finder", frame=None, children=[node("AXButton", "Share")]))[0] == ["Share"]


def test_drops_unlabelled_controls():
    assert labels(app(node("AXButton", "")))[0] == []


def test_skips_nameless_group_even_when_pressable():
    tree = app(node("AXGroup", "", press=True), node("AXGroup", "Toolbar", press=True))
    assert labels(tree)[0] == ["Toolbar"]


def test_press_action_makes_an_unlisted_role_actionable():
    assert labels(app(node("AXStaticText", "Sign in", press=True)))[0] == ["Sign in"]
    assert labels(app(node("AXStaticText", "Sign in")))[0] == []


def test_prunes_slivers_and_offscreen_frames():
    tree = app(
        node("AXLink", "clamped", frame=(100.0, 125.0, 72.0, 1.0)),
        node("AXLink", "narrow", frame=(100.0, 125.0, 2.0, 30.0)),
        node("AXLink", "below the display", frame=(100.0, 40000.0, 200.0, 30.0)),
        node("AXLink", "above the display", frame=(100.0, -300.0, 200.0, 30.0)),
        node("AXLink", "on screen", frame=(100.0, 125.0, 72.0, 30.0)),
    )
    assert labels(tree)[0] == ["on screen"]


def test_offscreen_container_prunes_its_whole_subtree():
    row = node("AXRow", "Note 900", frame=(1085.0, 42718.0, 280.0, 68.0), children=[node("AXButton", "Delete")])
    assert labels(app(row))[0] == []


def test_a_scrolled_out_link_is_collected_off_screen_without_joining_the_items():
    tree = app(
        node("AXLink", "Register Now", frame=(320.0, -4200.0, 120.0, 32.0), press=True),
        node("AXLink", "clamped to a sliver", frame=(100.0, 125.0, 72.0, 1.0), press=True),
        node("AXLink", "on screen", frame=(100.0, 125.0, 72.0, 30.0), press=True),
    )
    found, hidden, capped = walk(tree)
    assert [n.label for n in found] == ["on screen"]
    assert [(n.role, n.label, n.pressable) for n in hidden] == [
        ("AXLink", "Register Now", True),
        ("AXLink", "clamped to a sliver", True),
    ]
    assert hidden[0].ref is tree["children"][0] and capped is False


def test_an_unlabelled_or_unpressable_off_screen_node_is_not_collected():
    tree = app(
        node("AXLink", "", frame=(320.0, -4200.0, 120.0, 32.0), press=True),
        node("AXRow", "Note 900", frame=(1085.0, 42718.0, 280.0, 68.0)),
        node("AXGroup", "", frame=(0.0, -900.0, 400.0, 80.0), press=True),
    )
    assert offscreen(tree) == []


def test_a_giant_frame_crossing_the_display_but_centred_off_it_never_becomes_an_item():
    """A Chromium `shell` starts thousands of points above the viewport yet crosses it: `off_display`
    alone calls it partially visible, and a click on its centre would land far off the screen. It is
    no item, but the controls inside it that are on screen still are."""
    shell = node(
        "AXGroup",
        "shell",
        frame=(0.0, -13312.0, 1728.0, 20000.0),
        press=True,
        children=[
            node("AXButton", "Submit", frame=(100.0, 500.0, 120.0, 30.0), press=True),
            node("AXButton", "Footer link", frame=(100.0, 6000.0, 120.0, 30.0), press=True),
        ],
    )
    found, hidden, _capped = walk(app(shell))
    assert [n.label for n in found] == ["Submit"]
    assert [n.label for n in hidden] == ["shell", "Footer link"]


def test_a_control_centred_past_the_edge_is_pressed_rather_than_clicked():
    half = node("AXButton", "Half out", frame=(1650.0, 100.0, 200.0, 30.0), press=True)
    found, hidden, _capped = walk(app(half, node("AXButton", "Share")))
    assert [n.label for n in found] == ["Share"]
    assert [n.label for n in hidden] == ["Half out"]


def test_an_off_display_container_still_yields_its_pressable_children():
    row = node(
        "AXRow",
        "Note 900",
        frame=(1085.0, 42718.0, 280.0, 68.0),
        press=True,
        children=[node("AXButton", "Delete", frame=(1300.0, 42730.0, 40.0, 40.0), press=True)],
    )
    found, hidden, _capped = walk(app(row))
    assert found == [] and [n.label for n in hidden] == ["Note 900", "Delete"]


def test_the_offscreen_cap_stops_collection_and_leaves_the_on_screen_walk_alone():
    rows = [node("AXRow", f"Note {i}", frame=(1085.0, 4000.0 + 70.0 * i, 280.0, 68.0), press=True) for i in range(8)]
    tree = app(node("AXButton", "New Note", press=True), *rows)
    found, hidden, capped = walk(tree, offscreen_cap=3)
    assert [n.label for n in hidden] == ["Note 0", "Note 1", "Note 2"]
    assert [n.label for n in found] == ["New Note"] and capped is False


def test_skips_closed_menu_subtrees_but_keeps_the_menu_bar_item():
    menu = node(
        "AXMenu", "", frame=(0.0, 1117.0, 0.0, 0.0), children=[node("AXMenuItem", "New Folder", frame=(0.0, 0.0, 100.0, 20.0))]
    )
    bar_item = node("AXMenuBarItem", "File", frame=(50.0, 0.0, 34.0, 24.0), children=[menu])
    assert labels(app(bar_item))[0] == ["File"]


def test_decorative_child_does_not_repeat_its_parents_label():
    button = node("AXButton", "Add reaction", children=[node("AXImage", "", frame=(20.0, 12.0, 16.0, 16.0))])
    assert labels(app(button))[0] == ["Add reaction"]


def test_child_recovers_the_label_of_a_parent_that_was_not_emitted():
    cell = node("AXCell", "Inbox", frame=(10.0, 10.0, 100.0, 2.0), children=[node("AXImage", "", frame=(10.0, 40.0, 20.0, 20.0))])
    found, _offscreen, _capped = walk(app(cell))
    assert [(n.role, n.label) for n in found] == [("AXImage", "Inbox")]


def test_window_title_does_not_leak_onto_its_buttons():
    window = node(
        "AXWindow", "Notes", frame=(0.0, 0.0, 900.0, 600.0), children=[node("AXButton", "", frame=(882.0, 56.0, 16.0, 16.0))]
    )
    assert labels(app(window))[0] == []


def test_row_recovers_its_label_from_a_shallow_static_text():
    child_text = node("AXRow", "", children=[node("AXStaticText", "Projects", frame=(12.0, 12.0, 80.0, 16.0))])
    grandchild_text = node(
        "AXRow",
        "",
        frame=(10.0, 40.0, 100.0, 20.0),
        children=[node("AXGroup", "", children=[node("AXStaticText", "Downloads", frame=(12.0, 42.0, 80.0, 16.0))])],
    )
    assert labels(app(child_text, grandchild_text))[0] == ["Projects", "Downloads"]


def test_node_cap_stops_the_walk_and_is_reported():
    wide = app(*[node("AXButton", f"b{i}", frame=(10.0 * i, 10.0, 8.0, 20.0)) for i in range(20)])
    found, _offscreen, capped = walk(wide, node_cap=5)
    assert capped is True and len(found) == 4  # the application element itself costs one visit


def test_time_cap_stops_the_walk_and_is_reported():
    ticks = iter([0.0] + [0.1 * i for i in range(1, 40)])
    wide = app(*[node("AXButton", f"b{i}", frame=(10.0 * i, 10.0, 8.0, 20.0)) for i in range(20)])
    found, _offscreen, capped = walk(wide, time_cap=0.5, clock=lambda: next(ticks))
    assert capped is True and 0 < len(found) < 20


def test_ax_items_convert_points_to_capture_pixels_and_name_the_role(screen, monkeypatch):
    nodes = [
        AxNode(role="AXPopUpButton", label="View site information", x=126.0, y=89.0, w=24.0, h=24.0, pressable=True),
        AxNode(role="AXDisclosureTriangle", label="More", x=10.0, y=10.0, w=12.0, h=12.0, pressable=True),
    ]
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: (nodes, [], False))
    items = perception.ax_items(replace(screen, pid=123), 255)
    assert [(it.role, it.source, it.text) for it in items] == [
        ("popup", "ax", "View site information"),
        ("other", "ax", "More"),
    ]
    assert (items[0].x1, items[0].y1, items[0].x2, items[0].y2) == (252.0, 178.0, 300.0, 226.0)


def test_ax_items_are_skipped_without_a_pid_and_when_the_walk_raises(screen, monkeypatch):
    def boom(pid, w, h):
        raise RuntimeError("accessibility said no")

    monkeypatch.setattr(desktop, "actionable_elements", boom)
    assert perception.ax_items(screen, 255) == []
    assert perception.ax_items(replace(screen, pid=123), 255) == []


def test_the_walker_keeps_a_handle_to_every_element_it_reports():
    button = node("AXButton", "Share")
    found, _offscreen, _capped = walk(app(button))
    assert [n.ref for n in found] == [button]
    assert found[0] == AxNode(role="AXButton", label="Share", x=10.0, y=10.0, w=100.0, h=20.0, pressable=False)


def test_ax_refs_follow_items_through_the_merge_and_the_renumbering(screen, monkeypatch):
    left, right = object(), object()
    nodes = [
        AxNode(role="AXButton", label="Right", x=400.0, y=50.0, w=60.0, h=20.0, pressable=True, ref=right),
        AxNode(role="AXLink", label="Left", x=50.0, y=52.0, w=60.0, h=20.0, pressable=True, ref=left),
    ]
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: (nodes, [], False))
    monkeypatch.setattr(
        perception,
        "ocr",
        lambda screen, budget, goal, *_: [
            Item(0, "Left", 0.9, 100.0, 104.0, 220.0, 140.0),  # names the control, so the two merge
            Item(1, "Unrelated text", 0.9, 100.0, 400.0, 300.0, 430.0),
        ],
    )
    live = replace(screen, pid=123)
    items = perception.perceive(live, 255, "goal")
    assert [(it.index, it.text, it.source) for it in items] == [
        (0, "Left", "ax+ocr"),
        (1, "Right", "ax"),
        (2, "Unrelated text", "ocr"),
    ]
    assert live.ax_refs == {0: left, 1: right}


def test_offscreen_controls_are_deduplicated_and_never_repeat_a_visible_item(screen, monkeypatch):
    hidden = [
        AxNode(role="AXRow", label="Note 900", x=0.0, y=42718.0, w=280.0, h=68.0, pressable=True, ref=object()),
        AxNode(role="AXRow", label="Note 900", x=0.0, y=48000.0, w=280.0, h=68.0, pressable=True, ref=object()),
        AxNode(role="AXButton", label="Note 900", x=0.0, y=48000.0, w=40.0, h=40.0, pressable=True, ref=object()),
        AxNode(role="AXLink", label="Only text", x=0.0, y=-900.0, w=60.0, h=20.0, pressable=True, ref=object()),
    ]
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: ([], hidden, False))
    monkeypatch.setattr(perception, "ocr", lambda screen, budget, goal, *_: [Item(0, "Only text", 0.9, 10.0, 10.0, 90.0, 40.0)])
    live = replace(screen, pid=123)
    perception.perceive(live, 255, "goal")
    assert [(n.role, n.label) for n in live.offscreen] == [("AXRow", "Note 900"), ("AXButton", "Note 900")]


def test_offscreen_controls_are_empty_in_replay(screen, monkeypatch):
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: pytest.fail("no pid to walk"))
    monkeypatch.setattr(perception, "ocr", lambda screen, budget, goal, *_: [])
    perception.perceive(screen, 255, "goal")
    assert screen.offscreen == []


def test_ax_refs_are_empty_without_an_accessibility_tree(screen, monkeypatch):
    monkeypatch.setattr(perception, "ocr", lambda screen, budget, goal, *_: [Item(0, "Only text", 0.9, 10.0, 10.0, 90.0, 40.0)])
    items = perception.perceive(screen, 255, "goal")
    assert [it.source for it in items] == ["ocr"] and screen.ax_refs == {}


def test_a_subtree_repeated_under_several_parents_is_walked_once():
    """Ghostty hangs its menu bar under every window: same role, label and frame, new objects each time."""
    tree = {
        "app": ["win1", "win2", "win3"],
        "win1": ["bar1"],
        "win2": ["bar2"],
        "win3": ["bar3"],
        "bar1": ["file1"],
        "bar2": ["file2"],
        "bar3": ["file3"],
        "file1": [],
        "file2": [],
        "file3": [],
    }
    frames = {"app": None, "win1": (0, 40, 800, 600), "win2": (0, 40, 800, 600), "win3": (0, 40, 800, 600)}
    for b in ("bar1", "bar2", "bar3"):
        frames[b] = (0, 0, 800, 24)
    for f in ("file1", "file2", "file3"):
        frames[f] = (40, 0, 30, 24)
    roles = {"app": "AXApplication", "win1": "AXWindow", "win2": "AXWindow", "win3": "AXWindow"}
    labels = {"file1": "File", "file2": "File", "file3": "File"}

    def attrs(n):
        return AxAttrs(roles.get(n, "AXMenuBar" if n.startswith("bar") else "AXMenuBarItem"), labels.get(n, ""), frames[n])

    found, _, cap_hit = walk_actionable("app", lambda n: tree[n], attrs, lambda n: ["AXPress"], 1000, 800)
    assert [n.label for n in found] == ["File"]
    assert not cap_hit


def test_a_wrapper_keyed_like_its_parent_is_walked_into_and_not_listed_twice():
    """AT-SPI nests nameless panels of one frame, and a labelled control can wrap its own twin.
    Neither is a copy from elsewhere: the walk goes through them, and keeps one of the twins."""
    tree = {
        "app": ["outer"],
        "outer": ["inner"],
        "inner": ["innermost"],
        "innermost": ["button"],
        "button": ["twin"],
        "twin": [],
    }
    frames = {"app": None, "outer": (0, 0, 800, 600), "inner": (0, 0, 800, 600), "innermost": (0, 0, 800, 600)}
    frames["button"] = frames["twin"] = (10, 10, 60, 24)
    roles = {"app": "AXApplication", "outer": "AXGroup", "inner": "AXGroup", "innermost": "AXGroup"}

    def attrs(n):
        return AxAttrs(roles.get(n, "AXButton"), "OK" if n in ("button", "twin") else "", frames[n])

    found, _, cap_hit = walk_actionable("app", lambda n: tree[n], attrs, lambda n: [], 1000, 800)
    assert [(n.label, n.ref) for n in found] == [("OK", "button")]
    assert not cap_hit


def test_an_app_that_lists_itself_as_a_child_terminates():
    tree = {"app": ["app", "app", "bar"], "bar": ["file"], "file": []}
    frames = {"app": None, "bar": (0, 0, 800, 24), "file": (40, 0, 30, 24)}
    roles = {"app": "AXApplication", "bar": "AXMenuBar", "file": "AXMenuBarItem"}

    def attrs(n):
        return AxAttrs(roles[n], "File" if n == "file" else "", frames[n])

    found, _, cap_hit = walk_actionable("app", lambda n: tree[n], attrs, lambda n: ["AXPress"], 1000, 800)
    assert [n.label for n in found] == ["File"]
    assert not cap_hit
