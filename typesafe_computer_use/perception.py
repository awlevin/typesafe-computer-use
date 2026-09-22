"""Turn the display into clickable items: OCR text blocks and accessibility controls."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from .config import MAX_OPTIONS, MIN_OCR_CONFIDENCE
from .models import AxNode, Box, Item, Screen
from .platform_adapter import desktop
from .timing import OCR_RECTS, OCR_REGION_PCT, phase

Line = tuple[str, float, Box]
ECHO_CHARS = 24
MIN_BOX_OVERLAP = 0.5  # intersection over the smaller box
MIN_TOKEN_OVERLAP = 0.5

# OCR costs about two thirds of a step, and it scales with the amount of text, so the way to make it
# cheaper is to read less of the screen: the frontmost window's own columns instead of the display,
# and within them only the blobs of tiles that changed since the previous capture, one crop each.
MENU_BAR_PT = 40.0  # the strip above every window, which the app's own menus live in
REGION_MARGIN_PT = 8.0  # slack around the window, for the shadow and a clipped glyph
THUMB_DIVISOR = 8  # the change detector works on a 1/8 scale grayscale copy
TILE_PX = 256.0  # tile side in capture pixels
TILE_DIFF = 6.0  # mean absolute 8-bit difference that counts a tile as changed
REOCR_FRACTION = 0.6  # above this share of changed tiles, reading the whole region is cheaper
MAX_REOCR_RECTS = 4  # past this, the per-call overhead outweighs the pixels another rectangle saves


def capture(
    image_path: Path | None = None,
    app: str | None = None,
    url: str | None = None,
    browser: str = "",
    timing: dict[str, float] | None = None,
) -> Screen:
    """Capture the main display, or load a saved capture for replay (then app/url are taken as given).

    Each query below is a round trip to the window server, AX, or AppleScript. Pass `timing` to
    record the seconds each one costs under "screenshot", "app", "window", "field", and "url".
    """
    replay = image_path is not None and app is not None
    with phase(timing, "screenshot"):
        image = Image.open(image_path).convert("RGB") if image_path else desktop.screenshot()
    with phase(timing, "app"):
        if replay:
            frontmost, pid = app, None
        else:
            frontmost, pid = desktop.frontmost_app_and_pid()
            frontmost = app or frontmost
    with phase(timing, "window"):
        window = None if replay else desktop.frontmost_window_bounds(pid)
    with phase(timing, "field"):
        field = None if replay else desktop.focused_field()
    with phase(timing, "url"):
        page_url = url if url is not None else (None if replay else desktop.browser_url(browser))
    return Screen(
        image=image, scale=desktop.display_scale(image), app=frontmost, field=field, url=page_url, pid=pid, window=window
    )


def goal_echoes(goal: str) -> set[str]:
    """Substrings that identify a screen line as the command that launched this run."""
    norm = " ".join(goal.lower().split())
    return {norm[:ECHO_CHARS], norm[-ECHO_CHARS:]} if len(norm) >= ECHO_CHARS else {norm}


def is_echo(text: str, echoes: set[str]) -> bool:
    norm = " ".join(text.lower().split())
    return any(e in norm for e in echoes)


def perceive(
    screen: Screen,
    budget: int,
    goal: str,
    timing: dict[str, float] | None = None,
    cache: OcrCache | None = None,
) -> list[Item]:
    """Everything worth clicking on this screen: OCR text blocks, plus the app's own controls.

    Fills `screen.ax_refs` on the way, so an item that came from the accessibility tree can be
    pressed through it later. The merge renumbers everything, hence the side table over the
    final indices rather than a handle on the item itself, which has to stay printable.

    Fills `screen.offscreen` too: labelled controls the app exposes but does not show. They are
    offered on their own, never as items, because nothing on the capture points at them.

    A `cache` carries the previous capture's OCR, so only the tiles that changed are read again.
    Pass None to read the whole region every time, which is what a replay and an inspection do.
    """
    with phase(timing, "ocr"):
        blocks = ocr(screen, budget, goal, cache, timing)
    with phase(timing, "ax"):
        nodes, hidden = ax_nodes(screen, budget)
        controls = to_ax_items(nodes, screen.scale)
    merged = merge_with_origins(blocks, controls, budget)
    screen.ax_refs.clear()
    screen.ax_refs.update({it.index: nodes[origin].ref for it, origin in merged if origin is not None and nodes[origin].ref})
    items = [it for it, _ in merged]
    screen.offscreen.clear()
    screen.offscreen.extend(offscreen_controls(hidden, items))
    return items


def ocr(
    screen: Screen,
    budget: int,
    goal: str,
    cache: OcrCache | None = None,
    timing: dict[str, float] | None = None,
) -> list[Item]:
    """The screen's text as items, filtered and merged into blocks.

    The filter runs over the raw lines every step, including the reused ones, so a cached line is
    treated exactly as a freshly read one.
    """
    lines, read_pct, rects = ocr_lines(screen, cache)
    if timing is not None:
        timing[OCR_REGION_PCT] = round(read_pct, 1)
        timing[OCR_RECTS] = rects
    echoes = goal_echoes(goal)
    kept: list[Line] = [
        (t.strip(), c, b) for t, c, b in lines if t.strip() and c >= MIN_OCR_CONFIDENCE and not is_echo(t, echoes)
    ]
    return to_items(merge_blocks(kept), budget)


# ------------------------------------------------------------------ reading less of the screen


class OcrCache:
    """The previous capture's OCR, and what makes it reusable.

    Holds a reduced grayscale copy of the capture, to find what moved, and the raw lines before
    merging and filtering, in full-capture pixels. One cache belongs to one run.
    """

    def __init__(self) -> None:
        self.app: str | None = None
        self.window: tuple[float, float, float, float] | None = None
        self.region: Box | None = None
        self.thumb: Image.Image | None = None
        self.lines: list[Line] = []

    def reusable(self, screen: Screen, region: Box, thumb: Image.Image) -> bool:
        """Never across a different app, a moved or resized window, or a different read region."""
        return (
            self.thumb is not None
            and self.thumb.size == thumb.size
            and self.app == screen.app
            and self.window == screen.window
            and self.region == region
        )

    def store(self, screen: Screen, region: Box, thumb: Image.Image, lines: list[Line]) -> None:
        self.app, self.window, self.region, self.thumb, self.lines = screen.app, screen.window, region, thumb, list(lines)


def ocr_lines(screen: Screen, cache: OcrCache | None = None) -> tuple[list[Line], float, int]:
    """Raw OCR lines for this capture in full-capture pixels, the share of it read, and how many crops.

    Without a cache this reads the region once. With one it reads only the rectangles covering the
    tiles that changed, one Vision call each, unless too much of the screen moved, in which case the
    whole region is cheaper than stitching. A line a re-read rectangle touches is dropped and read
    again whole, because Vision segments a crop slightly differently from the full image.
    """
    region = ocr_region(screen)
    area = float(max(1, screen.image.width * screen.image.height))

    def read_pct(rects: list[Box]) -> float:
        return 100.0 * sum(area_of(rect) for rect in rects) / area

    if cache is None:
        return ocr_crop(screen.image, region), read_pct([region]), 0

    thumb = thumbnail(screen.image)
    if not cache.reusable(screen, region, thumb):
        return _read_region(screen, region, thumb, cache), read_pct([region]), 0
    tiles = tiles_in(region)
    changed = changed_tiles(thumb, cache.thumb, tiles)
    if len(changed) > REOCR_FRACTION * len(tiles):
        return _read_region(screen, region, thumb, cache), read_pct([region]), 0
    rects = reocr_rects(changed, region, cache.lines)
    if not rects:
        cache.store(screen, region, thumb, cache.lines)
        return cache.lines, 0.0, 0
    if sum(area_of(rect) for rect in rects) > REOCR_FRACTION * area_of(region):
        return _read_region(screen, region, thumb, cache), read_pct([region]), 0
    fresh = [ln for rect in rects for ln in ocr_crop(screen.image, rect)]
    lines = merge_reocr(cache.lines, fresh, rects)
    cache.store(screen, region, thumb, lines)
    return lines, read_pct(rects), len(rects)


def _read_region(screen: Screen, region: Box, thumb: Image.Image, cache: OcrCache) -> list[Line]:
    lines = ocr_crop(screen.image, region)
    cache.store(screen, region, thumb, lines)
    return lines


def ocr_region(screen: Screen) -> Box:
    """The part of the capture worth reading, in capture pixels.

    The frontmost window with a margin, joined with the menu bar strip over the same columns and
    clamped to the display. Text on the desktop and in background windows is noise to the decision,
    so it is left unread. Clipping the strip to the window's x-range is what makes the crop worth
    anything on a full-height window, whose own rectangle already reaches the bottom of the display.

    The cost is that status items to the right of the window, the clock and the menu extras, go
    unread. They stay clickable: the accessibility tree lists them as AXMenuBarItem controls.
    """
    width, height = float(screen.image.width), float(screen.image.height)
    if screen.window is None:
        return (0.0, 0.0, width, height)
    x, y, w, h = screen.window
    scale, margin = screen.scale, REGION_MARGIN_PT
    window = ((x - margin) * scale, (y - margin) * scale, (x + w + margin) * scale, (y + h + margin) * scale)
    joined = (window[0], min(window[1], 0.0), window[2], max(window[3], MENU_BAR_PT * scale))
    clamped = (max(0.0, joined[0]), max(0.0, joined[1]), min(width, joined[2]), min(height, joined[3]))
    return clamped if clamped[2] > clamped[0] and clamped[3] > clamped[1] else (0.0, 0.0, width, height)


def ocr_crop(image: Image.Image, rect: Box) -> list[Line]:
    """OCR one rectangle of the capture. Boxes come back in full-capture pixels, so nothing downstream
    knows a crop happened."""
    x1, y1, x2, y2 = (round(v) for v in rect)
    crop = image if (x1, y1, x2, y2) == (0, 0, image.width, image.height) else image.crop((x1, y1, x2, y2))
    raw = desktop.recognize_text(crop)
    return [(text, conf, (b[0] + x1, b[1] + y1, b[2] + x1, b[3] + y1)) for text, conf, b in raw]


def thumbnail(image: Image.Image, divisor: int = THUMB_DIVISOR) -> Image.Image:
    """A grayscale copy at 1/divisor scale. Box-averaged, so it costs a few milliseconds and smooths
    away the compression noise that would otherwise read as a change."""
    return image.convert("L").reduce(divisor)


def tiles_in(region: Box, tile: float = TILE_PX) -> list[Box]:
    """The region cut into tiles aligned to its own origin. The last row and column are short."""
    x1, y1, x2, y2 = region
    out: list[Box] = []
    y = y1
    while y < y2:
        x = x1
        while x < x2:
            out.append((x, y, min(x + tile, x2), min(y + tile, y2)))
            x += tile
        y += tile
    return out


def tile_changed(
    thumb: Image.Image,
    previous: Image.Image,
    tile: Box,
    divisor: int = THUMB_DIVISOR,
    threshold: float = TILE_DIFF,
) -> bool:
    """True when the tile's mean absolute pixel difference clears the threshold. A tile whose patch
    cannot be compared counts as changed, so a doubt is always paid for with a re-read."""
    a, b = _patch(thumb, tile, divisor), _patch(previous, tile, divisor)
    if a.size != b.size or not a.width or not a.height:
        return True
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0] > threshold


def _patch(thumb: Image.Image, tile: Box, divisor: int) -> Image.Image:
    x1, y1, x2, y2 = (round(v / divisor) for v in tile)
    return thumb.crop((x1, y1, max(x2, x1 + 1), max(y2, y1 + 1)))


def changed_tiles(
    thumb: Image.Image,
    previous: Image.Image,
    tiles: list[Box],
    divisor: int = THUMB_DIVISOR,
    threshold: float = TILE_DIFF,
) -> list[Box]:
    return [tile for tile in tiles if tile_changed(thumb, previous, tile, divisor, threshold)]


def tile_clusters(changed: list[Box], tile: float = TILE_PX) -> list[list[Box]]:
    """The changed tiles grouped into blobs that touch along a side or at a corner.

    Scattered change is the ordinary case: the menu bar clock ticks a digit while one panel
    repaints. A single bounding box around both spans the display and forces a full read, where
    two boxes leave everything between them alone.
    """
    parent = list(range(len(changed)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(changed):
        for j, b in enumerate(changed[i + 1 :], start=i + 1):
            if abs(a[0] - b[0]) <= 1.5 * tile and abs(a[1] - b[1]) <= 1.5 * tile:
                parent[root(i)] = root(j)
    blobs: dict[int, list[Box]] = {}
    for i, box in enumerate(changed):
        blobs.setdefault(root(i), []).append(box)
    return list(blobs.values())


def reocr_rects(
    changed: list[Box],
    region: Box,
    lines: list[Line],
    limit: int = MAX_REOCR_RECTS,
    tile: float = TILE_PX,
) -> list[Box]:
    """The rectangles to read again, one Vision call each. Empty when nothing changed.

    One rectangle per blob of changed tiles, each grown until it cuts no known line, the ones that
    end up touching merged, and the count brought down to `limit` by merging the closest pairs.
    """
    rects = settled([blob_rect(blob, region, tile) for blob in tile_clusters(changed, tile)], lines, region)
    while len(rects) > limit:
        i, j = closest_pair(rects)
        rects = settled([union_box(rects[i], rects[j])] + [r for k, r in enumerate(rects) if k not in (i, j)], lines, region)
    return rects


def blob_rect(blob: list[Box], region: Box, tile: float = TILE_PX) -> Box:
    """One blob's bounding box, padded by a tile so a line crossing the edge is read whole, clamped."""
    x1 = min(b[0] for b in blob) - tile
    y1 = min(b[1] for b in blob) - tile
    x2 = max(b[2] for b in blob) + tile
    y2 = max(b[3] for b in blob) + tile
    return (max(region[0], x1), max(region[1], y1), min(region[2], x2), min(region[3], y2))


