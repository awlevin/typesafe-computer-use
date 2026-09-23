"""The API keys window: one secure field per key the app can hold.

Keys are kept in the settings file, which is written readable by this user alone. A key already
present in the environment - exported in your shell, or loaded from `.env` - is used in preference
to anything stored here, so that row says so and takes no typing: otherwise a key entered in the
app would appear to do nothing, which is worse than not offering the field at all.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import AppKit
from Foundation import NSMakeRect

from ..settings import Settings, key_slots
from . import widgets as w

WIDTH = 620
MARGIN = 20
ROW = 34
HEADER = 54
FOOTER = 60


class KeysWindow:
    """Every key in one list, so a key can be entered before the provider that needs it is chosen."""

    def __init__(self, settings: Settings, on_save: Callable[[Settings], None]):
        self.settings = settings
        self.on_save = on_save
        self.slots = key_slots()
        height = HEADER + ROW * len(self.slots) + FOOTER
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, height),
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("API keys")
        self.window.setReleasedWhenClosed_(False)
        root = self.window.contentView()
        right = WIDTH - MARGIN

        w.label(
            root,
            "Keys are stored in ~/.config/typesafe-computer-use/settings.json, readable only by you.",
            MARGIN,
            16,
            right - MARGIN,
            15,
            font=w.SMALL,
            color=AppKit.NSColor.secondaryLabelColor(),
        )
        w.label(
            root,
            "A key already in your shell or .env is used instead, and is shown here as such.",
            MARGIN,
            33,
            right - MARGIN,
            15,
            font=w.SMALL,
            color=AppKit.NSColor.secondaryLabelColor(),
        )

        self.fields: dict[str, object] = {}
        for i, (key_env, providers) in enumerate(self.slots):
            y = HEADER + i * ROW
            w.label(root, providers, MARGIN, y + 5, 170, 16)
            from_environment = bool(_environment_value(key_env))
            if from_environment:
                w.label(
                    root,
                    f"{key_env} is set in your environment",
                    MARGIN + 178,
                    y + 5,
                    right - MARGIN - 178,
                    16,
                    color=AppKit.NSColor.secondaryLabelColor(),
                )
                continue
            field = w.field(
                root, settings.keys.get(key_env, ""), MARGIN + 178, y, right - MARGIN - 178, 24, placeholder=key_env, secure=True
            )
            self.fields[key_env] = field

        y = HEADER + ROW * len(self.slots) + 12
        w.button(root, "Save", self._save, right - 90, y, 90, 30, key="\r")
        w.button(root, "Cancel", self._cancel, right - 190, y, 90, 30, key="\x1b")

    def show(self) -> None:
        self.window.center()
        w.present(self.window)

    def _save(self, sender) -> None:
        """Keep what was typed, drop what was cleared, and leave rows served by the environment alone."""
        for key_env, field in self.fields.items():
            typed = field.stringValue().strip()
            if typed:
                self.settings.keys[key_env] = typed
            else:
                self.settings.keys.pop(key_env, None)
        self.settings.save()
        self.window.orderOut_(None)
        self.on_save(self.settings)

    def _cancel(self, sender) -> None:
        self.window.orderOut_(None)


def _environment_value(key_env: str) -> str:
    return (os.environ.get(key_env) or "").strip()
