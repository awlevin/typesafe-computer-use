"""What an action did, read off the screen it was taken on and the next one.

An action's history line says what was tried: "clicked 'Organise'". A decision reads the history
and the screen now showing, never the screen before, so it cannot tell a click that missed from one
that worked. On OSWorld's chrome/2ad9387a a click meant for Organise only closed Chrome's "Restore
pages?" bubble, which sat on top of it, and the steps after it went to finding that out. So each line
ends with the coarsest change between the two screens, in what every tree gives, roles, names, the
page, and labels, with OCR's text standing in for the labels where there is no tree:

- a new page: "went to chrome://bookmarks"
- otherwise a popup that opened, "opened: menu ('Sort by name', 'Add new bookmark', …)", or one that
  closed while none opened: "closed: alert 'Restore pages?'"
- otherwise the items that came and went: "new: 'Add new folder', 'Import', …; gone: 'Search'",
  or, on the same screen by the rule the stall count uses (`same_screen`), what the focused text
  field now holds, which text field the focus moved to, or "no change".

A new page comes first because it changes everything else, a closed menu among it. A line stays
short, about twenty tokens: the classifier reads eight of them every step.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import TAB_MEMORY, Item, Popup, Screen, Signature, same_screen, signature

ARROW = " → "  # between an action and what came of it
PAGE_CHARS = 60  # a URL or a title, cut to this
NAME_CHARS = 40  # a popup's name
LABEL_CHARS = 24  # an item's label
POPUPS = 2  # popups named in one change
POPUP_LABELS = 2  # labels that name a popup with no name of its own
NEW = 2  # labels named of the items that came
GONE = 1  # and of the ones that went


@dataclass(frozen=True)
class Glance:
    """What the comparison reads of one screen."""

    app: str
    url: str | None
    title: str | None
    field: str | None  # the focused text field's label
    value: str | None  # and what it holds
    popups: dict[tuple, str]  # each popup showing, by its role and what identifies it, as a line names it
    labels: tuple[str, ...]  # the items' text in reading order, once each, the clock's strip left out
    signature: Signature
    tree: bool  # whether any item came from the accessibility tree


def glance(screen: Screen, items: list[Item]) -> Glance:
    """The facts of one screen a history line is made of.

    A popup is the tree's, with all its controls, whether or not the capture has drawn them yet: a
    capture can lag the tree by seconds, and a menu named only by the item it had drawn, "menu ('Sort
    by name')", read to the answer model as the wrong menu, where the tree held all six items.
    """
    popups: dict[tuple, str] = {}
    for popup, labels in screen.popups.items():
        # A popup is known by its role and name. A menu has no name, so its first items name it.
        key = (popup.role, popup.name) if popup.name else (popup.role, *labels[:3])
        popups.setdefault(key, describe(popup, labels))
    text = screen.field if screen.field and screen.field.is_text else None
    return Glance(
        app=screen.app,
        url=screen.url,
        title=screen.title,
        field=text.label if text else None,
        value=text.value if text else None,
        popups=popups,
        labels=item_labels(screen, items),
        signature=signature(screen, items),
        tree=any(it.from_ax for it in items),
    )


def item_labels(screen: Screen, items: list[Item]) -> tuple[str, ...]:
    """The items' text, once each, as a line may name them: the app's controls first, then the text
    OCR read, each in reading order. A lone glyph, which is mostly OCR reading an icon, says nothing
    in a line, and the clock's strip changes on its own."""
    kept = [it for it in items if len(it.text.strip()) > 1 and not screen.in_menu_bar(it)]
    ordered = [it for it in kept if it.from_ax] + [it for it in kept if not it.from_ax]
    return tuple(dict.fromkeys(TAB_MEMORY.sub("", it.text) for it in ordered))


def describe(popup: Popup, labels: list[str]) -> str:
    """A popup as a line names it: "alert 'Restore pages?'", or "menu ('Sort by name', 'Add new bookmark', …)"."""
    role = popup.role or "popup"
    if popup.name:
        return f"{role} {clip(popup.name, NAME_CHARS)!r}"
    return f"{role} ({listed(labels, POPUP_LABELS)})"


