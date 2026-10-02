"""OSWorld's Ubuntu accessibility tree, read offline from a hand-written fixture in its format."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from typesafe_computer_use.models import TEXT_ROLES
from typesafe_computer_use.osworld import a11y

FIXTURE = Path(__file__).parent / "fixtures" / "osworld" / "ubuntu-chrome.xml"
DISPLAY = (1920.0, 1080.0)
EMPTY = f'<desktop-frame xmlns:st="{a11y.NS_STATE}" xmlns:cp="{a11y.NS_COMPONENT}"/>'


@pytest.fixture
def tree():
    return a11y.parse(FIXTURE.read_text())


def with_focus_on(tree, tag):
    """The fixture with the focus moved from the address bar to the first `tag`."""
    focused = f"{{{a11y.NS_STATE}}}focused"
    for element in tree.iter():
        element.attrib.pop(focused, None)
    next(tree.iter(tag)).set(focused, "true")
    return tree


@pytest.mark.parametrize(
    ("tag", "role"),
    [
        ("push-button", "AXButton"),
        ("link", "AXLink"),
        ("entry", "AXTextField"),
        ("password-text", "AXSecureTextField"),
        ("check-box", "AXCheckBox"),
        ("combo-box", "AXComboBox"),
        ("page-tab", "AXTab"),
        ("list-item", "AXRow"),
        ("menu-item", "AXMenuItem"),
    ],
)
def test_atspi_roles_map_onto_ax_names(tag, role):
    assert a11y.ax_role(tag) == role


def test_a_password_is_never_a_text_role_and_other_roles_keep_their_name():
    assert a11y.ax_role("password-text") not in TEXT_ROLES
    assert a11y.ax_role("entry") in TEXT_ROLES
    assert a11y.ax_role("document-web") == "document-web"


def test_the_active_app_and_window_are_chrome(tree):
    assert a11y.name(a11y.active_app(tree)) == "Google Chrome"
    window = a11y.active_window(tree)
    assert a11y.name(window) == "Google - Google Chrome"
    assert a11y.frame(window) == (70.0, 27.0, 1850.0, 1053.0)


def test_the_app_holding_the_focus_is_active_when_no_window_says_so(tree):
    del next(tree.iter("frame")).attrib[f"{{{a11y.NS_STATE}}}active"]
    assert a11y.active_window(tree) is None
    assert a11y.name(a11y.active_app(tree)) == "Google Chrome"


def test_the_focused_field_is_the_address_bar(tree):
    field = a11y.focused_field(tree)
    assert field.role == "AXTextField"
    assert field.is_text
    assert field.label == "Address and search bar"
    assert field.placeholder == "Search Google or type a URL"
    assert field.value == "https://www.google.com/"
    assert (field.x, field.y, field.w, field.h) == (230.0, 78.0, 1560.0, 30.0)
    assert field.ref is None


def test_a_focused_password_is_not_text_and_its_value_is_never_read(tree):
    field = a11y.focused_field(with_focus_on(tree, "password-text"))
    assert field.label == "Password"
    assert not field.is_text
    assert field.value == ""


def test_the_url_is_the_address_bar_text(tree):
    assert a11y.browser_url(tree) == "https://www.google.com/"
    assert a11y.browser_url(tree, "Google Chrome") == "https://www.google.com/"
    assert a11y.browser_url(tree, "Firefox") is None


def test_the_walk_yields_chromes_on_screen_controls_with_no_refs(tree):
    found, offscreen, capped = a11y.walk(a11y.active_app(tree), *DISPLAY)
    assert [(n.role, n.label) for n in found] == [
        ("AXButton", "Back"),
        ("AXButton", "Reload"),
        ("AXTextField", "Address and search bar"),
        ("AXTab", "Google"),
        ("AXTextField", "Search"),
        ("AXLink", "Gmail"),
        ("AXLink", "Images"),
        ("AXRow", "Recent searches"),
    ]
    assert found[0].x == 78.0 and found[0].w == 34.0
    assert all(n.ref is None and not n.pressable for n in found)
    assert offscreen == []
    assert capped is False


def test_the_walk_leaves_out_off_screen_and_unshown_links_and_the_password(tree):
    labels = {n.label for n in a11y.walk(a11y.active_app(tree), *DISPLAY)[0]}
    assert not labels & {"Privacy", "Terms", "Password", "Google Chrome"}


def test_frames_read_tuple_strings(tree):
    link = next(e for e in tree.iter("link") if a11y.name(e) == "Privacy")
    assert a11y.frame(link) == (90.0, 2400.0, 52.0, 24.0)
    assert a11y.frame(next(e for e in tree.iter("link") if a11y.name(e) == "Terms")) is None


@pytest.mark.parametrize("xml", [None, "", b"", "<desktop-frame><application", "not xml"])
def test_no_tree_parses_to_none(xml):
    assert a11y.parse(xml) is None


@pytest.mark.parametrize("root", [None, a11y.parse(EMPTY)], ids=["none", "empty"])
def test_no_tree_and_an_empty_tree_yield_nothing_and_raise_nothing(root):
    assert a11y.active_app(root) is None
    assert a11y.active_window(root) is None
    assert a11y.focused_field(root) is None
    assert a11y.browser_url(root) is None
    assert a11y.walk(a11y.active_app(root), *DISPLAY) == ([], [], False)
    assert a11y.name(None) == ""
    assert a11y.label(None) == ""
    assert a11y.text(None) == ""
    assert a11y.frame(None) is None
    assert a11y.has_state(None, "focused") is False


def test_the_walk_of_an_empty_app_is_empty():
    app = a11y.parse('<desktop-frame><application name="Google Chrome"/></desktop-frame>')[0]
    assert a11y.walk(app, *DISPLAY) == ([], [], False)


# ----- a real tree, captured from OSWorld's VM -------------------------------------------------

CAPTURED = Path(__file__).parent / "fixtures" / "osworld" / "ubuntu-chrome-captured.xml"


@pytest.fixture
def captured():
    return a11y.parse(CAPTURED.read_text())


def test_the_captured_tree_names_chrome_as_the_active_app_over_the_shells_focus(captured):
    """GNOME Shell's own window holds `focused`, but Chrome's window is the active one."""
    assert a11y.name(a11y.active_app(captured)) == "Google Chrome"
    window = a11y.active_window(captured)
    assert a11y.name(window) == "New Tab - Google Chrome"
    assert a11y.frame(window) == (70.0, 27.0, 1850.0, 1053.0)


def test_the_captured_focus_is_the_new_tab_page_not_a_text_field(captured):
    field = a11y.focused_field(captured)
    assert field.role == "document-web"
    assert not field.is_text
    assert (field.x, field.y, field.w, field.h) == (70.0, 114.0, 1850.0, 966.0)


def test_the_captured_address_bar_is_chromes_omnibox_and_empty_on_a_new_tab(captured):
    (omnibox,) = [e for e in captured.iter("entry") if a11y.name(e) == a11y.ADDRESS_BAR]
    assert a11y.placeholder(omnibox) == "Search Google or type a URL"  # Chromium's attribute is `placeholder`
    assert a11y.browser_url(captured) is None  # the New Tab page shows no URL
    omnibox.text = "bing.com"
    assert a11y.browser_url(captured, "Google Chrome") == "bing.com"


def test_the_captured_omnibox_reads_as_a_text_field_with_its_placeholder_when_focused(captured):
    field = a11y.focused_field(with_focus_on(captured, "entry"))
    assert field.role == "AXTextField" and field.is_text
    assert field.label == a11y.ADDRESS_BAR
    assert field.placeholder == "Search Google or type a URL"


def test_the_captured_walk_goes_through_chromes_nested_panels(captured):
    """AT-SPI nests nameless panels of one frame; the walk once stopped at the first repeat."""
    found, offscreen, capped = a11y.walk(a11y.active_app(captured), *DISPLAY)
    labels = [(n.role, n.label) for n in found]
    for control in [
        ("AXButton", "Back"),
        ("AXButton", "Reload"),
        ("AXTextField", "Address and search bar"),
        ("AXButton", "Chrome"),  # the menu that leads to Settings
        ("AXTab", "New Tab - Memory usage - 56.2 MB"),
        ("AXComboBox", "Search Google or type a URL"),
        ("AXLink", "Gmail"),
        ("AXButton", "Restore"),  # the Restore pages? bubble, a second top-level window
        ("AXCheckBox", "Customise Chrome"),  # a toggle button
    ]:
        assert control in labels
    assert len(labels) == len(set((n.role, n.label, n.x, n.y) for n in found))  # no control twice
    chrome = next(n for n in found if n.label == "Chrome")
    assert (chrome.x, chrome.y, chrome.w, chrome.h) == (1881.0, 73.0, 39.0, 34.0)
    assert all(n.ref is None and not n.pressable for n in found)
    assert offscreen == [] and capped is False


def test_the_captured_walk_leaves_out_hidden_and_frameless_controls(captured):
    labels = {n.label for n in a11y.walk(a11y.active_app(captured), *DISPLAY)[0]}
    assert not labels & {"Home", "Extensions", "Save card", "Pin to toolbar", "Apps"}


SETTINGS = Path(__file__).parent / "fixtures" / "osworld" / "ubuntu-chrome-settings-captured.xml"


@pytest.fixture
def settings():
    return a11y.parse(SETTINGS.read_text())


def test_the_captured_settings_page_has_its_url_and_focus(settings):
    assert a11y.browser_url(settings) == "chrome://settings/appearance"
    field = a11y.focused_field(settings)
    assert (field.role, field.label) == ("AXMenuItem", "Appearance")
    assert not field.is_text


def test_the_settings_sidebar_entries_are_controls_each_with_its_own_frame(settings):
    """They are `menu-item`s whose one action is `select`, no control role on a Mac."""
    found = {n.label: n for n in a11y.walk(a11y.active_app(settings), *DISPLAY)[0]}
    for entry in ("Appearance", "Search engine", "Default browser", "On start-up"):
        assert found[entry].role == "AXMenuItem"
    engine = found["Search engine"]
    assert (engine.x, engine.y, engine.w, engine.h) == (71.0, 378.0, 247.0, 40.0)
    assert found["Show Home button"].role == "AXCheckBox"
    assert found["Font size"].role == "AXComboBox"
    assert not any(n.pressable or n.ref is not None for n in found.values())


NO_ACTIVE_WINDOW = Path(__file__).parent / "fixtures" / "osworld" / "ubuntu-chrome-no-active-window-captured.xml"


@pytest.fixture
def no_active_window():
    return a11y.parse(NO_ACTIVE_WINDOW.read_text())


def test_with_no_window_active_the_focus_is_the_field_in_the_app_holding_it(no_active_window):
    """OSWorld's chrome/2ae9ba84: GNOME Shell's window, focused in every tree and after Chrome in this
    one, passed for the focused field, so jev refused to type into Chrome's focused Name field and
    clicked it seven more times instead."""
    assert a11y.active_window(no_active_window) is None
    assert a11y.name(a11y.active_app(no_active_window)) == "Google Chrome"
    field = a11y.focused_field(no_active_window)
    assert (field.role, field.label, field.value) == ("AXTextField", "Name", "Person 1")
    assert field.is_text
    assert (field.x, field.y, field.w, field.h) == (717.0, 331.0, 269.0, 17.0)


def test_the_shells_focused_window_does_not_make_it_the_active_app_wherever_it_falls(no_active_window):
    shell = next(app for app in no_active_window if a11y.name(app) == "gnome-shell")
    no_active_window.remove(shell)
    no_active_window.insert(0, shell)
    assert a11y.name(a11y.active_app(no_active_window)) == "Google Chrome"
    assert a11y.focused_field(no_active_window).label == "Name"


def test_with_nothing_else_focused_the_shell_still_holds_the_focus(no_active_window):
    focused = f"{{{a11y.NS_STATE}}}focused"
    (name,) = [e for e in no_active_window.iter("entry") if e.get(focused) == "true"]
    del name.attrib[focused]
    assert a11y.name(a11y.active_app(no_active_window)) == "gnome-shell"
    assert a11y.focused_field(no_active_window).role == "window"


RESTORE_BUBBLE = Path(__file__).parent / "fixtures" / "osworld" / "ubuntu-chrome-restore-bubble-captured.xml"


@pytest.fixture
def restore_bubble():
    return a11y.parse(RESTORE_BUBBLE.read_text())


def test_the_page_control_under_the_restore_bubble_is_covered_by_it(restore_bubble):
    """OSWorld's chrome/2ad9387a: the bookmark manager's Organise button lies under the bubble's Close
    button. jev clicked Organise, which closed the bubble, saw no menu, and took two steps to recover."""
    found = a11y.walk(a11y.active_app(restore_bubble), *DISPLAY)[0]
    (organise,) = [n for n in found if n.covered_by is not None]
    assert (organise.role, organise.label) == ("AXButton", "Organise")
    bubble = organise.covered_by
    assert (bubble.name, bubble.x, bubble.y, bubble.w, bubble.h) == ("Restore pages?", 1592.0, 103.0, 334.0, 260.0)
    assert bubble.title == "'Restore pages?'"
    close = bubble.close
    assert (close.role, close.label, close.x, close.y, close.w, close.h) == ("AXButton", "Close", 1879.0, 128.0, 24.0, 22.0)


def test_the_bubbles_own_controls_are_not_covered_though_chrome_lists_the_bubble_twice(restore_bubble):
    """Chrome lists the bubble inside the browser window as well as beside it."""
    assert len(list(restore_bubble.iter("alert"))) == 2
    found = a11y.walk(a11y.active_app(restore_bubble), *DISPLAY)[0]
    own = [n for n in found if 1592 <= n.x < 1926 and 103 <= n.y < 363 and n.label != "Organise"]
    assert sorted(n.label for n in own) == [
        "Close",
        "Help make Google Chrome better by sending crash reports and usage statistics to Google",
        "Restore",
        "statistics",
        "usage",
    ]
    assert all(n.covered_by is None for n in own)


def chrome_with(*windows: str) -> ET.Element:
    """Chrome's browser window with a Save button centred on (900, 400), and `windows` listed after it."""
    return a11y.parse(
        f'<desktop-frame xmlns:st="{a11y.NS_STATE}" xmlns:cp="{a11y.NS_COMPONENT}"><application name="Google Chrome">'
        '<frame name="Page - Google Chrome" st:active="true" cp:screencoord="(0, 0)" cp:size="(1920, 1080)">'
        '<push-button name="Save" cp:screencoord="(880, 390)" cp:size="(40, 20)"/></frame>'
        f"{''.join(windows)}</application></desktop-frame>"
    )


