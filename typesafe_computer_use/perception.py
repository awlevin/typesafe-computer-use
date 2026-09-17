"""Turn the display into clickable items: OCR text blocks and accessibility controls."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ocrmac import ocrmac
from PIL import Image

from . import macos
from .config import MAX_OPTIONS, MIN_OCR_CONFIDENCE
from .models import AxNode, Box, Item, Screen
from .timing import phase

Line = tuple[str, float, Box]
ECHO_CHARS = 24
MIN_BOX_OVERLAP = 0.5  # intersection over the smaller box
MIN_TOKEN_OVERLAP = 0.5

# Accessibility roles as one human word. Anything unlisted is "other".
ROLE_WORDS = {
    "AXButton": "button",
    "AXCell": "cell",
    "AXCheckBox": "checkbox",
    "AXComboBox": "field",
    "AXImage": "image",
    "AXLink": "link",
    "AXMenuBarItem": "menu",
    "AXMenuButton": "button",
    "AXPopUpButton": "popup",
    "AXRadioButton": "radio",
    "AXRow": "cell",
    "AXSearchField": "field",
    "AXSlider": "slider",
    "AXTab": "tab",
    "AXTextArea": "field",
    "AXTextField": "field",
}


def capture(
    image_path: Path | None = None,
    app: str | None = None,
    url: str | None = None,
    browser: str = "",
    timing: dict[str, float] | None = None,
) -> Screen:
    """Capture the main display, or load a saved capture for replay (then app/url are taken as given).

    Each query below is a round trip to the window server, AX, or AppleScript. Pass `timing` to
    record the seconds each one costs under "screenshot", "app", "field", and "url".
    """
    replay = image_path is not None and app is not None
    with phase(timing, "screenshot"):
        image = Image.open(image_path).convert("RGB") if image_path else macos.screenshot()
    with phase(timing, "app"):
        if replay:
            frontmost, pid = app, None
        else:
            frontmost, pid = macos.frontmost_app_and_pid()
            frontmost = app or frontmost
    with phase(timing, "field"):
        field = None if replay else macos.focused_field()
    with phase(timing, "url"):
        page_url = url if url is not None else (None if replay else macos.browser_url(browser))
    return Screen(image=image, scale=macos.display_scale(image), app=frontmost, field=field, url=page_url, pid=pid)


def goal_echoes(goal: str) -> set[str]:
    """Substrings that identify a screen line as the command that launched this run."""
    norm = " ".join(goal.lower().split())
    return {norm[:ECHO_CHARS], norm[-ECHO_CHARS:]} if len(norm) >= ECHO_CHARS else {norm}


def is_echo(text: str, echoes: set[str]) -> bool:
    norm = " ".join(text.lower().split())
    return any(e in norm for e in echoes)


def perceive(screen: Screen, budget: int, goal: str, timing: dict[str, float] | None = None) -> list[Item]:
    """Everything worth clicking on this screen: OCR text blocks, plus the app's own controls.

    Fills `screen.ax_refs` on the way, so an item that came from the accessibility tree can be
    pressed through it later. The merge renumbers everything, hence the side table over the
    final indices rather than a handle on the item itself, which has to stay printable.
    """
    with phase(timing, "ocr"):
        blocks = ocr(screen, budget, goal)
    with phase(timing, "ax"):
        nodes = ax_nodes(screen, budget)
        controls = to_ax_items(nodes, screen.scale)
    merged = merge_with_origins(blocks, controls, budget)
    screen.ax_refs.clear()
    screen.ax_refs.update({it.index: nodes[origin].ref for it, origin in merged if origin is not None and nodes[origin].ref})
    return [it for it, _ in merged]


def ocr(screen: Screen, budget: int, goal: str) -> list[Item]:
    raw = ocrmac.OCR(screen.image, recognition_level="accurate").recognize(px=True)
    echoes = goal_echoes(goal)
    lines: list[Line] = [(t.strip(), c, b) for t, c, b in raw if t.strip() and c >= MIN_OCR_CONFIDENCE and not is_echo(t, echoes)]
    return to_items(merge_blocks(lines), budget)


def ax_nodes(screen: Screen, budget: int) -> list[AxNode]:
    """The frontmost app's labelled controls, in screen points.

    Icon-only buttons are invisible to OCR and live only here. Accessibility is best effort:
    a missing pid, a refusing app, or a raising bridge all mean OCR carries the step alone.
    """
    if screen.pid is None:
        return []
    width_pt, height_pt = screen.size_pt
    try:
        nodes, _capped = macos.actionable_elements(screen.pid, width_pt, height_pt)
    except Exception:
        return []
    return [node for node in nodes[:budget] if node.label]


def to_ax_items(nodes: list[AxNode], scale: float) -> list[Item]:
    """Controls as items, converted from screen points to capture pixels."""
    return [
        Item(
            index=i,
            text=node.label,
            ocr_confidence=1.0,
            x1=node.x * scale,
            y1=node.y * scale,
            x2=(node.x + node.w) * scale,
            y2=(node.y + node.h) * scale,
            role=ROLE_WORDS.get(node.role, "other"),
            source="ax",
        )
        for i, node in enumerate(nodes)
    ]


def ax_items(screen: Screen, budget: int) -> list[Item]:
    """The frontmost app's labelled controls as items on the capture."""
    return to_ax_items(ax_nodes(screen, budget), screen.scale)


