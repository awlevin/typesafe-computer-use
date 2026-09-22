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
- Platform calls live in `macos.py` or `windows.py` only, behind `platform_adapter.py`.
  The bounded accessibility-tree walk is shared, in `ax_walk.py`.
- Never add a path that types a password.

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