def settled(rects: list[Box], lines: list[Line], region: Box) -> list[Box]:
    """Grow every rectangle past the lines it would cut, merge the ones that meet, until neither moves.

    Growing can push two rectangles together, and a merged rectangle has edges neither original had,
    which can cut a line neither of them cut. So the two steps run to a fixed point, which they reach:
    a rectangle only ever grows, bounded by the region, and a merge only ever removes one.
    """
    while True:
        grown = [grown_for_lines(rect, lines, region) for rect in rects]
        merged = merge_touching(grown)
        if grown == rects and merged == grown:
            return merged
        rects = merged


def merge_touching(rects: list[Box]) -> list[Box]:
    """The rectangles with every overlapping or touching pair replaced by the box around both."""
    out = list(rects)
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(out):
            for j, b in enumerate(out[i + 1 :], start=i + 1):
                if boxes_touch(a, b):
                    out = [union_box(a, b)] + [r for k, r in enumerate(out) if k not in (i, j)]
                    merged = True
                    break
            if merged:
                break
    return out


def closest_pair(rects: list[Box]) -> tuple[int, int]:
    """Positions of the two rectangles with the smallest gap between them."""
    _, i, j = min((gap_between(rects[i], rects[j]), i, j) for i in range(len(rects)) for j in range(i + 1, len(rects)))
    return i, j


