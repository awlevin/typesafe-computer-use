"""Naming the controls nobody labelled.

An icon-only button is invisible twice over: OCR finds no text in it, and the accessibility tree
carries it with an empty label. It is still a real element with a real frame; the one thing missing
is a name. A model that reads images can supply that: the unlabelled frames are boxed and numbered
on a crop of the capture, and one writer request names them all.

It costs a request, so it is asked once per layout (the same app with the same controls in the same
places is the same answer), only when the user turned it on, and only with a writer that reads images.
"""

from __future__ import annotations

from dataclasses import replace

from PIL import Image, ImageDraw, ImageFont

from .models import AxNode, Screen
from .report import FONT_PATH
from .writer import Writer, WriterError, compose_icon_names

PAD_PX = 24  # slack around the controls, so the crop shows what each icon sits beside
WHOLE_SCREEN = 0.6  # a crop bigger than this share of the capture is no crop; send it all
MIN_EDGE_PX = 240  # never a sliver: a tiny crop tells the model nothing about the app


def bounds(screen: Screen, nodes: list[AxNode]) -> tuple[int, int, int, int]:
    """The part of the capture worth sending: everything the unlabelled controls cover, plus slack.

    A window button is sixteen points across, eight pixels once a whole screen is shrunk to the edge
    a model reads: too small to recognise. The same icon in a crop of its own corner stays legible.
    """
    starts = [screen.to_pixels(node.x, node.y) for node in nodes]
    ends = [screen.to_pixels(node.x + node.w, node.y + node.h) for node in nodes]
    left = max(0, int(min(x for x, _ in starts)) - PAD_PX)
    top = max(0, int(min(y for _, y in starts)) - PAD_PX)
    right = min(screen.image.width, int(max(x for x, _ in ends)) + PAD_PX)
    bottom = min(screen.image.height, int(max(y for _, y in ends)) + PAD_PX)
    if (right - left) * (bottom - top) > WHOLE_SCREEN * screen.image.width * screen.image.height:
        return 0, 0, screen.image.width, screen.image.height
    return (
        left,
        top,
        min(max(right, left + MIN_EDGE_PX), screen.image.width),
        min(max(bottom, top + MIN_EDGE_PX), screen.image.height),
    )


def draw(screen: Screen, nodes: list[AxNode]) -> Image.Image:
    """The crop, with every unlabelled control boxed and numbered."""
    left, top, right, bottom = bounds(screen, nodes)
    crop = screen.image.crop((left, top, right, bottom)).convert("RGB")
    pen = ImageDraw.Draw(crop)
    try:
        font = ImageFont.truetype(FONT_PATH, int(13 * screen.scale))
    except OSError:
        font = ImageFont.load_default()
    for i, node in enumerate(nodes):
        x1, y1 = screen.to_pixels(node.x, node.y)
        x2, y2 = screen.to_pixels(node.x + node.w, node.y + node.h)
        box = (x1 - left, y1 - top, x2 - left, y2 - top)
        pen.rectangle(box, outline=(255, 0, 0), width=2)
        pen.text((box[0], max(0, box[1] - 15 * screen.scale)), str(i), fill=(255, 0, 0), font=font)
    return crop


class Labeller:
    """Names unlabelled controls, remembering each layout it has named. One belongs to one run.

    The key is the app plus where the controls are, so a toolbar that stays put is paid for once
    however many steps look at it, and a screen that rearranges itself is named again.
    """

    def __init__(self) -> None:
        self.seen: dict[tuple, dict[int, str]] = {}
        self.failures = 0

    def __call__(self, writer: Writer, screen: Screen, nodes: list[AxNode]) -> list[AxNode]:
        """The same controls, carrying names, less the ones the model could not place."""
        key = (screen.app, tuple((round(n.x), round(n.y), round(n.w), round(n.h)) for n in nodes))
        if key not in self.seen:
            try:
                self.seen[key] = compose_icon_names(writer, screen.app, [n.role_word for n in nodes], draw(screen, nodes))
            except WriterError:  # a failed naming costs the icons, never the step
                self.failures += 1
                self.seen[key] = {}  # asking again next step would fail the same way
        named = self.seen[key]
        return [replace(node, label=named[i]) for i, node in enumerate(nodes) if i in named]
