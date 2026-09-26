"""What OSWorld's runs scored, read from the result folders its runner writes.

OSWorld files each task under `<results>/<action_space>/<observation_type>/<model>/<domain>/<task_id>/`:
`result.txt` holds the score and `traj.jsonl` one line per action, with its time. jev adds its own
run folder there, `jev/`, whose `run.json` holds jev's outcome, its tokens per model, and what the
run ran on: the OCR backend, OSWorld's provider, and the machine's architecture.

`scripts/osworld results` prints this. It reads files only, so it runs anywhere, with no OSWorld
checkout. Earlier runs that `scripts/osworld` moved aside live under `<results>/archive/` and are
left out; point it at one of those folders to read an earlier run.

    python -m typesafe_computer_use.osworld.results [results folder]
"""

from __future__ import annotations

import json
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ARCHIVE = "archive"  # where scripts/osworld moves earlier runs, inside the results folder
RUN_FOLDER = "jev"  # jev's run folder, inside a task's result folder (see agent.py)
TIMESTAMP = "%Y%m%d@%H%M%S%f"  # how OSWorld's runner stamps each action in traj.jsonl


@dataclass(frozen=True)
class Usage:
    requests: int = 0
    input_tokens: int = 0  # uncached
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Jev:
    """What jev's run.json says about the run."""

    outcome: str | None
    seconds: float | None
    ocr: str | None
    provider: str | None
    architecture: str | None
    usage: dict[str, Usage] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskResult:
    folder: Path
    action_space: str
    observation_type: str
    model: str
    domain: str
    task_id: str
    score: str | None  # result.txt as written, or None when the run never reached evaluation
    steps: int  # OSWorld steps: one `predict` call each
    actions: int  # the actions those steps ran
    seconds: float | None  # from the first action to the last
    errors: list[str]
    jev: Jev | None  # None for an agent that is not jev


def read(root: Path) -> list[TaskResult]:
    """Every task folder under `root`, in order of model, domain, and task, leaving out the archive."""
    found = []
    for folder in sorted(root.glob("*/*/*/*/*")):
        if not folder.is_dir() or folder.relative_to(root).parts[0] == ARCHIVE:
            continue
        if not any((folder / name).exists() for name in ("result.txt", "traj.jsonl", RUN_FOLDER)):
            continue
        found.append(task(folder))
    return sorted(found, key=lambda r: (r.model, r.domain, r.task_id, r.observation_type, r.action_space))


def task(folder: Path) -> TaskResult:
    """One task's result folder."""
    action_space, observation_type, model, domain, task_id = folder.parts[-5:]
    score_file = folder / "result.txt"
    score = score_file.read_text(encoding="utf-8").strip() if score_file.is_file() else None
    steps, actions, seconds, errors = _trajectory(folder / "traj.jsonl")
    return TaskResult(
        folder=folder,
        action_space=action_space,
        observation_type=observation_type,
        model=model,
        domain=domain,
        task_id=task_id,
        score=score,
        steps=steps,
        actions=actions,
        seconds=seconds,
        errors=errors,
        jev=_jev(folder / RUN_FOLDER / "run.json"),
    )


def _trajectory(path: Path) -> tuple[int, int, float | None, list[str]]:
    """Steps, actions, seconds from the first action to the last, and the errors the runner logged."""
    if not path.is_file():
        return 0, 0, None, []
    steps, actions, errors, times = 0, 0, [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if "Error" in entry:
            errors.append(str(entry["Error"]))
        if "action" in entry:
            actions += 1
        if isinstance(entry.get("step_num"), int):
            steps = max(steps, entry["step_num"])
        with suppress(KeyError, ValueError):
            times.append(datetime.strptime(str(entry["action_timestamp"]), TIMESTAMP))
    seconds = (max(times) - min(times)).total_seconds() if times else None
    return steps, actions, seconds, errors


def _jev(path: Path) -> Jev | None:
    if not path.is_file():
        return None
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return Jev(outcome="run.json is not valid JSON", seconds=None, ocr=None, provider=None, architecture=None)
    osworld = summary.get("osworld") or {}
    usage = {
        model: Usage(**{name: int(counts.get(name) or 0) for name in Usage.__dataclass_fields__})
        for model, counts in (summary.get("usage") or {}).items()
    }
    return Jev(
        outcome=summary.get("outcome"),
        seconds=summary.get("seconds"),
        ocr=osworld.get("ocr"),
        provider=osworld.get("provider"),
        architecture=osworld.get("architecture"),
        usage=usage,
    )


def duration(seconds: float) -> str:
    """72.4 -> '1m 12s'."""
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


def describe(result: TaskResult) -> list[str]:
    """One task as a few lines of text."""
    lines = [f"{result.model}  {result.domain}/{result.task_id}"]

    def row(label: str, text: str) -> None:
        lines.append(f"  {label:<12} {text}")

    row("score", result.score if result.score is not None else "none (the run never reached evaluation)")
    row("observation", result.observation_type)
    steps = f"{result.steps} ({result.actions} actions)"
    if result.seconds is not None:
        steps += f", {duration(result.seconds)} from the first action to the last"
    row("steps", steps)
    for error in result.errors:
        row("error", error)
    if result.jev is not None:
        jev = result.jev
        ended = jev.outcome or "no outcome"
        if jev.seconds is not None:
            ended += f" after {duration(jev.seconds)}"
        row("jev", f"{ended}; ocr {jev.ocr}, provider {jev.provider}, architecture {jev.architecture}")
        label = "tokens"
        for model, used in sorted(jev.usage.items()):
            row(
                label,
                f"{model}: {used.requests:,} request{'' if used.requests == 1 else 's'}, {used.input_tokens:,} in, "
                f"{used.cached_input_tokens:,} cached in, {used.output_tokens:,} out",
            )
            label = ""
    lines.append(f"  {'folder':<12} {result.folder}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print("usage: python -m typesafe_computer_use.osworld.results [results folder]", file=sys.stderr)
        return 2
    root = Path(args[0] if args else "results")
    results = read(root)
    if not results:
        print(f"no OSWorld results under {root}")
        return 0
    print("\n\n".join("\n".join(describe(r)) for r in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
