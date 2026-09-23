"""The Settings window: which provider answers which question, and how dictation is done.

Nothing here knows any provider's details. The panels are built from `settings.TEXT_PROVIDERS` and
`settings.DECISION_PROVIDERS`, and each model menu is filled by asking that provider what it
serves, so a provider added to those catalogs appears in this window, with its real catalog of
models, without a line changing here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

import AppKit
from Foundation import NSMakeRect, NSOperationQueue

from .. import voice as voice_module
from ..platform_adapter import desktop
from ..session import list_models
from ..settings import DECISION_PROVIDERS, TEXT_PROVIDERS, Endpoint, ProviderSpec, Settings, SettingsError, Shortcut
from . import widgets as w
from .keys_panel import KeysWindow

# MLX ports of Whisper, smallest first. Hugging Face has no catalog to ask, unlike every provider
# below, so this list is written out; any repo id can be typed in instead.
WHISPER_MODELS = [
    "mlx-community/whisper-tiny",
    "mlx-community/whisper-base",
    "mlx-community/whisper-small",
    "mlx-community/whisper-medium",
    "mlx-community/whisper-large-v3-turbo",
    "mlx-community/whisper-large-v3",
]

# Browsers worth offering, by the name their application bundle carries. Only those this Mac has are
# listed; any other can still be typed.
BROWSERS = (
    "Safari",
    "Google Chrome",
    "Firefox",
    "Microsoft Edge",
    "Arc",
    "Brave Browser",
    "Opera",
    "Vivaldi",
    "Orion",
    "Chromium",
    "Zen",
    "DuckDuckGo",
    "Tor Browser",
)

NO_DEFAULT = "the provider's default"  # shown when a provider picks the model itself, as TypeSafe does

PANEL_HEIGHT = 78  # a header, one row of controls, and the line that reports on them
VOICE_HEIGHT = 104
WIDTH = 680
MARGIN = 20


class EndpointPanel:
    """One service's two choices: the provider, and which of its models.

    There is nothing else to fill in. A base URL and an API key are either the provider's own
    defaults or come from the environment, which beats anything typed here anyway, so the panel
    reports on them in one line rather than asking for them.
    """

    def __init__(self, parent, title: str, endpoint: Endpoint, catalog: dict[str, ProviderSpec], y: float, keys: dict[str, str]):
        self.endpoint = endpoint  # the saved endpoint, so overrides it carries survive being edited
        self.catalog = catalog
        self.keys = keys  # the live key store, so a key entered in the Keys window is used at once
        self.by_label = {spec.label: key for key, spec in catalog.items()}
        self.generation = 0  # replies from a provider the user has already switched away from are dropped
        spec = endpoint.spec(catalog)
        right = WIDTH - MARGIN

        w.label(parent, title, MARGIN, y, right - MARGIN, 16, font=w.BOLD)
        self.provider = w.popup(
            parent, [s.label for s in catalog.values()], spec.label, MARGIN, y + 21, 190, handler=self._switched
        )
        self.model = w.combo(
            parent, endpoint.model, [], MARGIN + 198, y + 22, right - MARGIN - 198, placeholder=spec.default_model or NO_DEFAULT
        )
        self.note = w.label(
            parent, "", MARGIN, y + 52, right - MARGIN, 15, font=w.SMALL, color=AppKit.NSColor.secondaryLabelColor()
        )

    # --- reading the controls ----------------------------------------------------------------

    def read(self) -> Endpoint:
        """What is on screen. Switching provider drops the old one's overrides; staying keeps them."""
        provider = self.by_label.get(self.provider.titleOfSelectedItem(), self.endpoint.provider)
        model = self.model.stringValue().strip()
        if provider != self.endpoint.provider:
            return Endpoint(provider=provider, model=model)
        return replace(self.endpoint, model=model)

    def spec(self) -> ProviderSpec:
        return self.read().spec(self.catalog)

    # --- the model menu ------------------------------------------------------------------------

    def refresh(self) -> None:
        """Ask the provider what it serves, off the main thread, and fill the menu with the answer."""
        endpoint, spec = self.read(), self.spec()
        self.generation += 1
        mine = self.generation
        self.note.setStringValue_(f"asking {spec.label} for its models…")

        def work() -> None:
            """`message` is bound in the except block because the name is gone once it ends."""
            try:
                models = list_models(endpoint, self.catalog, self.keys)
            except SettingsError as e:
                message = str(e)
                NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: self._failed(mine, message))
                return
            NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: self._fill(mine, models))

        threading.Thread(target=work, daemon=True).start()

    def _fill(self, generation: int, models: list[str]) -> None:
        if generation != self.generation:
            return
        spec = self.spec()
        self.model.removeAllItems()
        self.model.addItemsWithObjectValues_(models)
        current = self.model.stringValue().strip()
        if not current and spec.default_model in models:
            self.model.setStringValue_(spec.default_model)  # show what the run would actually use
        where = self.read().resolved_base_url(self.catalog) or "the provider's own endpoint"
        self.note.setStringValue_(f"{len(models)} models from {where}")

    def _failed(self, generation: int, message: str) -> None:
        """Leave the box typeable and say why the menu is empty: a missing key is the usual reason."""
        if generation != self.generation:
            return
        self.note.setStringValue_(message)

    def _switched(self, sender) -> None:
        """A new provider brings its own models, so the box is emptied and the menu asked for again."""
        spec = self.spec()
        self.model.setStringValue_("")
        self.model.removeAllItems()
        self.model.setPlaceholderString_(spec.default_model or NO_DEFAULT)
        self.refresh()


