"""What an action did, read off the screen before it and the one after: `outcome.change`.

The popup cases run on trees captured in one run of OSWorld's chrome/2ad9387a, each read by jev's
own capture and perception through the OSWorld adapter, as a run reads them. The rest are screens
made by hand, with and without a tree.
"""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from conftest import busy_page
from PIL import Image

from typesafe_computer_use.ax_walk import AxAttrs, walk_actionable
from typesafe_computer_use.config import MAX_OPTIONS
from typesafe_computer_use.models import Field, Item, Popup, Screen
from typesafe_computer_use.osworld.desktop import OSWorldDesktop
from typesafe_computer_use.outcome import change, glance
from typesafe_computer_use.perception import capture, perceive
from typesafe_computer_use.platform_adapter import using

FIXTURES = Path(__file__).parent / "fixtures" / "osworld"
BUBBLE = FIXTURES / "ubuntu-chrome-restore-bubble-captured.xml"  # the bookmark manager, "Restore pages?" over Organise
CLOSED = FIXTURES / "ubuntu-chrome-restore-bubble-closed-captured.xml"  # after a click on Organise the bubble took
MENU = FIXTURES / "ubuntu-chrome-organise-menu-captured.xml"  # after a second click, Organise's menu open
ADD_FOLDER = FIXTURES / "ubuntu-chrome-add-folder-captured.xml"  # after "Add new folder" in that menu


def png() -> bytes:
    out = BytesIO()
    busy_page((1920, 1080)).save(out, format="PNG")  # drawn under every control, so the tree's controls all count
    return out.getvalue()


SCREENSHOT = png()


def seen(tree: Path):
    """What a step's capture and perception make of a captured tree, with no text read off the pixels."""

    def no_step(actions):
        raise AssertionError("reading a screen takes no action")

    adapter = OSWorldDesktop({"screenshot": SCREENSHOT, "accessibility_tree": tree.read_text()}, lambda image: [], no_step)
    with using(adapter):
        screen = capture(browser="Google Chrome")
        items = perceive(screen, MAX_OPTIONS, "make a bookmarks bar folder called Favorites")
    return glance(screen, items)


# ----- popups, from captured trees ---------------------------------------------------------


def test_a_click_the_bubble_took_closed_the_bubble_and_opened_nothing():
    """The click meant for Organise that only closed "Restore pages?": what the history never said."""
    assert change(seen(BUBBLE), seen(CLOSED)) == "closed: alert 'Restore pages?'"


def test_a_menu_with_no_name_is_named_by_its_first_items():
    assert change(seen(CLOSED), seen(MENU)) == "opened: menu ('Sort by name', 'Add new bookmark', …)"


def test_what_opened_is_the_news_when_a_menu_item_opens_a_dialog():
    assert change(seen(MENU), seen(ADD_FOLDER)) == "opened: dialog 'Add folder'"
    assert change(seen(ADD_FOLDER), seen(CLOSED)) == "closed: dialog 'Add folder'"


def test_the_same_tree_twice_is_no_change():
    assert change(seen(MENU), seen(MENU)) == "no change"


def test_the_window_title_is_the_frame_behind_a_popup_window():
    assert seen(BUBBLE).title == "Bookmarks - Google Chrome"
    assert seen(BUBBLE).popups == {("alert", "Restore pages?"): "alert 'Restore pages?'"}


# ----- screens made by hand ----------------------------------------------------------------

POPUP = Popup("", 0.0, 0.0, 400.0, 300.0, role="menu")
SIDEBAR = Popup("", 0.0, 100.0, 120.0, 500.0, role="menu")
DIALOG = Popup("Search engine", 300.0, 200.0, 400.0, 300.0, role="dialog")


def screen_of(
    texts: list[str],
    url: str | None = "https://example.com/",
    title: str | None = "Example - Google Chrome",
    app: str = "Google Chrome",
    field: Field | None = None,
    within: dict[str, Popup] | None = None,
    controls: frozenset[str] = frozenset(),
):
    """A screen of one line per text, 40 px apart from y=200, below the clock's strip. `within` puts
    the items with those texts in a popup, as perception does for a control of the tree, and the
    texts in `controls` came from the tree; the rest were read off the pixels."""
    items = [
        Item(i, text, 1.0, 100.0, 200.0 + 40 * i, 500.0, 230.0 + 40 * i, source="ax" if text in controls else "ocr")
        for i, text in enumerate(texts)
    ]
    screen = Screen(image=Image.new("RGB", (2000, 1200)), scale=2.0, app=app, field=field, url=url, title=title)
    for text, popup in (within or {}).items():
        screen.popups.setdefault(popup, []).append(text)
    return glance(screen, items)


