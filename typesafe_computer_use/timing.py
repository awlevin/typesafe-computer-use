"""Phase stopwatches: seconds per phase in a plain dict."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

PHASE_ORDER = ("capture", "screenshot", "app", "window", "field", "url", "ocr", "ax", "decide", "act", "total")
# Neither of these is seconds: both print on the ocr phase rather than as phases of their own.
OCR_REGION_PCT = "ocr_region_pct"  # share of the capture handed to Vision
OCR_RECTS = "ocr_rects"  # how many rectangles it took, 0 for a full read or for nothing to read
EXTRAS = (OCR_REGION_PCT, OCR_RECTS)


@contextmanager
def phase(timing: dict[str, float] | None, name: str) -> Iterator[None]:
    """Record the seconds spent in the block under `name`. A None dict makes this a no-op."""
    started = time.perf_counter()
    try:
        yield
    finally:
        if timing is not None:
            timing[name] = round(time.perf_counter() - started, 3)


def ordered(timing: dict[str, float]) -> list[tuple[str, float]]:
    """Known phases first, in pipeline order, then anything unexpected."""
    known = [(name, timing[name]) for name in PHASE_ORDER if name in timing]
    return known + [(name, seconds) for name, seconds in timing.items() if name not in PHASE_ORDER]


def format_timing(timing: dict[str, float]) -> str:
    """One log line. A zero `act` means the step never acted, so it is left out.

    What Vision was given rides on the `ocr` phase: `ocr 0.31s (22% of screen, 2 rects)`. The rect
    count is left off a full read and a step with nothing to re-read, where it says nothing.
    """
    shown = [(name, s) for name, s in ordered(timing) if name not in EXTRAS and not (name == "act" and s == 0)]
    parts = [f"{name} {seconds:.2f}s" + (ocr_note(timing) if name == "ocr" else "") for name, seconds in shown]
    return "  timing: " + "  ".join(parts)


def ocr_note(timing: dict[str, float]) -> str:
    """What the ocr phase read, in parentheses, or nothing when the step did not record it."""
    pct = timing.get(OCR_REGION_PCT)
    if pct is None:
        return ""
    rects = int(timing.get(OCR_RECTS) or 0)
    return f" ({pct:.0f}% of screen" + (f", {rects} rect{'' if rects == 1 else 's'})" if rects else ")")


def summarize(timings: list[dict[str, float]]) -> dict:
    """Mean and max per phase over the steps that recorded it."""
    names = [name for name, _ in ordered(dict.fromkeys((k for t in timings for k in t), 0.0))]
    mean, peak = {}, {}
    for name in names:
        seen = [t[name] for t in timings if name in t]
        mean[name] = round(sum(seen) / len(seen), 3)
        peak[name] = round(max(seen), 3)
    return {"steps_timed": len(timings), "mean": mean, "max": peak}
