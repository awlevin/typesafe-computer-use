"""Tunables, the site catalog, and environment loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

MIN_OCR_CONFIDENCE = 0.3
MAX_OPTIONS = 255  # TypeSafe Choice ceiling
ABORT_CORNER_PX = 4
DEFAULT_MIN_CONFIDENCE = 0.4
DEFAULT_STEPS = 100
DEFAULT_DELAY = 2.0
DEFAULT_HANDOFFS = 10  # each one is a call to the answer model, a few seconds and a few cents
DEFAULT_WRITER_MODEL = "claude-haiku-4-5"
DEFAULT_ANSWER_MODEL = "claude-sonnet-5"  # runs only when the classifier stops, on a screenshot: worth a stronger reader
DEFAULT_BROWSER = "Google Chrome"
ANTHROPIC_HOST = "api.anthropic.com"
WRITER_APIS = ("anthropic", "openai")
MAX_MENU_ITEMS = 120  # the frontmost app's menu commands read per step
CLIPBOARD_CHARS = 500  # how much of a shared clipboard the classifier is shown


@dataclass(frozen=True)
class KeyAction:
    """One keystroke the classifier can ask for, named by what it does rather than by its chord."""

    key: str  # a key both adapters press
    command: bool = False
    shift: bool = False
    description: str = ""


# The keyboard as intentions, for press_key. The chords are the Mac's own and mean the same thing in
# most apps; the Windows adapter spells each as Windows does. Each description names the keys, so a
# wrong guess in an unusual app reads plainly in the log. Nothing here duplicates a fixed action:
# Back is go_back, a page at a time is scroll_down and scroll_up, Return and Escape are their own.
KEY_ACTIONS: dict[str, KeyAction] = {
    "save": KeyAction("s", command=True, description="Save the document being worked on (Command-S)."),
    "undo": KeyAction("z", command=True, description="Undo the last change (Command-Z). Use it to take back a wrong move."),
    "redo": KeyAction("z", command=True, shift=True, description="Redo what was just undone (Shift-Command-Z)."),
    "copy": KeyAction("c", command=True, description="Copy the selection to the clipboard (Command-C)."),
    "cut": KeyAction("x", command=True, description="Cut the selection to the clipboard (Command-X)."),
    "paste": KeyAction("v", command=True, description="Paste the clipboard into the focused field (Command-V)."),
    "select_all": KeyAction("a", command=True, description="Select everything in the focused field or document (Command-A)."),
    "find": KeyAction("f", command=True, description="Open the app's find bar (Command-F), so a search term can be typed next."),
    "new": KeyAction("n", command=True, description="Make a new document, note, message or window (Command-N)."),
    "new_tab": KeyAction("t", command=True, description="Open a new tab (Command-T)."),
    "close": KeyAction("w", command=True, description="Close the front tab or window (Command-W). Unsaved work may be lost."),
    "forward": KeyAction("]", command=True, description="Go forward again after going back (Command-])."),
    "reload": KeyAction("r", command=True, description="Reload the page (Command-R)."),
    "next_field": KeyAction("tab", description="Move to the next field or control (Tab)."),
    "previous_field": KeyAction("tab", shift=True, description="Move to the previous field or control (Shift-Tab)."),
    "backspace": KeyAction("delete", description="Delete the character or selection before the caret in the focused field."),
    "up": KeyAction("up", description="Press the up arrow: move the caret, or the highlight in a list or menu."),
    "down": KeyAction("down", description="Press the down arrow: move the caret, or the highlight in a list or menu."),
    "left": KeyAction("left", description="Press the left arrow."),
    "right": KeyAction("right", description="Press the right arrow."),
    "to_top": KeyAction("up", command=True, description="Jump to the very top of the document or list (Command-Up)."),
    "to_bottom": KeyAction("down", command=True, description="Jump to the very bottom of the document or list (Command-Down)."),
    "zoom_in": KeyAction("=", command=True, description="Make the content larger (Command-+)."),
    "zoom_out": KeyAction("-", command=True, description="Make the content smaller (Command--)."),
}
FIELD_KEYS = ("paste", "backspace")  # they change a field's text, so they need one that could be typed into

# Sites the classifier can pick by name. Anything else goes through the writer.
SITES: dict[str, str] = {
    "github": "https://github.com/",
    "gmail": "https://mail.google.com/",
    "google_calendar": "https://calendar.google.com/",
    "launchdarkly": "https://app.launchdarkly.com/",
    "linear": "https://linear.app/",
    "notion": "https://www.notion.so/",
    "slack": "https://app.slack.com/",
    "typesafe_console": "https://console.typesafe.ai/",
}


def load_dotenv(path: Path) -> None:
    """Set KEY=VALUE lines from a .env file into the environment unless already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def browser() -> str:
    return os.environ.get("CLICKER_BROWSER", DEFAULT_BROWSER)


def writer_model() -> str:
    return os.environ.get("CLICKER_WRITER_MODEL", DEFAULT_WRITER_MODEL)


def writer_api() -> str:
    """The API the writer's endpoint speaks: `anthropic` (Messages) or `openai` (Chat Completions)."""
    api = os.environ.get("CLICKER_WRITER_API", "").strip().lower() or "anthropic"
    if api not in WRITER_APIS:
        raise ValueError(f"CLICKER_WRITER_API must be one of {', '.join(WRITER_APIS)}, not {api!r}")
    return api


def writer_base_url() -> str | None:
    """The endpoint in CLICKER_WRITER_BASE_URL, or None to leave it to the Anthropic SDK.

    The SDK appends the rest of the path, so the full request URL works too: `.../v1/messages`
    comes off for the Anthropic API, `.../chat/completions` for the OpenAI one, which keeps its `/v1`.
    ANTHROPIC_BASE_URL is not read here: the SDK reads it together with the Anthropic credentials,
    so a key and the endpoint it was meant for always travel as a pair.
    """
    raw = os.environ.get("CLICKER_WRITER_BASE_URL")
    if not raw:
        return None
    base = raw.strip().rstrip("/")
    suffixes = ("/chat/completions",) if writer_api() == "openai" else ("/v1/messages", "/messages", "/v1")
    for suffix in suffixes:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/") or None


def custom_writer_endpoint() -> bool:
    """Whether the writer talks to anything but Anthropic's own API, by either variable."""
    if writer_api() == "openai":
        return True
    url = writer_base_url() or os.environ.get("ANTHROPIC_BASE_URL")
    return bool(url) and urlparse(url).hostname != ANTHROPIC_HOST


def writer_vision() -> bool:
    """Whether the answer model gets the screenshot. Off for a model that reads text only."""
    raw = os.environ.get("CLICKER_WRITER_VISION", "").strip().lower() or "true"
    if raw not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError(f"CLICKER_WRITER_VISION must be true or false, not {raw!r}")
    return raw in ("true", "1", "yes")


def answer_model() -> str:
    return os.environ.get("CLICKER_ANSWER_MODEL", DEFAULT_ANSWER_MODEL)


def email() -> str | None:
    return os.environ.get("CLICKER_EMAIL") or None
