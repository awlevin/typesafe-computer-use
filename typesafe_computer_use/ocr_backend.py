"""OCR backend selected by the current operating system."""

from __future__ import annotations

import re
import sys
import unicodedata
from functools import lru_cache


def normalize_text(text: str) -> str:
    """Normalize OCR text without changing its bounding box.

    CJK OCR often inserts spaces between adjacent glyphs. Keep spaces inside
    Latin words, numbers, and mixed labels, but remove the spaces that split a
    Chinese label or sit before CJK punctuation.
    """
    text = unicodedata.normalize("NFKC", str(text))
    text = " ".join(text.split())
    cjk = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    closing_punctuation = ",.!?:;)]}>\u3001\u3002\uff01\uff1f\uff1a\uff1b\uff09\u3011\u300b\u300f\u300d"
    opening_punctuation = "([<{\u3010\u300a\u300e\u300c"
    text = re.sub(rf"(?<=[{cjk}])\s+(?=[{cjk}])", "", text)
    text = re.sub(rf"(?<=[{cjk}])\s+(?=[{re.escape(closing_punctuation)}])", "", text)
    text = re.sub(rf"([{re.escape(opening_punctuation)}])\s+(?=[{cjk}])", r"\1", text)
    return text.strip()


@lru_cache(maxsize=1)
def _rapidocr():
    """Load one OCR engine per process instead of once per crop."""
    from rapidocr_onnxruntime import RapidOCR

    # The game has many labels smaller than the library's general 30px
    # minimum. A lower threshold is useful for UI text; Jev still sees the
    # confidence and the outer loop has its own stop threshold.
    return RapidOCR(min_height=10, text_score=0.35)


def recognize(image):
    if sys.platform == "darwin":
        from ocrmac import ocrmac

        raw = ocrmac.OCR(image, recognition_level="accurate").recognize(px=True)
        return [(text, conf, (b[0], b[1], b[2], b[3])) for text, conf, b in raw]

    if sys.platform == "win32":
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - depends on host setup
            raise RuntimeError("Windows OCR support is missing; install rapidocr-onnxruntime and numpy") from exc
        result, _ = _rapidocr()(np.asarray(image), box_thresh=0.35, text_score=0.35)
        lines = []
        for box, text, confidence in result or []:
            text = normalize_text(text)
            if not text or float(confidence) < 0.25:
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            lines.append((text, float(confidence), (min(xs), min(ys), max(xs), max(ys))))
        return lines

    raise RuntimeError(f"unsupported OCR platform: {sys.platform}")
