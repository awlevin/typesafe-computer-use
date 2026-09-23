"""The red dot: what is on screen while the Mac is listening.

A borderless window above everything, including full-screen apps, that no click can land on. It
says one thing - your voice is being recorded right now - from wherever you happen to be, since
the window you dictated from may well be hidden or behind something.

It sits in the top right corner, always: out of the way of whatever you are reading, and out of
the way of whatever the loop is clicking, which matters because it is on screen while a run is
driving the machine.
"""

from __future__ import annotations

import warnings

import AppKit
import objc
from Foundation import NSMakeRect

from . import widgets as w

SIZE = 72.0  # points across: big enough to catch the eye from the corner of it
INSET = 24.0  # from the edges of the screen's usable area, so it clears the menu bar
PULSE_SECONDS = 0.6
BRIGHT, DIM = 0.95, 0.55


class Dot:
    """One red circle, shown while the dictation key is held."""

    def __init__(self) -> None:
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, SIZE, SIZE), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.window.setIgnoresMouseEvents_(True)  # it is a sign, not a control
        self.window.setLevel_(AppKit.NSScreenSaverWindowLevel)  # above other apps, and above full screen
        self.window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.window.setHasShadow_(False)
        view = self.window.contentView()
        view.setWantsLayer_(True)
        layer = view.layer()
        with warnings.catch_warnings():  # PyObjC narrates every CGColor it bridges
            warnings.simplefilter("ignore", objc.ObjCPointerWarning)
            layer.setBackgroundColor_(AppKit.NSColor.systemRedColor().CGColor())
            layer.setBorderColor_(AppKit.NSColor.whiteColor().colorWithAlphaComponent_(0.85).CGColor())
        layer.setCornerRadius_(SIZE / 2)
        layer.setBorderWidth_(4.0)
        self.timer = None
        self.bright = True

    def show(self) -> None:
        """Put it in the top right of the screen the mouse is on, and start the pulse."""
        self.window.setFrameOrigin_(self._corner())
        self.window.setAlphaValue_(BRIGHT)
        w.present(self.window, above_everything=True)
        if self.timer is None:
            self.timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                PULSE_SECONDS, True, lambda t: self._pulse()
            )

    def hide(self) -> None:
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        self.window.orderOut_(None)

    def _pulse(self) -> None:
        """A slow breath, so a dot that is stuck on is told apart from one that is listening."""
        self.bright = not self.bright
        self.window.animator().setAlphaValue_(BRIGHT if self.bright else DIM)

    def _corner(self):
        """Top right, below the menu bar, where it covers nothing the loop is working on."""
        frame = self._screen().visibleFrame()
        return AppKit.NSMakePoint(
            frame.origin.x + frame.size.width - SIZE - INSET,
            frame.origin.y + frame.size.height - SIZE - INSET,
        )

    def _screen(self):
        """Whichever screen the pointer is on, so the dot appears where the user is looking."""
        where = AppKit.NSEvent.mouseLocation()
        for screen in AppKit.NSScreen.screens():
            if AppKit.NSPointInRect(where, screen.frame()):
                return screen
        return AppKit.NSScreen.mainScreen()
