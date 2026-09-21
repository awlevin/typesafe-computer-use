"""Tunables, the site catalog, and environment loading."""

from __future__ import annotations

import os
from pathlib import Path

MIN_OCR_CONFIDENCE = 0.3
MAX_OPTIONS = 255  # TypeSafe Choice ceiling
ABORT_CORNER_PX = 4
DEFAULT_MIN_CONFIDENCE = 0.4
DEFAULT_STEPS = 100
DEFAULT_DELAY = 2.0
DEFAULT_WRITER_MODEL = "claude-haiku-4-5"
DEFAULT_ANSWER_MODEL = "claude-sonnet-5"  # runs once per run, on a screenshot: worth a stronger reader
DEFAULT_BROWSER = "Google Chrome"

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

# Desktop apps the classifier can bring forward or launch by name. Anything else needs to already
# be running and visible to click, or be reachable as a website through the browser catalog above.
APPS: dict[str, str] = {
    "outlook": "Outlook",
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


def answer_model() -> str:
    return os.environ.get("CLICKER_ANSWER_MODEL", DEFAULT_ANSWER_MODEL)


def email() -> str | None:
    return os.environ.get("CLICKER_EMAIL") or None
