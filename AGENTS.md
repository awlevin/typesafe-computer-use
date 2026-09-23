# Agent rules

This tool drives a real computer. When you work on it, you are almost always on the
maintainer's own Mac, often while they are using it.

## Never touch the machine without explicit approval

Without the maintainer's explicit approval for that specific command, run only:

- `git`
- `uv sync`, `uv lock`
- `uv run ruff check .` and `uv run ruff format .`
- `uv run pytest` (the offline unit tests)
- `scripts/sandbox` and the `docker`/`docker compose` commands it runs, for the sandbox
  container only (`compose.yaml`, `sandbox/`). The sandbox is its own computer: it never touches
  this machine's screen, input, or apps. The run folders it writes land in `./runs`.

Everything else needs a yes first, every time. That includes:

- `clicker`, `clicker-inspect`, or any script that imports the platform adapter and calls it
- anything that moves the mouse, presses keys, clicks, scrolls, or types
- AppleScript or `osascript`, `open`, or any command that launches, activates, or quits an app
  or opens a URL or file
- launching Chrome or any browser, and anything that talks to a browser over CDP
- screen capture of any kind
- any other Docker container
- requests to local model servers (Ollama, LM Studio) or any other local service

Propose the exact command and wait. Approval covers that command once, not the kind of command
from then on. If you run subagents, give them this rule word for word.

## Tests stay off the machine

`tests/conftest.py` makes every call that would reach the machine refuse during tests: input
events, AppleScript, screen capture, `open`, and accessibility actions. The pointer reads as
mid-screen. Do not weaken or bypass that guard. A test that needs one of those calls patches it
itself. When you add a new call that reaches the machine, add it to the guard in the same change.

## Everything else

See `CONTRIBUTING.md` for how the project works and what a pull request needs.