PAGE = ["Bookmarks", "Organise", "Search bookmarks", "Bookmarks bar", "Other bookmarks"]


def test_a_new_url_under_a_new_title_is_a_new_page():
    before = screen_of(PAGE, url=None, title="New Tab - Google Chrome")
    assert change(before, screen_of(PAGE, url="chrome://bookmarks", title="Bookmarks - Google Chrome")) == (
        "went to chrome://bookmarks"
    )


def test_a_new_url_under_the_same_title_is_text_in_the_address_bar():
    assert change(screen_of(PAGE), screen_of(PAGE, url="hello world")) == "no change"


def test_without_titles_a_new_url_is_a_new_page():
    assert change(screen_of(PAGE, title=None), screen_of(PAGE, url="https://example.com/b", title=None)) == (
        "went to https://example.com/b"
    )


def test_another_window_title_or_app_is_a_new_page():
    assert change(screen_of(PAGE, url=None, title="Downloads"), screen_of(PAGE, url=None, title="Documents")) == (
        "went to 'Documents'"
    )
    assert change(screen_of(PAGE), screen_of(PAGE, url=None, title="Documents", app="Finder")) == "went to Finder 'Documents'"


def test_a_window_with_no_title_is_still_on_the_page_behind_it():
    assert change(screen_of(PAGE, url=None, title="Report.pages"), screen_of(PAGE, url=None, title=None)) == "no change"


def test_items_that_came_and_went_are_named_and_capped():
    after = ["Bookmarks", "Organise", "Add new folder", "Import bookmarks", "Export bookmarks", "Bookmarks bar"]
    assert change(screen_of(PAGE), screen_of(after)) == (
        "new: 'Add new folder', 'Import bookmarks', …; gone: 'Search bookmarks', …"
    )


def test_a_control_that_came_is_named_before_text_and_a_lone_glyph_not_at_all():
    before = screen_of(PAGE, controls=frozenset({"Organise"}))
    after = screen_of([*PAGE, "中", "Your bookmarks sync", "Add new folder"], controls=frozenset({"Organise", "Add new folder"}))
    assert change(before, after) == "new: 'Add new folder', 'Your bookmarks sync'"


def test_a_tree_that_came_or_went_between_the_screens_says_nothing():
    """Chrome builds its tree only once asked, so a task's first capture may have none."""
    with_tree = screen_of([*PAGE, "Restore"], within={"Restore": DIALOG}, controls=frozenset({"Restore"}))
    assert change(screen_of(PAGE), with_tree) is None
    assert change(with_tree, screen_of(PAGE)) is None


def test_a_clock_that_ticked_is_no_change_as_the_stall_count_has_it():
    page = [*PAGE, "Sep 29 16:43", "Help", "Settings", "Profile", "Sync"]
    assert change(screen_of(page), screen_of([*PAGE, "Sep 29 16:44", "Help", "Settings", "Profile", "Sync"])) == "no change"


def test_a_long_label_is_cut_short():
    assert change(screen_of(["Home"]), screen_of(["Home", "Delete browsing data Delete history, cookies"])) == (
        "new: 'Delete browsing data De…'"
    )


def test_a_focus_that_moved_to_a_text_field_names_it():
    name = Field(role="AXTextField", label="Name", placeholder="", value="", x=0, y=0, w=10, h=10)
    row = Field(role="AXRow", label="Bookmarks bar", placeholder="", value="", x=0, y=0, w=10, h=10)
    assert change(screen_of(PAGE), screen_of(PAGE, field=name)) == "focused 'Name'"
    assert change(screen_of(PAGE), screen_of(PAGE, field=row)) == "only the focus moved"


def test_text_the_focused_field_took_is_what_it_now_holds():
    """OCR's reading of a one-line field is no item, so typing into one changes no line of the screen."""
    before = Field(role="AXTextField", label="Name", placeholder="", value="Person 1", x=0, y=0, w=10, h=10)
    after = replace(before, value="Thomas")
    assert change(screen_of(PAGE, field=before), screen_of(PAGE, field=after)) == "'Name' holds 'Thomas'"
    assert change(screen_of(PAGE, field=after), screen_of(PAGE, field=after)) == "no change"


