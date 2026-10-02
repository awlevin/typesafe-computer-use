"""Route a spoken utterance: short app commands first, everything else to the clicker."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import macos

OPEN_RE = re.compile(r"^(?:open|launch|start|switch\s+to)\s+(.+)$", re.IGNORECASE)
QUIT_RE = re.compile(r"^(?:quit|close)\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class AppCommand:
    verb: str  # "open" or "quit"
    app: str


@dataclass(frozen=True)
class ScreenGoal:
    goal: str


@dataclass(frozen=True)
class DispatchResult:
    spoken: str | None = None  # say this now; None means silence (or a screen goal follows)
    screen_goal: str | None = None  # when set, hand this to the clicker


def parse_utterance(text: str) -> AppCommand | ScreenGoal:
    """Classify one spoken line. App names that start with 'the ' stay as screen goals."""
    utterance = " ".join(text.split()).strip()
    if not utterance:
        return ScreenGoal(goal="")
    if match := QUIT_RE.match(utterance):
        app = match.group(1).strip()
        if app:
            return AppCommand(verb="quit", app=app)
    if match := OPEN_RE.match(utterance):
        app = match.group(1).strip()
        # "open the Playground" is a clicker goal; "open Spotify" is a launch.
        if app and not app.lower().startswith("the "):
            return AppCommand(verb="open", app=app)
    return ScreenGoal(goal=utterance)


def dispatch(command: AppCommand | ScreenGoal) -> DispatchResult:
    """Run a direct app command, or mark a screen goal for the clicker.

    When `open -a` cannot find the app (for example "open Gmail"), the utterance falls
    through as a screen goal so the catalog and writer can still handle it.
    """
    if isinstance(command, ScreenGoal):
        return DispatchResult(screen_goal=command.goal or None)
    if command.verb == "open":
        if macos.open_app(command.app):
            return DispatchResult(spoken=f"Opening {command.app}")
        return DispatchResult(screen_goal=f"open {command.app}")
    if command.verb == "quit":
        spoken = f"Quitting {command.app}" if macos.quit_app(command.app) else f"Could not quit {command.app}"
        return DispatchResult(spoken=spoken)
    return DispatchResult(spoken=f"Unknown command {command.verb}")
