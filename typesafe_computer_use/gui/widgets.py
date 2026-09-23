"""Small AppKit constructors, so the window code reads as layout rather than as Objective-C.

Every control is made and placed in one call, with the frame given in the top-left coordinates
people actually think in; `place` flips them into the bottom-left ones AppKit uses. Nothing here
holds state: the window owns its controls, and this module only builds them.
"""

from __future__ import annotations

from collections.abc import Callable

import AppKit
import objc
from Foundation import NSMakeRect, NSObject

LABEL = AppKit.NSFont.systemFontOfSize_(12)
BOLD = AppKit.NSFont.boldSystemFontOfSize_(12)
MONO = AppKit.NSFont.monospacedSystemFontOfSize_weight_(11, AppKit.NSFontWeightRegular)
SMALL = AppKit.NSFont.systemFontOfSize_(11)


class Action(NSObject):
    """An Objective-C target that forwards to a Python callable.

    AppKit sends actions to a selector on an object, so every button would otherwise need a method
    of its own on the window. One of these per control keeps the handlers as ordinary functions.
    """

    def initWithHandler_(self, handler):
        self = objc.super(Action, self).init()
        self._handler = handler
        return self

    def invoke_(self, sender) -> None:
        self._handler(sender)


def place(view, parent, x: float, y: float, w: float, h: float):
    """Add `view` to `parent` at top-left coordinates, and hand it back."""
    view.setFrame_(NSMakeRect(x, parent.frame().size.height - y - h, w, h))
    parent.addSubview_(view)
    return view


def label(parent, text: str, x: float, y: float, w: float, h: float = 17, font=None, color=None):
    view = AppKit.NSTextField.alloc().init()
    view.setStringValue_(text)
    view.setBezeled_(False)
    view.setDrawsBackground_(False)
    view.setEditable_(False)
    view.setSelectable_(True)
    view.setFont_(font or LABEL)
    if color is not None:
        view.setTextColor_(color)
    return place(view, parent, x, y, w, h)


def field(parent, value: str, x: float, y: float, w: float, h: float = 24, placeholder: str = "", secure: bool = False):
    view = (AppKit.NSSecureTextField if secure else AppKit.NSTextField).alloc().init()
    view.setStringValue_(value)
    view.setPlaceholderString_(placeholder)
    view.setFont_(LABEL)
    return place(view, parent, x, y, w, h)


def combo(parent, value: str, options: list[str], x: float, y: float, w: float, h: float = 24, placeholder: str = ""):
    """An editable field with a list attached: a model name can be picked or simply typed."""
    view = AppKit.NSComboBox.alloc().init()
    view.setUsesDataSource_(False)
    view.addItemsWithObjectValues_(options)
    view.setStringValue_(value)
    view.setPlaceholderString_(placeholder)
    view.setCompletes_(True)
    view.setFont_(LABEL)
    return place(view, parent, x, y, w, h)


def button(parent, title: str, handler: Callable, x: float, y: float, w: float, h: float = 26, key: str = ""):
    view = AppKit.NSButton.alloc().init()
    view.setTitle_(title)
    view.setBezelStyle_(AppKit.NSBezelStyleRounded)
    view.setFont_(LABEL)
    if key:
        view.setKeyEquivalent_(key)
    _wire(view, handler)
    return place(view, parent, x, y, w, h)


def checkbox(parent, title: str, on: bool, x: float, y: float, w: float, h: float = 20, handler: Callable | None = None):
    view = AppKit.NSButton.alloc().init()
    view.setButtonType_(AppKit.NSButtonTypeSwitch)
    view.setTitle_(title)
    view.setFont_(LABEL)
    view.setState_(AppKit.NSControlStateValueOn if on else AppKit.NSControlStateValueOff)
    if handler is not None:
        _wire(view, handler)
    return place(view, parent, x, y, w, h)


def popup(parent, titles: list[str], selected: str, x: float, y: float, w: float, h: float = 26, handler: Callable | None = None):
    view = AppKit.NSPopUpButton.alloc().init()
    view.addItemsWithTitles_(titles)
    if selected in titles:
        view.selectItemWithTitle_(selected)
    view.setFont_(LABEL)
    if handler is not None:
        _wire(view, handler)
    return place(view, parent, x, y, w, h)