def gap_between(a: Box, b: Box) -> float:
    """Distance between two rectangles, zero when they overlap or touch."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def union_box(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def boxes_touch(a: Box, b: Box) -> bool:
    """Overlapping, or meeting along an edge or at a corner: worth reading as one rectangle."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def area_of(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def grown_for_lines(rect: Box, lines: list[Line], region: Box) -> Box:
    """The rectangle grown until no known line straddles its edge, clamped to the region.

    A crop cuts a line in half, and Vision reads the visible half as its own line, so a rectangle
    that ends mid-line would trade a whole headline for a fragment. Reading a larger rectangle is
    the cheaper mistake.
    """
    x1, y1, x2, y2 = rect
    growing = True
    while growing:
        growing = False
        for _, _, box in lines:
            if not boxes_intersect(box, (x1, y1, x2, y2)):
                continue
            grown = (
                max(region[0], min(x1, box[0])),
                max(region[1], min(y1, box[1])),
                min(region[2], max(x2, box[2])),
                min(region[3], max(y2, box[3])),
            )
            if grown != (x1, y1, x2, y2):
                x1, y1, x2, y2 = grown
                growing = True
    return (x1, y1, x2, y2)


def boxes_intersect(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def merge_reocr(previous: list[Line], fresh: list[Line], rects: list[Box]) -> list[Line]:
    """Previous lines no re-read rectangle touches, plus every line just read inside them."""
    return [ln for ln in previous if not any(boxes_intersect(ln[2], rect) for rect in rects)] + list(fresh)


def ax_nodes(screen: Screen, budget: int) -> tuple[list[AxNode], list[AxNode]]:
    """The frontmost app's labelled controls, in screen points, and the off-screen ones it still exposes.

    Icon-only buttons are invisible to OCR and live only here. Accessibility is best effort:
    a missing pid, a refusing app, or a raising bridge all mean OCR carries the step alone.
    """
    if screen.pid is None:
        return [], []
    width_pt, height_pt = screen.size_pt
    try:
        nodes, hidden, _capped = desktop.actionable_elements(screen.pid, width_pt, height_pt)
    except Exception:
        return [], []
    return [node for node in nodes[:budget] if node.label], [node for node in hidden if node.label]


def offscreen_controls(nodes: list[AxNode], items: list[Item]) -> list[AxNode]:
    """The off-screen controls worth offering: one per role and label, minus anything already on screen.

    An app repeats a label across a scrolled list and across the copies of a view it keeps alive, and
    the first one is as good as any since the press goes to the element. A label the visible list
    already carries is dropped outright: the item on screen is the better way to reach it.
    """
    visible = {it.text for it in items}
    seen: set[tuple[str, str]] = set()
    out: list[AxNode] = []
    for node in nodes:
        key = (node.role, node.label)
        if key in seen or node.label in visible:
            continue
        seen.add(key)
        out.append(node)
    return out


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
            role=node.role_word,
            source="ax",
        )
        for i, node in enumerate(nodes)
    ]


def ax_items(screen: Screen, budget: int) -> list[Item]:
    """The frontmost app's labelled on-screen controls as items on the capture."""
    return to_ax_items(ax_nodes(screen, budget)[0], screen.scale)


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
