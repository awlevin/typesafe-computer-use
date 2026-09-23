"""Logging, annotated screenshots, and the human-readable payload dump."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from PIL import ImageDraw, ImageFont

from .decide import (
    DOUBLE_PREFIX,
    RIGHT_PREFIX,
    base_state,
    item_criteria,
    offscreen_criteria,
    screen_kind_criteria,
    site_criteria,
    target_criteria,
)
from .models import Guidance, Item, Screen

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
RULE = "=" * 78


class Log:
    """Print, append to a file, and hand each line to anything else following the run.

    `sink` is how a window follows a run: the lines the terminal gets, as they are written, with no
    second formatting of the same facts. `quiet` prints nothing, for a run with no terminal to print to.
    """

    def __init__(self, path: Path | None = None, sink: Callable[[str], None] | None = None, quiet: bool = False):
        self.path = path
        self.sink = sink
        self.quiet = quiet

    def __call__(self, msg: str = "", echo: bool = True) -> None:
        if echo and not self.quiet:
            print(msg)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(msg + "\n")
        if self.sink is not None:
            self.sink(msg)


def top(answer, n: int = 5) -> list[tuple[str, float]]:
    return sorted(answer.probabilities.items(), key=lambda kv: -kv[1])[:n]


def ax_count(items: list[Item]) -> int:
    return sum(1 for it in items if it.from_ax)


def render_payload(
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    browser: str,
    email: str | None,
    tried: list[str] | None = None,
    guidance: Guidance | None = None,
    apps: Sequence[str] = (),
    clipboard_shared: bool = False,
    listening: bool = False,
) -> str:
    """Exactly what goes to the classifier for this screen, plus a table of every item."""
    targets = target_criteria(screen, browser, apps, clipboard_shared)
    parts = [
        RULE,
        "STATE  (sent as `state`)",
        RULE,
        json.dumps(base_state(goal, screen, items, history, tried, guidance, listening), indent=2),
        "",
        RULE,
        "QUESTION kind  (Choice criteria)",
        RULE,
        json.dumps(screen_kind_criteria(browser, email, screen, items, targets, listening), indent=2),
        "",
        RULE,
        "QUESTION item  (Choice criteria)",
        RULE,
        json.dumps(item_criteria(screen, items), indent=2),
        "",
        RULE,
        "QUESTION site  (Choice criteria)",
        RULE,
        json.dumps(site_criteria(), indent=2),
        "",
    ]
    if screen.offscreen:
        parts += [
            RULE,
            "QUESTION offscreen  (Choice criteria)",
            RULE,
            json.dumps(offscreen_criteria(screen.offscreen), indent=2),
            "",
        ]
    for name, criteria in targets.items():
        parts += [RULE, f"QUESTION {name}  (Choice criteria)", RULE, json.dumps(criteria, indent=2), ""]
    parts += [
        RULE,
        f"ITEMS  ({len(items)} after merge/filter, {ax_count(items)} from the accessibility tree; "
        f"pixel boxes on the {screen.image.width}x{screen.image.height} capture, scale {screen.scale:g})",
        RULE,
    ]
    for it in items:
        cx, cy = screen.to_points(it)
        parts.append(
            f"[{it.index:3d}] src={it.source:6} role={it.role or '-':8} conf={it.ocr_confidence:.2f} "
            f"box=({it.x1:.0f},{it.y1:.0f})-({it.x2:.0f},{it.y2:.0f}) "
            f"click_pt=({cx:.0f},{cy:.0f}) {screen.region(it):13} {it.text!r}"
        )
    if screen.offscreen:
        parts += [
            "",
            RULE,
            f"OFFSCREEN CONTROLS  ({len(screen.offscreen)} the app exposes without showing; pressed through "
            "accessibility, never clicked)",
            RULE,
        ]
        parts += [f"[{i:3d}] role={node.role:22} {node.label!r}" for i, node in enumerate(screen.offscreen)]
    if screen.field:
        parts += ["", "FOCUSED FIELD", json.dumps(screen.field.record(), indent=2)]
    return "\n".join(parts) + "\n"


def annotate(screen: Screen, items: list[Item], chosen: str, out: Path) -> None:
    """Blue boxes for OCR blocks, orange for accessibility controls, red for the chosen one, green for the focused field."""
    image = screen.image.copy()
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(FONT_PATH, int(11 * screen.scale))
    except OSError:
        font = ImageFont.load_default()
    for it in items:
        hit = str(it.index) in (chosen, chosen.removeprefix(DOUBLE_PREFIX), chosen.removeprefix(RIGHT_PREFIX))
        color = (255, 0, 0) if hit else (255, 140, 0) if it.from_ax else (0, 160, 255)
        draw.rectangle((it.x1, it.y1, it.x2, it.y2), outline=color, width=3 if hit else 1)
        draw.text((it.x1, max(0, it.y1 - 12 * screen.scale)), str(it.index), fill=color, font=font)
    f = screen.field
    if f is not None:
        draw.rectangle((*screen.to_pixels(f.x, f.y), *screen.to_pixels(f.x + f.w, f.y + f.h)), outline=(0, 200, 0), width=3)
    image.save(out)
