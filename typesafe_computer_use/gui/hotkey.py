"""The dictation key, watched everywhere on the Mac.

A global monitor rather than a menu shortcut, because the point is to dictate without going to
find this app first - including while it is hidden mid-run. macOS only delivers these events to a
process it trusts for Accessibility, which this app asks for anyway; without that trust the key
simply never fires, and the window says so rather than pretending to listen.

Pressing it starts the dictation and pressing it again stops it, so a long thought does not have
to be said with a finger held down. Holding the key does nothing extra: a key that repeats must
not toggle once per repeat, so only the first press of a hold counts and the release re-arms it.

The monitor does not swallow the keystroke, so the chord also reaches whatever is in front. That
is why the default is Control-Option-D: two modifiers, and nothing on a Mac answers to it.
"""

from __future__ import annotations

from collections.abc import Callable

import AppKit

from ..settings import Shortcut

# The modifier bits, masked to the ones a person can hold; the rest carry keyboard state.
RELEVANT = (
    AppKit.NSEventModifierFlagCommand
    | AppKit.NSEventModifierFlagControl
    | AppKit.NSEventModifierFlagOption
    | AppKit.NSEventModifierFlagShift
)


def held(flags: int) -> tuple[bool, bool, bool, bool]:
    return (
        bool(flags & AppKit.NSEventModifierFlagCommand),
        bool(flags & AppKit.NSEventModifierFlagControl),
        bool(flags & AppKit.NSEventModifierFlagOption),
        bool(flags & AppKit.NSEventModifierFlagShift),
    )


def matches(event, shortcut: Shortcut) -> bool:
    return event.keyCode() == shortcut.key_code and held(event.modifierFlags() & RELEVANT) == shortcut.modifiers


def _add_monitors(mask: int, handler, local_handler) -> tuple:
    """Start watching every key event on the Mac for this app: the global monitor and the local one.

    The global monitor sees keystrokes typed into other apps, which is why the test guard refuses it.
    """
    globally = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, handler)
    locally = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, local_handler)
    return globally, locally


class Hotkey:
    """Calls `on_toggle` each time the chord is pressed: once to start, once to stop.

    Both a global monitor and a local one are installed: the global one sees the key when another
    app is in front, the local one when this app is, and macOS delivers to exactly one of them.
    """

    DOWN = AppKit.NSEventMaskKeyDown
    UP = AppKit.NSEventMaskKeyUp

    def __init__(self, on_toggle: Callable[[], None]):
        self.on_toggle = on_toggle
        self.shortcut = Shortcut()
        self.down = False  # the chord is physically held right now, so repeats are ignored
        self._monitors: list = []

    @property
    def watching(self) -> bool:
        return bool(self._monitors)

    def start(self, shortcut: Shortcut) -> bool:
        """Watch for this chord. False when macOS will not deliver the events, which means no trust."""
        self.stop()
        self.shortcut = shortcut
        if not shortcut.enabled:
            return False
        globally, locally = _add_monitors(self.DOWN | self.UP, self._handle, self._handle_local)
        self._monitors = [monitor for monitor in (globally, locally) if monitor is not None]
        return globally is not None

    def stop(self) -> None:
        for monitor in self._monitors:
            AppKit.NSEvent.removeMonitor_(monitor)
        self._monitors = []
        self.down = False

    # --- the events -------------------------------------------------------------------------

    def _handle_local(self, event):
        """The local monitor must hand the event back, or this app stops receiving keys at all."""
        self._handle(event)
        return event

    def _handle(self, event) -> None:
        kind = event.type()
        if kind == AppKit.NSEventTypeKeyDown and matches(event, self.shortcut):
            if not self.down:  # a held key repeats; one press is one toggle
                self.down = True
                self.on_toggle()
        elif kind == AppKit.NSEventTypeKeyUp and event.keyCode() == self.shortcut.key_code:
            self.down = False  # the chord is free to be pressed again
