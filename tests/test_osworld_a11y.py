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
