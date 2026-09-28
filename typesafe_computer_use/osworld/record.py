"""One JSON line per task a cloud run scored, with what it takes to trace the number to its code.

`scripts/osworld-gcp` writes `benchmarks/osworld/<run>.jsonl` after each run, from the result folders
it pulled back and the git state it synced. Rows are never edited: a rerun is a new file. They are
not committed automatically; committing them is the deliberate step that makes a result part of the
record, and a row whose code was not committed (`git_dirty`) says so, with a hash of the diff.

    python -m typesafe_computer_use.osworld.record RUN_FILE RESULTS AGENT_MODEL SETTINGS_JSON TASK...

`SETTINGS_JSON` holds the git state and the run's settings, as `scripts/osworld-gcp` gathers them.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import results

SCHEMA = 1


def rows(root: Path, model: str, tasks: list[str], settings: dict) -> list[dict]:
    """A row per task, from its result folder under `root`; a task with no folder still gets one, scoreless."""
    found = {(r.domain, r.task_id): r for r in results.read(root) if r.model == model}
    out = []
    for task in tasks:
        domain, task_id = task.split("/", 1)
        result = found.get((domain, task_id))
        row = {"schema": SCHEMA, **settings, "agent": model, "task": task}
        if result is None:
            out.append({**row, "score": None, "error": "no result folder came back"})
            continue
        out.append(
            {
                **row,
                "score": float(result.score) if result.score not in (None, "") else None,
                "steps": result.steps,
                "actions": result.actions,
                "seconds": result.seconds,  # from the first action to the last
                "outcome": result.jev.outcome if result.jev else None,
                "agent_seconds": result.jev.seconds if result.jev else None,
                "usage": {name: asdict(used) for name, used in result.usage.items()},
                "errors": result.errors,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 5:
        print(
            "usage: python -m typesafe_computer_use.osworld.record RUN_FILE RESULTS AGENT_MODEL SETTINGS_JSON TASK...",
            file=sys.stderr,
        )
        return 2
    run_file, root, model, settings, *tasks = args
    path = Path(run_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows(Path(root), model, tasks, json.loads(settings)):
            f.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"recorded {len(tasks)} task{'' if len(tasks) == 1 else 's'} in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
