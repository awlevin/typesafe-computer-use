# Agent rules

This tool drives a real computer. When you work on it, you are almost always on the
maintainer's own Mac, often while they are using it.

## Never touch the machine without explicit approval

Without the maintainer's explicit approval for that specific command, run only:

- `git`
- `uv sync`, `uv lock`
- `uv run ruff check .` and `uv run ruff format .`
- `uv run pytest` (the offline unit tests)

Everything else needs a yes first, every time. That includes:

- `clicker`, `clicker-inspect`, `clicker-gui`, or any script that imports the platform adapter and calls it
- anything that moves the mouse, presses keys, clicks, scrolls, or types
- AppleScript or `osascript`, `open`, or any command that launches, activates, or quits an app
  or opens a URL or file
- launching Chrome or any browser, and anything that talks to a browser over CDP
- screen capture of any kind
- microphone capture, or anything that starts `AVAudioRecorder`
- registering a global keyboard monitor (`NSEvent.addGlobalMonitorForEventsMatchingMask_...`)
- Docker containers
- requests to local model servers (Ollama, LM Studio) or any other local service

Propose the exact command and wait. Approval covers that command once, not the kind of command
from then on. If you run subagents, give them this rule word for word.

## Tests stay off the machine

`tests/conftest.py` makes every call that would reach the machine refuse during tests: input
events, AppleScript, screen capture, `open`, the clipboard, accessibility actions, and, where
AppKit exists, the microphone, the global key monitor, system sounds, and any window put on the
screen. The pointer reads as mid-screen. Do not weaken or bypass that guard. A test that needs one
of those calls patches it itself. When you add a new call that reaches the machine, add it to the
guard in the same change, with a case in `tests/test_no_real_machine.py` asserting it refuses.

## Everything else

See `CONTRIBUTING.md` for how the project works and what a pull request needs.