def test_a_popup_that_opened_is_named_by_role_and_name():
    within = {"Google": DIALOG, "Bing": DIALOG}
    assert change(screen_of(PAGE), screen_of([*PAGE, "Google", "Bing"], within=within)) == "opened: dialog 'Search engine'"


def test_a_menu_the_modal_dialog_hid_did_not_open_when_the_dialog_closed():
    """Chrome's settings list down its side is a menu, and a modal dialog hides it from the tree."""
    sidebar = {"You and Google": SIDEBAR, "Autofill": SIDEBAR}
    with_dialog = screen_of(["Google", "Bing"], within={"Google": DIALOG, "Bing": DIALOG})
    back = screen_of(["You and Google", "Autofill", "Google"], within=sidebar)
    assert change(screen_of(["You and Google", "Autofill"], within=sidebar), with_dialog) == "opened: dialog 'Search engine'"
    assert change(with_dialog, back) == "closed: dialog 'Search engine'"


def test_a_menu_the_capture_has_not_drawn_yet_is_named_by_the_trees_items():
    """chrome/2ad9387a: the tree had Organise's six items while the capture showed one, and a line
    naming the menu by that one read to the answer model as the wrong menu."""
    labels = ["Sort by name", "Add new bookmark", "Add new folder", "Import bookmarks", "Export bookmarks", "Help Centre"]
    lagging = screen_of([*PAGE, "Sort by name"], within=dict.fromkeys(labels, POPUP), controls=frozenset({"Sort by name"}))
    assert (
        change(screen_of(PAGE, controls=frozenset({"Organise"})), lagging)
        == "opened: menu ('Sort by name', 'Add new bookmark', …)"
    )


def test_two_menus_with_no_name_are_told_apart_by_their_items():
    first = screen_of(["New tab", "History"], within={"New tab": POPUP, "History": POPUP})
    other = Popup("", 500.0, 0.0, 300.0, 300.0, role="menu")
    second = screen_of(
        ["Sort by name", "Add new folder", "Import"], within={t: other for t in ("Sort by name", "Add new folder", "Import")}
    )
    assert change(first, second) == "opened: menu ('Sort by name', 'Add new folder', …)"


# ----- the walk: which controls a popup holds ----------------------------------------------


def node(role, label="", frame=(10.0, 10.0, 100.0, 20.0), children=()):
    return {"role": role, "label": label, "frame": frame, "children": list(children)}


def walked(root):
    found, _, _ = walk_actionable(
        root, lambda n: n["children"], lambda n: AxAttrs(n["role"], n["label"], n["frame"]), lambda n: [], 1920.0, 1080.0
    )
    return {n.label: n.within for n in found}


@pytest.mark.parametrize("role, word", [("alert", "alert"), ("dialog", "dialog"), ("menu", "menu"), ("AXSheet", "dialog")])
def test_a_control_in_a_popup_is_within_the_innermost_one(role, word):
    tree = node(
        "application",
        frame=None,
        children=[
            node("AXButton", "Share"),
            node(
                role,
                "Outer",
                frame=(300.0, 300.0, 400.0, 300.0),
                children=[
                    node("AXButton", "OK", frame=(320.0, 320.0, 60.0, 20.0)),
                    node(
                        "menu",
                        "",
                        frame=(400.0, 400.0, 200.0, 100.0),
                        children=[node("AXButton", "Inner", frame=(410.0, 410.0, 60.0, 20.0))],
                    ),
                ],
            ),
        ],
    )
    within = walked(tree)
    assert within["Share"] is None
    assert (within["OK"].role, within["OK"].name) == (word, "Outer")
    assert (within["Inner"].role, within["Inner"].name, within["Inner"].x) == ("menu", "", 400.0)


def test_a_popup_role_with_no_frame_on_screen_is_no_popup():
    """Chrome keeps a closed dropdown's menu in the tree with no frame, under its selected option."""
    closed = node("menu", "", frame=None, children=[node("AXButton", "Medium", frame=(10.0, 10.0, 100.0, 20.0))])
    assert walked(node("application", frame=None, children=[closed])) == {"Medium": None}
