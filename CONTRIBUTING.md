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
- Platform calls live in `macos.py` or `windows.py` only, behind `platform_adapter.py`.
  The bounded accessibility-tree walk is shared, in `ax_walk.py`.
- Never add a path that types a password.

## Before a pull request

```
uv run ruff check . && uv run ruff format .
uv run pytest -q
```

Add a replay-based note to the PR when a change alters what the model sees:
which run folder, which step, what the decision was before and after.