def text_view(parent, x: float, y: float, w: float, h: float, mono: bool = True):
    """A read-only scrolling text area, and the scroll view that holds it. Returns both."""
    scroll = AppKit.NSScrollView.alloc().init()
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(AppKit.NSBezelBorder)
    scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    view = AppKit.NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    view.setEditable_(False)
    view.setRichText_(False)
    view.setFont_(MONO if mono else LABEL)
    view.setAutoresizingMask_(AppKit.NSViewWidthSizable)
    view.textContainer().setWidthTracksTextView_(True)
    scroll.setDocumentView_(view)
    place(scroll, parent, x, y, w, h)
    return scroll, view


def image_view(parent, x: float, y: float, w: float, h: float):
    view = AppKit.NSImageView.alloc().init()
    view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
    view.setImageAlignment_(AppKit.NSImageAlignTop)
    view.setWantsLayer_(True)
    view.layer().setBorderWidth_(1.0)
    view.layer().setBorderColor_(AppKit.NSColor.separatorColor().CGColor())
    return place(view, parent, x, y, w, h)


def separator(parent, x: float, y: float, w: float):
    view = AppKit.NSBox.alloc().init()
    view.setBoxType_(AppKit.NSBoxSeparator)
    return place(view, parent, x, y, w, 1)


def append(view, text: str) -> None:
    """Add a line to a text view and keep the newest line in sight."""
    storage = view.textStorage()
    storage.beginEditing()
    storage.replaceCharactersInRange_withString_((storage.length(), 0), text + "\n")
    storage.endEditing()
    view.setFont_(MONO)
    view.scrollRangeToVisible_((storage.length(), 0))


def present(window, above_everything: bool = False) -> None:
    """Put a window on the user's screen. Every window this app shows goes through here, so the test
    guard can refuse it: a window ordered front takes the focus from whatever the user is doing."""
    if above_everything:
        window.orderFrontRegardless()
    else:
        window.makeKeyAndOrderFront_(None)


def alert(title: str, message: str, style=None) -> None:
    sheet = AppKit.NSAlert.alloc().init()
    sheet.setMessageText_(title)
    sheet.setInformativeText_(message)
    sheet.setAlertStyle_(style if style is not None else AppKit.NSAlertStyleWarning)
    sheet.runModal()


_ACTIONS: list[Action] = []  # AppKit does not retain a target, so the forwarders are kept here


def _wire(control, handler: Callable) -> None:
    target = Action.alloc().initWithHandler_(handler)
    _ACTIONS.append(target)
    control.setTarget_(target)
    control.setAction_("invoke:")


def menu_item(menu, title: str, selector_or_handler, key: str = "", option: bool = False):
    """One menu item, wired either to a built-in selector (a string) or to a Python callable."""
    item = AppKit.NSMenuItem.alloc().init()
    item.setTitle_(title)
    item.setKeyEquivalent_(key)
    if option:
        item.setKeyEquivalentModifierMask_(AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagOption)
    if isinstance(selector_or_handler, str):
        item.setAction_(selector_or_handler)  # sent up the responder chain, so it works with no target
    else:
        _wire(item, selector_or_handler)
    menu.addItem_(item)
    return item


def submenu(main_menu, title: str):
    """A top-level menu, and the menu to hang its items on."""
    holder = AppKit.NSMenuItem.alloc().init()
    holder.setTitle_(title)
    menu = AppKit.NSMenu.alloc().initWithTitle_(title)
    holder.setSubmenu_(menu)
    main_menu.addItem_(holder)
    return menu


def confirm(title: str, message: str, accept: str, cancel: str) -> bool:
    """A two-button question. True when the first button was pressed."""
    sheet = AppKit.NSAlert.alloc().init()
    sheet.setMessageText_(title)
    sheet.setInformativeText_(message)
    sheet.setAlertStyle_(AppKit.NSAlertStyleWarning)
    sheet.addButtonWithTitle_(accept)
    sheet.addButtonWithTitle_(cancel)
    return sheet.runModal() == AppKit.NSAlertFirstButtonReturn