def change(before: Glance, after: Glance) -> str | None:
    """The coarsest change from one screen to the next, as the end of a history line.

    None when one screen had a tree and the other had none: what the tree brings or takes then is
    no change the action made. Chrome builds its tree only once a client asks for it, so the first
    capture of a task may have none, and a fetch of the tree can fail.
    """
    if before.tree != after.tree:
        return None
    page = went(before, after)
    if page is not None:
        return page
    popups = shown(before, after)
    if popups is not None:
        return popups
    # The page is the same (see `went`), so the lines alone say whether the screen is. Text typed
    # into a field is no line of it (`perception.in_field`), so the field says what it now holds.
    if same_screen(before.signature, (*before.signature[:3], after.signature[3])):
        if before.signature[2] != after.signature[2]:
            return f"focused {clip(after.field, LABEL_CHARS)!r}" if after.field else "only the focus moved"
        if after.field and after.value != before.value:
            return f"{clip(after.field, LABEL_CHARS)!r} holds {clip(after.value, LABEL_CHARS)!r}"
        return "no change"
    new = [label for label in after.labels if label not in before.labels]
    gone = [label for label in before.labels if label not in after.labels]
    if new or gone:
        return "; ".join(part for part in (listed(new, NEW, "new: "), listed(gone, GONE, "gone: ")) if part)
    return "items moved"


def went(before: Glance, after: Glance) -> str | None:
    """The page the action went to, when it went to one: a URL, another app, or another window title.

    A title one side does not know is no change: a window with no title of its own, such as a sheet,
    is still on the page behind it. A new URL under the same title is none either, where the title is
    known: in an OSWorld VM the URL is what Chrome's address bar holds, which is also text typed into
    it and not yet sent.
    """
    retitled = bool(before.title and after.title and after.title != before.title)
    if after.url and after.url != before.url and (retitled or not (before.title and after.title)):
        return f"went to {clip(after.url, PAGE_CHARS)}"
    if after.app and after.app != before.app:
        return f"went to {after.app}" + (f" {clip(after.title, PAGE_CHARS)!r}" if after.title else "")
    if retitled:
        return f"went to {clip(after.title, PAGE_CHARS)!r}"
    return None


def shown(before: Glance, after: Glance) -> str | None:
    """The popups that opened, or, when none did, the ones that closed; None when neither.

    What opened is the news: a click on a menu's item that opens a dialog closes the menu too. A
    modal dialog hides the page behind it from the tree, so a menu of the page, such as the list down
    the side of Chrome's settings, goes with it and comes back when it closes: a menu that shows as a
    dialog closes did not open. A closed popup is said without "opened: nothing" after it: in a replay
    of the 24 captured requests with such a line, those words lowered the classifier's confidence in a
    `done` after a dialog's Save, and in clicking Organise again after the bubble took the first click.
    """
    closed = {key: text for key, text in before.popups.items() if key not in after.popups}
    opened = {key: text for key, text in after.popups.items() if key not in before.popups}
    if any(key[0] == "dialog" for key in closed):
        opened = {key: text for key, text in opened.items() if key[0] != "menu"}
    if opened:
        return f"opened: {listed(list(opened.values()), POPUPS, quote=False)}"
    if closed:
        return f"closed: {listed(list(closed.values()), POPUPS, quote=False)}"
    return None


def listed(texts: list[str], limit: int, prefix: str = "", quote: bool = True) -> str:
    """The first `limit` texts, quoted and cut short, and an ellipsis for the rest; "" for none."""
    if not texts:
        return ""
    head = ", ".join(repr(clip(t, LABEL_CHARS)) if quote else t for t in texts[:limit])
    return f"{prefix}{head}{', …' if len(texts) > limit else ''}"


def clip(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: chars - 1] + "…"
