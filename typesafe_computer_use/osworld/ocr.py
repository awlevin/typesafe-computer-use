"""The OCR backends an OSWorld run can read the screen with, chosen by name for every run.

There is no default: a benchmark result depends on the OCR that read the screen, so the backend is
always named, and written into the run's `run.json`. `vision` is macOS's own OCR; `rapidocr` runs
anywhere, and is the one for a benchmark on Linux. Each entry of `BACKENDS` loads its backend
when asked, and raises `Unavailable` with the reason when it cannot run here, so a bad choice fails
before any VM starts.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from PIL import Image

from .. import platform_adapter
from ..platform_adapter import OcrLine

Recognize = Callable[[Image.Image], list[OcrLine]]  # the shape of `Desktop.recognize_text`


class Unavailable(RuntimeError):
    """A known backend that cannot run on this machine."""


def _vision() -> Recognize:
    """macOS's own Vision OCR, through the host adapter. It exists only on macOS."""
    if sys.platform != "darwin":
        raise Unavailable(f"the vision backend is macOS's Vision OCR, and this machine runs {sys.platform}")
    return platform_adapter.host.recognize_text


def _rapidocr() -> Recognize:
    """RapidOCR on ONNX Runtime, which runs anywhere. It needs the `rapidocr` extra."""
    try:
        from .. import ocr_rapid
    except ImportError as error:
        raise Unavailable(
            f"the rapidocr backend cannot load ({error}); install it with: uv sync --extra rapidocr"
            " (or pip install 'typesafe-computer-use[rapidocr]')"
        ) from error
    return ocr_rapid.recognize_text


BACKENDS: dict[str, Callable[[], Recognize]] = {"vision": _vision, "rapidocr": _rapidocr}


def backends() -> dict[str, Callable[[], Recognize]]:
    """Every known backend by name, each as the loader `backend` calls."""
    return dict(BACKENDS)


def backend(name: str) -> Recognize:
    """The recognizer for `name`. An unknown name raises ValueError listing the known ones, and a
    known one that cannot run here raises `Unavailable` with the reason."""
    load = BACKENDS.get(name)
    if load is None:
        raise ValueError(f"unknown OCR backend {name!r}; known backends: {', '.join(sorted(BACKENDS))}")
    return load()