def installed_browsers() -> list[str]:
    """The browsers this Mac actually has, so the setting cannot name one that is not there."""
    apps = set(desktop.installed_apps())
    return [name for name in BROWSERS if name in apps]


class ShortcutField:
    """The dictation chord: what it is now, and a button that records a new one.

    Recording is a local event monitor that swallows one keystroke, so pressing Command-Q to set
    the shortcut sets the shortcut instead of quitting.
    """

    def __init__(self, parent, shortcut: Shortcut, x: float, y: float):
        self.shortcut = shortcut
        self.monitor = None
        w.label(parent, "press", x, y + 4, 34, 15, font=w.SMALL)
        self.button = w.button(parent, shortcut.describe(), self._record, x + 38, y, 88, 26)
        self.button.setToolTip_("Click, then press the keys you want to start and stop dictation with")
        self.enabled = w.checkbox(parent, "anywhere", shortcut.enabled, x + 132, y + 3, 90)

    def read(self) -> Shortcut:
        return replace(self.shortcut, enabled=self.enabled.state() == AppKit.NSControlStateValueOn)

    def _record(self, sender) -> None:
        if self.monitor is not None:
            return
        self.button.setTitle_("press keys\u2026")
        mask = AppKit.NSEventMaskKeyDown
        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, self._captured)

    def _captured(self, event):
        """Take the chord and swallow the event, so it does nothing else on its way past."""
        from .hotkey import RELEVANT, held

        command, control, option, shift = held(event.modifierFlags() & RELEVANT)
        character = (event.charactersIgnoringModifiers() or "").strip() or f"key {event.keyCode()}"
        self.shortcut = replace(
            self.shortcut,
            key_code=event.keyCode(),
            character=character,
            command=command,
            control=control,
            option=option,
            shift=shift,
        )
        self.button.setTitle_(self.shortcut.describe())
        self._stop()
        return None

    def _stop(self) -> None:
        if self.monitor is not None:
            AppKit.NSEvent.removeMonitor_(self.monitor)
            self.monitor = None


