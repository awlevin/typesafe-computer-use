"""Browser backend: DOM-driven computer use, no pixels, no OCR."""

from .cdp import CDPError, Chrome, Session, find_chrome, free_port
from .decide import BROWSER_ACTIONS, Decision, decide
from .perceive import Element, Page, perceive
from .runner import RunResult, Step, run_goal

__all__ = [
    "BROWSER_ACTIONS",
    "CDPError",
    "Chrome",
    "Decision",
    "Element",
    "Page",
    "RunResult",
    "Session",
    "Step",
    "decide",
    "find_chrome",
    "free_port",
    "perceive",
    "run_goal",
]