def save(tree: ET.Element):
    return next(n for n in a11y.walk(a11y.active_app(tree), *DISPLAY)[0] if n.label == "Save")


def window(tag: str, name: str, x: int, *controls: str) -> str:
    """A window 300x200 with its top-left corner at (x, 300)."""
    return f'<{tag} name="{name}" cp:screencoord="({x}, 300)" cp:size="(300, 200)">{"".join(controls)}</{tag}>'


def control(tag: str, name: str) -> str:
    return f'<{tag} name="{name}" cp:screencoord="(820, 320)" cp:size="(200, 30)"/>'


def test_a_popup_with_no_close_button_is_closed_some_other_way_and_close_this_profile_is_not_one():
    covered_by = save(chrome_with(window("alert", "Profile", 800, control("push-button", "Close this profile")))).covered_by
    assert covered_by.name == "Profile" and covered_by.close is None


def test_a_nameless_window_with_a_control_is_a_popup_and_the_last_one_listed_is_on_top():
    """A menu is a nameless frame, opened after the bubble under it."""
    tree = chrome_with(
        window("alert", "Restore pages?", 800, control("push-button", "Restore")),
        window("frame", "", 750, control("menu-item", "Settings")),
    )
    popup = save(tree).covered_by
    assert (popup.name, popup.title, popup.x) == ("", "a popup", 750.0)


