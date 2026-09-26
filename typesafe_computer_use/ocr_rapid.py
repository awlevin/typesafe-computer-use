"""RapidOCR on ONNX Runtime: OCR that runs anywhere, for machines without macOS Vision.

It needs the `rapidocr` extra (`uv sync --extra rapidocr`). The OSWorld benchmark runs on Linux,
so it reads the VM's screen with this backend (`--ocr rapidocr`). `recognize_text` returns what
`Desktop.recognize_text` returns: text, confidence, and a box in the image's own pixels, top to
bottom and left to right. The engine loads its models once, on the first call, since that load is
the slow part; importing this module loads nothing.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Sequence
from typing import Any

from PIL import Image
from rapidocr import RapidOCR

from .platform_adapter import OcrLine


@functools.cache
def _engine() -> RapidOCR:
    """The one engine this process uses. It logs errors only, so a run's output stays jev's own."""
    return RapidOCR(params={"Global.log_level": "error"})


def to_line(corners: Iterable[Sequence[float]], text: str, score: float) -> OcrLine:
    """One RapidOCR line as an `OcrLine`. RapidOCR gives four corner points, which need not be
    axis-aligned when the text is tilted; the box is the smallest upright one that holds them all."""
    xs, ys = zip(*((float(x), float(y)) for x, y in corners), strict=True)
    return (str(text), float(score), (min(xs), min(ys), max(xs), max(ys)))


def lines_of(output: Any) -> list[OcrLine]:
    """Every line of one RapidOCR result, top to bottom and left to right.

    The `rapidocr` package returns an object with `boxes`, `txts`, and `scores`, all None when it
    finds no text. Its predecessor, `rapidocr_onnxruntime`, returned `(result, elapse)`, where
    `result` is a list of `[corners, text, score]` or None."""
    if hasattr(output, "boxes"):
        raw = zip(output.boxes, output.txts, output.scores, strict=True) if output.boxes is not None else ()
    else:
        raw = output[0] or ()
    return reading_order([to_line(*line) for line in raw])


def reading_order(lines: list[OcrLine]) -> list[OcrLine]:
    """Top to bottom, and left to right within a row. Two lines share a row when their tops are
    closer than half the shorter one's height, so a line a few pixels higher on the right still
    reads after the one on its left."""
    ordered = sorted(lines, key=lambda line: (line[2][1], line[2][0]))
    for i in range(1, len(ordered)):
        j = i
        while j > 0 and _same_row(ordered[j - 1], ordered[j]) and ordered[j][2][0] < ordered[j - 1][2][0]:
            ordered[j - 1], ordered[j] = ordered[j], ordered[j - 1]
            j -= 1
    return ordered


def _same_row(a: OcrLine, b: OcrLine) -> bool:
    (_, ay1, _, ay2), (_, by1, _, by2) = a[2], b[2]
    return abs(ay1 - by1) < min(ay2 - ay1, by2 - by1) / 2


def recognize_text(image: Image.Image) -> list[OcrLine]:
    """RapidOCR lines as text, confidence, and a box in the image's own pixels."""
    return lines_of(_engine()(image.convert("RGB")))
