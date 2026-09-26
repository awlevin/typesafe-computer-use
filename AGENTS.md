# Agent rules

This tool drives a real computer. When you work on it, you are almost always on the
maintainer's own Mac, often while they are using it.

## Never take over the machine without explicit approval

The rule is about this machine's screen, input, and apps, not about running commands. Ask first,
every time, before anything that would use or take them over:

- running the project on this machine: `clicker`, `clicker-inspect`, or any script that imports
  the platform adapter and calls it
- anything that moves the mouse, presses keys, clicks, scrolls, or types
- AppleScript or `osascript`, `open`, or any command that launches, activates, or quits an app
  or opens a URL or file
- launching Chrome or any browser, and anything that talks to a browser over CDP
- screen capture of any kind
- any Docker container other than the sandbox, and requests to local model servers (Ollama,
  LM Studio) or any other local service

Propose the exact command and wait. Approval covers that command once, not the kind of command
from then on. If you run subagents, give them this rule word for word.

Everything else needs no approval: `git`, `uv`, ruff, the offline tests, `scripts/sandbox` (its
own computer, which never touches this machine's screen, input, or apps), and ordinary CLI
commands such as `gh` or read-only `gcloud` queries. Commands that create or delete cloud
resources, or spend money, still get a yes first.

## OSWorld

`scripts/osworld setup` fetches OSWorld and installs it and jev into `.osworld/`, and
`scripts/osworld results` reads result files. Neither needs approval. `scripts/osworld run-jev`,
`run-luna`, and anything else that starts an OSWorld VM, here or in the cloud, need approval for
each command.

## Tests stay off the machine

`tests/conftest.py` makes every call that would reach the machine refuse during tests: input
events, AppleScript, screen capture, `open`, and accessibility actions. The pointer reads as
mid-screen. Do not weaken or bypass that guard. A test that needs one of those calls patches it
itself. When you add a new call that reaches the machine, add it to the guard in the same change.

## Everything else

See `CONTRIBUTING.md` for how the project works and what a pull request needs.