class SettingsWindow:
    """The window itself. It edits a copy and only writes the file when Save is pressed."""

    def __init__(self, settings: Settings, on_save: Callable[[Settings], None]):
        self.settings = settings
        self.on_save = on_save
        height = 3 * PANEL_HEIGHT + VOICE_HEIGHT + 176
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, height),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("Settings")
        self.window.setReleasedWhenClosed_(False)
        root = self.window.contentView()
        right = WIDTH - MARGIN
        keys = settings.keys  # the live store: a key added in the Keys window fills these menus at once

        y = 16
        self.decisions = EndpointPanel(
            root,
            "Classifier — one decision per step, the model that drives the loop",
            settings.decisions,
            DECISION_PROVIDERS,
            y,
            keys,
        )
        y += PANEL_HEIGHT
        w.separator(root, MARGIN, y - 8, right - MARGIN)
        self.writer = EndpointPanel(
            root, "Writer — the text typed into fields, and the URLs to open", settings.writer, TEXT_PROVIDERS, y, keys
        )
        y += PANEL_HEIGHT
        w.separator(root, MARGIN, y - 8, right - MARGIN)
        self.answer = EndpointPanel(
            root, "Answer — reads the screen when the classifier stops", settings.answer, TEXT_PROVIDERS, y, keys
        )
        y += PANEL_HEIGHT
        w.separator(root, MARGIN, y - 8, right - MARGIN)

        w.label(root, "Dictation — Whisper, running on this Mac", MARGIN, y, right - MARGIN, 16, font=w.BOLD)
        self.voice_model = w.combo(
            root, settings.voice.model, WHISPER_MODELS, MARGIN, y + 22, 380, placeholder=WHISPER_MODELS[-2]
        )
        w.label(root, "language", MARGIN + 390, y + 26, 60, 15, font=w.SMALL)
        self.voice_language = w.field(
            root, settings.voice.language, MARGIN + 452, y + 22, right - MARGIN - 452, placeholder="auto"
        )
        self.voice_append = w.checkbox(
            root, "add to the goal box instead of replacing it", settings.voice.append, MARGIN, y + 54, 330
        )
        self.shortcut = ShortcutField(root, settings.voice.shortcut, MARGIN + 340, y + 52)
        installed = voice_module.available()
        w.label(
            root,
            "the model downloads on first use; the audio never leaves this Mac" if installed else voice_module.INSTALL_HINT,
            MARGIN,
            y + 80,
            right - MARGIN,
            15,
            font=w.SMALL,
            color=AppKit.NSColor.secondaryLabelColor() if installed else AppKit.NSColor.systemOrangeColor(),
        )
        y += VOICE_HEIGHT
        w.separator(root, MARGIN, y - 8, right - MARGIN)

        w.label(root, "Browser", MARGIN, y + 5, 60)
        self.browser = w.combo(root, settings.browser, installed_browsers(), MARGIN + 64, y, 200, placeholder="Google Chrome")
        self.browser.setToolTip_("The browser use_browser drives. Only browsers on this Mac are listed.")
        w.label(root, "Email", MARGIN + 280, y + 5, 44)
        self.email = w.field(
            root, settings.email, MARGIN + 328, y, right - MARGIN - 328, placeholder="enables the type_email action"
        )
        y += 34
        self.share_clipboard = w.checkbox(
            root,
            "show the classifier the clipboard each step (it may hold a copied password)",
            settings.share_clipboard,
            MARGIN,
            y,
            right - MARGIN,
        )
        self.name_icons = w.checkbox(
            root,
            "name icon-only buttons with the answer's model (a writer call per new layout)",
            settings.name_icons,
            MARGIN,
            y + 22,
            right - MARGIN,
        )
        y += 50
        w.label(
            root,
            "A model menu that will not load is usually a key that is not set.",
            MARGIN,
            y,
            right - MARGIN,
            15,
            font=w.SMALL,
            color=AppKit.NSColor.secondaryLabelColor(),
        )
        y += 26
        w.button(root, "API keys\u2026", self._open_keys, MARGIN, y, 110, 30)
        w.button(root, "Save", self._save, right - 90, y, 90, 30, key="\r")
        w.button(root, "Cancel", self._cancel, right - 190, y, 90, 30, key="\x1b")

    @property
    def panels(self) -> tuple[EndpointPanel, ...]:
        return (self.decisions, self.writer, self.answer)

    def show(self) -> None:
        self.window.center()
        w.present(self.window)
        for panel in self.panels:  # every menu is filled from the live provider each time it opens
            panel.refresh()

    def _open_keys(self, sender) -> None:
        """Keys live in their own window: every provider's, not just the three chosen here."""

        def saved(settings) -> None:
            for panel in self.panels:  # a key that was missing may now fill a menu
                panel.refresh()

        self.keys_window = KeysWindow(self.settings, saved)
        self.keys_window.show()

    def _save(self, sender) -> None:
        self.settings.decisions = self.decisions.read()
        self.settings.writer = self.writer.read()
        self.settings.answer = self.answer.read()
        self.settings.voice.model = self.voice_model.stringValue().strip() or self.settings.voice.model
        self.settings.voice.language = self.voice_language.stringValue().strip()
        self.settings.voice.append = self.voice_append.state() == AppKit.NSControlStateValueOn
        self.settings.voice.shortcut = self.shortcut.read()
        self.settings.browser = self.browser.stringValue().strip()
        self.settings.email = self.email.stringValue().strip()
        self.settings.share_clipboard = self.share_clipboard.state() == AppKit.NSControlStateValueOn
        self.settings.name_icons = self.name_icons.state() == AppKit.NSControlStateValueOn
        self.settings.save()
        self.window.orderOut_(None)
        self.on_save(self.settings)

    def _cancel(self, sender) -> None:
        self.window.orderOut_(None)
