"""Phase stopwatches: seconds per phase in a plain dict."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

PHASE_ORDER = ("capture", "screenshot", "app", "field", "url", "ocr", "ax", "decide", "act", "total")


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
    """One log line. A zero `act` means the step never acted, so it is left out."""
    shown = [(name, s) for name, s in ordered(timing) if not (name == "act" and s == 0)]
    return "  timing: " + "  ".join(f"{name} {seconds:.2f}s" for name, seconds in shown)


def summarize(timings: list[dict[str, float]]) -> dict:
    """Mean and max per phase over the steps that recorded it."""
    names = [name for name, _ in ordered(dict.fromkeys((k for t in timings for k in t), 0.0))]
    mean, peak = {}, {}
    for name in names:
        seen = [t[name] for t in timings if name in t]
        mean[name] = round(sum(seen) / len(seen), 3)
        peak[name] = round(max(seen), 3)
    return {"steps_timed": len(timings), "mean": mean, "max": peak}