def merge_sources(ocr_items: list[Item], ax_items: list[Item], budget: int = MAX_OPTIONS) -> list[Item]:
    """One item per thing. An accessibility control that sits on the OCR block naming it replaces both."""
    return [it for it, _ in merge_with_origins(ocr_items, ax_items, budget)]


def merge_with_origins(ocr_items: list[Item], ax_items: list[Item], budget: int = MAX_OPTIONS) -> list[tuple[Item, int | None]]:
    """The merge, each item paired with the position of the control it came from, or None for plain text.

    The pairing survives the budget cut and the renumbering, which is the only way back from a
    final item to the accessibility element behind it.
    """
    taken: set[int] = set()
    merged: list[tuple[Item, int | None]] = []
    for origin, control in enumerate(ax_items):
        best, best_overlap = None, MIN_BOX_OVERLAP
        for i, block in enumerate(ocr_items):
            if i in taken:
                continue
            overlap = box_overlap(control, block)
            if overlap >= best_overlap and texts_match(control.text, block.text):
                best, best_overlap = i, overlap
        if best is None:
            merged.append((control, origin))
            continue
        block = ocr_items[best]
        taken.add(best)
        text = control.text if len(control.text) >= len(block.text) else block.text
        merged.append((replace(block, text=text, role=control.role, source="ax+ocr"), origin))
    merged += [(block, None) for i, block in enumerate(ocr_items) if i not in taken]
    kept = [merged[i] for i in kept_by_budget([it for it, _ in merged], budget)]
    order = reading_order([it for it, _ in kept])
    return [(replace(kept[j][0], index=i), kept[j][1]) for i, j in enumerate(order)]


def box_overlap(a: Item, b: Item) -> float:
    """Intersection over the smaller box, so a tight control inside a wide text line still counts."""
    wide = min(a.x2, b.x2) - max(a.x1, b.x1)
    tall = min(a.y2, b.y2) - max(a.y1, b.y1)
    smaller = min((a.x2 - a.x1) * (a.y2 - a.y1), (b.x2 - b.x1) * (b.y2 - b.y1))
    return wide * tall / smaller if wide > 0 and tall > 0 and smaller > 0 else 0.0


def texts_match(a: str, b: str) -> bool:
    """One label contains the other, or they share half their words."""
    x, y = " ".join(a.lower().split()), " ".join(b.lower().split())
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    words_x, words_y = set(x.split()), set(y.split())
    return len(words_x & words_y) / min(len(words_x), len(words_y)) >= MIN_TOKEN_OVERLAP


def kept_by_budget(items: list[Item], budget: int) -> list[int]:
    """Which items survive the Choice ceiling: the faintest OCR-only blocks go first,
    and a control is never dropped for text. Their positions, in the order given."""
    if len(items) <= budget:
        return list(range(len(items)))
    ranked = sorted(range(len(items)), key=lambda i: (items[i].from_ax, items[i].ocr_confidence))
    dropped = set(ranked[: len(items) - budget])
    return [i for i in range(len(items)) if i not in dropped]


def to_items(lines: list[Line], budget: int) -> list[Item]:
    return order_items([Item(0, t, c, *b) for t, c, b in lines])[:budget]


def order_items(items: list[Item]) -> list[Item]:
    """Number items in reading order: rows by the median item height, then left to right."""
    return [replace(items[j], index=i) for i, j in enumerate(reading_order(items))]


def reading_order(items: list[Item]) -> list[int]:
    """Positions of the items in reading order: rows by the median item height, then left to right."""
    heights = sorted(it.y2 - it.y1 for it in items) or [1.0]
    row_h = max(1.0, heights[len(heights) // 2])
    return sorted(range(len(items)), key=lambda i: (round((items[i].y1 + items[i].y2) / 2 / row_h), items[i].x1))


def merge_blocks(lines: list[Line]) -> list[Line]:
    """Join lines that continue a block above them: aligned left edge, small gap, similar height."""
    blocks: list[list] = []  # [text, conf, box, last_line_height]
    for text, conf, (x1, y1, x2, y2) in sorted(lines, key=lambda r: (r[2][1], r[2][0])):
        h = y2 - y1
        best = None
        for block in blocks:
            bx1, _, _, by2 = block[2]
            bh = block[3]
            gap = y1 - by2
            continues = abs(x1 - bx1) < 0.6 * bh and -0.2 * bh < gap < 0.8 * bh and 0.7 < h / max(bh, 1) < 1.4
            if continues and (best is None or gap < best[0]):
                best = (gap, block)
        if best is None:
            blocks.append([text, conf, (x1, y1, x2, y2), h])
            continue
        block = best[1]
        bx1, by1, bx2, _ = block[2]
        block[0] = f"{block[0]} {text}"
        block[1] = min(block[1], conf)
        block[2] = (min(bx1, x1), by1, max(bx2, x2), y2)
        block[3] = h
    return [(t, c, b) for t, c, b, _ in blocks]


def near_field(screen: Screen, items: list[Item], radius_pt: float = 160) -> list[str]:
    """Text of items within a radius of the focused field, in screen points."""
    f = screen.field
    if f is None:
        return []
    out = []
    for it in items:
        cx, cy = screen.to_points(it)
        if abs(cx - (f.x + f.w / 2)) < radius_pt + f.w / 2 and abs(cy - (f.y + f.h / 2)) < radius_pt:
            out.append(it.text)
    return out
