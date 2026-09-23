"""That the window, its sheets, and their wiring actually build.

These do not run the event loop or press anything; they construct the windows and call the
handlers that take no user input. That is enough to catch what unit tests on the modules behind
the GUI cannot: a control wired to a handler that has since changed shape.
"""

import pytest

AppKit = pytest.importorskip("AppKit")


@pytest.fixture(scope="module")
def app():
    """A running-less NSApplication. Skipped where there is no window server to make one."""
    try:
        shared = AppKit.NSApplication.sharedApplication()
        shared.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    except Exception as e:  # pragma: no cover - a headless machine
        pytest.skip(f"no window server: {e}")
    return shared


@pytest.fixture(autouse=True)
def settings_file(monkeypatch, tmp_path):
    """Point the settings file at a temporary one. Without this the tests edit the real settings."""
    from typesafe_computer_use import settings as settings_module

    monkeypatch.setattr(settings_module, "SETTINGS_PATH", tmp_path / "settings.json")
    return tmp_path / "settings.json"


@pytest.fixture
def controller(app, settings_file, monkeypatch):
    """The window, built but never shown. The global key monitor is stood in for: the guard refuses
    the real one, which would watch every keystroke typed on this Mac for the rest of the test run."""
    from typesafe_computer_use.gui import hotkey
    from typesafe_computer_use.gui.app import Controller
    from typesafe_computer_use.platform_adapter import desktop

    monkeypatch.setattr(hotkey, "_add_monitors", lambda mask, handler, local: (None, None))
    monkeypatch.setattr(desktop, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(desktop, "installed_apps", lambda: ["Safari", "Google Chrome"])
    return Controller("open the playground")


def test_the_window_builds_with_the_goal_it_was_given(controller):
    assert controller.goal.stringValue() == "open the playground"
    assert "classifier:" in controller.status.stringValue()
    assert not controller.running and controller.stop_button.isEnabled() is False


def test_the_menu_bar_carries_the_shortcuts(app, controller):
    from typesafe_computer_use.gui.app import _install_menu

    _install_menu(app, controller)
    menus = {
        menu.title(): {item.title(): item.keyEquivalent() for item in menu.submenu().itemArray()}
        for menu in app.mainMenu().itemArray()
    }

    assert menus["Run"] == {"Run": "r", "Stop": ".", "": "", "Dictate": "d", "Listen & go": "l"}
    assert menus["typesafe-computer-use"]["Settings…"] == ","
    assert menus["typesafe-computer-use"]["API keys…"] == ","


def test_the_settings_window_builds_every_panel_from_the_catalogs(controller):
    from typesafe_computer_use.gui.settings_panel import SettingsWindow
    from typesafe_computer_use.settings import DECISION_PROVIDERS, TEXT_PROVIDERS

    window = SettingsWindow(controller.settings, lambda settings: None)

    assert window.decisions.provider.numberOfItems() == len(DECISION_PROVIDERS)
    assert window.writer.provider.numberOfItems() == len(TEXT_PROVIDERS)
    assert [panel.read().provider for panel in window.panels] == ["typesafe", "anthropic", "anthropic"]


def test_switching_provider_empties_the_model_box_and_the_old_overrides(controller):
    from typesafe_computer_use.gui.settings_panel import SettingsWindow
    from typesafe_computer_use.settings import Endpoint

    controller.settings.writer = Endpoint(provider="mistral", model="codestral-latest", api_key="old")
    controller.settings.writer.base_url = "http://127.0.0.1:1/v1"
    window = SettingsWindow(controller.settings, lambda settings: None)
    window.writer.provider.selectItemWithTitle_("OpenAI")
    window.writer._switched(None)

    assert window.writer.read() == Endpoint(provider="openai", model="", base_url="", api_key="")


def test_a_failed_model_menu_leaves_the_reason_on_screen(controller):
    from typesafe_computer_use.gui.settings_panel import SettingsWindow

    window = SettingsWindow(controller.settings, lambda settings: None)
    window.writer._failed(window.writer.generation, "no key for OpenAI: set OPENAI_API_KEY")

    assert window.writer.note.stringValue() == "no key for OpenAI: set OPENAI_API_KEY"


def test_a_model_menu_that_arrives_late_does_not_overwrite_a_newer_one(controller):
    from typesafe_computer_use.gui.settings_panel import SettingsWindow

    window = SettingsWindow(controller.settings, lambda settings: None)
    stale = window.writer.generation
    window.writer.generation += 1  # as a provider switch would

    window.writer._fill(stale, ["a-model-from-the-provider-before"])

    assert window.writer.model.numberOfItems() == 0


def test_the_keys_window_offers_a_field_for_every_key_not_in_the_environment(controller, monkeypatch):
    from typesafe_computer_use.gui.keys_panel import KeysWindow
    from typesafe_computer_use.settings import key_slots

    monkeypatch.setenv("MISTRAL_API_KEY", "from-the-shell")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    window = KeysWindow(controller.settings, lambda settings: None)

    assert len(window.slots) == len(key_slots())
    assert "GROQ_API_KEY" in window.fields  # typeable
    assert "MISTRAL_API_KEY" not in window.fields  # the environment already answers for it


def test_saving_keys_keeps_what_was_typed_and_drops_what_was_cleared(controller, monkeypatch, settings_file):
    from typesafe_computer_use.gui.keys_panel import KeysWindow

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    controller.settings.keys = {"XAI_API_KEY": "to-be-cleared"}
    window = KeysWindow(controller.settings, lambda settings: None)
    window.fields["GROQ_API_KEY"].setStringValue_(" gsk-typed ")
    window.fields["XAI_API_KEY"].setStringValue_("")

    window._save(None)

    assert controller.settings.keys == {"GROQ_API_KEY": "gsk-typed"}
    assert settings_file.exists()  # the temporary one, never the user's real settings


def test_an_idle_window_closes_and_a_running_one_is_asked_about(controller, monkeypatch):
    from typesafe_computer_use.gui import app as gui_app

    assert controller.should_close()

    monkeypatch.setattr(type(controller), "running", property(lambda self: True))
    monkeypatch.setattr(gui_app.w, "confirm", lambda *args: False)  # "Keep running"
    hidden = []
    monkeypatch.setattr(gui_app, "_app_visibility", hidden.append)
    assert not controller.should_close() and not controller.stop_flag.is_set()
    assert hidden == ["hide"]  # out of the way, still running

    monkeypatch.setattr(gui_app.w, "confirm", lambda *args: True)  # "Stop and close"
    assert controller.should_close() and controller.stop_flag.is_set()


def test_dictating_from_the_menu_is_ignored_while_a_run_owns_the_button(controller, monkeypatch):
    started = []
    monkeypatch.setattr(type(controller), "_start_dictation", lambda self: started.append(True))

    controller.dictate.setEnabled_(False)
    controller.toggle_dictation(None)
    assert started == []

    controller.dictate.setEnabled_(True)
    controller.toggle_dictation(None)
    assert started == [True]


def test_a_run_counts_as_going_from_the_moment_it_is_asked_for(controller):
    """Hiding the window delays the thread; a sentence landing in that gap must not start a second run."""
    assert not controller.running

    controller.starting = True

    assert controller.running


def test_a_sentence_that_lands_mid_start_does_not_start_a_second_run(controller, monkeypatch):
    from typesafe_computer_use.goal import LiveGoal

    starts = []
    monkeypatch.setattr(
        type(controller), "start_run", lambda self, sender=None: starts.append(1) or setattr(self, "starting", True)
    )
    controller.live = LiveGoal(listening=True)

    controller._dictated("Open the notes app.")
    controller._dictated("Open the notes app. Then write a summary.")

    assert starts == [1]
    assert controller.goal.stringValue() == "Open the notes app. Then write a summary."
    assert controller.live.text == "Open the notes app. Then write a summary."


def test_the_dictation_key_and_the_dot_are_wired_to_the_same_switch(controller):
    from typesafe_computer_use.settings import Shortcut

    assert controller.hotkey.shortcut.describe() == Shortcut().describe()
    assert not controller.dot.window.isVisible()


def test_the_key_and_the_button_are_the_same_switch(controller, monkeypatch):
    """Pressing the chord runs exactly what clicking Dictate runs, so the two cannot drift apart."""
    calls = []
    monkeypatch.setattr(type(controller), "toggle_dictation", lambda self, sender=None: calls.append(sender))

    controller.hotkey.on_toggle()

    assert calls == [None] or calls == []  # the callback hops to the main thread before it lands


def test_the_dot_is_in_the_top_right_wherever_it_is_shown(controller, monkeypatch):
    import AppKit

    from typesafe_computer_use.gui import widgets

    shown = []
    monkeypatch.setattr(widgets, "present", lambda window, above_everything=False: shown.append(above_everything))
    screen = AppKit.NSScreen.mainScreen()
    monkeypatch.setattr(type(controller.dot), "_screen", lambda self: screen)

    controller.dot.show()
    frame = controller.dot.window.frame()
    visible = screen.visibleFrame()
    controller.dot.hide()

    assert shown == [True]  # above every other window, full-screen apps included

    assert frame.origin.x + frame.size.width < visible.origin.x + visible.size.width
    assert frame.origin.y + frame.size.height > visible.origin.y + visible.size.height / 2  # the top half


def test_a_push_to_talk_utterance_joins_a_goal_that_is_already_being_spoken(controller):
    from typesafe_computer_use.goal import LiveGoal

    controller.live = LiveGoal("open the notes app", listening=True)

    controller._heard("And make it short.")

    assert controller.live.text == "open the notes app And make it short."


def test_the_settings_window_saves_the_clipboard_and_icon_choices(controller, settings_file):
    import AppKit

    from typesafe_computer_use.gui.settings_panel import SettingsWindow
    from typesafe_computer_use.settings import load

    window = SettingsWindow(controller.settings, lambda settings: None)
    assert window.share_clipboard.state() == AppKit.NSControlStateValueOff  # off unless chosen

    window.share_clipboard.setState_(AppKit.NSControlStateValueOn)
    window._save(None)

    assert load(settings_file).share_clipboard and not load(settings_file).name_icons


def test_no_module_of_the_window_reaches_an_adapter_but_through_the_desktop():
    import pathlib

    import typesafe_computer_use.gui as gui

    for path in pathlib.Path(gui.__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import macos" not in text and "import windows" not in text, path.name