def test_a_window_with_no_control_covers_nothing():
    """Chrome's status bubble shows a link's address in a nameless window, and moves off the pointer."""
    assert save(chrome_with(window("frame", "", 800, control("static", "https://example.com/")))).covered_by is None


def test_a_control_beside_a_popup_is_not_covered_by_it():
    assert save(chrome_with(window("dialog", "Bookmark added", 1000, control("push-button", "Done")))).covered_by is None


def test_a_node_that_answers_a_click_is_a_control_and_one_that_only_has_a_default_is_not():
    tree = a11y.parse(
        f'<desktop-frame xmlns:st="{a11y.NS_STATE}" xmlns:cp="{a11y.NS_COMPONENT}" xmlns:act="{a11y.NS_ACTION}">'
        '<application name="Google Chrome"><frame name="Chrome" st:active="true" cp:screencoord="(0, 0)" cp:size="(800, 600)">'
        '<section name="Row" cp:screencoord="(10, 10)" cp:size="(200, 40)" act:click_desc=""/>'
        '<menu-item name="Settings" cp:screencoord="(10, 60)" cp:size="(200, 30)" act:doDefault_desc=""/>'
        '<heading name="Title" cp:screencoord="(10, 100)" cp:size="(200, 30)" act:doDefault_desc=""/>'
        '<static name="Link text" cp:screencoord="(10, 140)" cp:size="(200, 30)" act:clickAncestor_desc=""/>'
        '<password-text name="Password" cp:screencoord="(10, 180)" cp:size="(200, 30)" act:activate_desc=""/>'
        "</frame></application></desktop-frame>"
    )
    found = a11y.walk(a11y.active_app(tree), *DISPLAY)[0]
    assert [(n.role, n.label) for n in found] == [("section", "Row"), ("AXMenuItem", "Settings")]


@pytest.mark.parametrize(("lines", "role"), [("multi_line", "AXTextArea"), ("single_line", "AXTextField")])
def test_a_multi_line_entry_is_a_text_area_as_on_a_mac(lines, role):
    """A `textarea` is an entry that says it is multi-line: its lines are content, not one value."""
    tree = a11y.parse(
        f'<desktop-frame xmlns:st="{a11y.NS_STATE}" xmlns:cp="{a11y.NS_COMPONENT}">'
        '<application name="Google Chrome"><frame name="Chrome" st:active="true" cp:screencoord="(0, 0)" cp:size="(800, 600)">'
        f'<entry name="Comment" st:focused="true" st:{lines}="true" cp:screencoord="(10, 10)" cp:size="(400, 120)"/>'
        "</frame></application></desktop-frame>"
    )
    field = a11y.focused_field(tree)
    assert (field.role, field.label) == (role, "Comment")
    assert field.is_text
