# Contributing

Bug reports with a run folder attached are the most useful thing you can send.
`runs/<timestamp>/` holds the capture, the exact payload, and every probability,
so a stall can be replayed offline with `--image`.

## Ground rules

- Keep the action set mutually exclusive. Two options that mean the same thing
  split the vote and read as low confidence.
- The classifier picks; code decides facts. Anything the model would have to
  compute (dates, URL validity, whether a field is focused) is computed in code
  and handed over as state.
- Free text only ever comes from `writer.py`, with a structured reply and a
  code-side guard.
- The writer never picks an action. When the classifier stops it may name a focus
  or ask the user, and the classifier takes every step from there. Watch the
  `calls:` line: a change that moves work to the writer should say why.
- Platform calls live in the adapters, `macos.py` and `windows.py`, only. Everything else
  reaches them through `platform_adapter.desktop`, and a new adapter call goes into the
  `Desktop` protocol and both adapters together. The accessibility-tree walk is shared, in
  `ax_walk.py`.
- Never add a path that types a password.

## Providers, and a goal still being spoken

Two parts the rules above did not have to cover yet.

- One client shape per request. The classifier is anything that answers `system_one`, TypeSafe's
  client or `chat_classifier.ChatClassifier`; the writer is anything that answers
  `messages.create` the Anthropic way, the Anthropic SDK or `OpenAIWriter`. A new provider on
  either API is a row in `settings.py`'s catalog, not a client of its own. Requests go through
  the metered clients the runner builds, so every one lands on the `calls:` line, including any
  a new feature adds.
- The environment beats the settings file, and the file beats the catalog. A key only ever goes
  to the endpoint it was given for.
- Anything that shows the classifier more of the user's machine than the screen, the clipboard
  for one, is off until the user turns it on.
- A dictated goal is a `LiveGoal`: the runner reads it after each capture, and replaces nothing
  itself. Whoever listens sets it whole each time, never appends, since the transcriber revises
  what it heard. While it is still arriving, a stop is a hold: no step spent, no outcome set.
- The window in `gui/` is a macOS front end, not an adapter. It reaches the machine being driven
  only through `platform_adapter.desktop`; its own reach (the microphone, the global key monitor,
  sounds, windows on screen) goes through one named call each, and the test guard refuses them.

## A task the loop cannot do

Write it as a scenario in `tests/test_scenarios.py`: a page graph on the simulated
computer in `tests/world.py`, a policy standing in for the classifier, and assertions on
the outcome, the final page, and the actions the world received. Leave it failing under
`xfail` until the loop can do it, then fix the loop rather than the scenario. The
scenarios run the real `runner.run`, so a failure there is an architecture finding, not
a harness one.

## Before a pull request

```
uv run ruff check . && uv run ruff format .
uv run pytest -q
```

Add a replay-based note to the PR when a change alters what the model sees:
which run folder, which step, what the decision was before and after.
