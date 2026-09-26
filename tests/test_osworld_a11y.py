"""OSWorld's Ubuntu accessibility tree, read offline from a hand-written fixture in its format."""

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
