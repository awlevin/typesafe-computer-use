"""What OSWorld's runs scored, read from the result folders its runner writes.

OSWorld files each task under `<results>/<action_space>/<observation_type>/<model>/<domain>/<task_id>/`:
`result.txt` holds the score and `traj.jsonl` one line per action, with its time. jev adds its own
run folder there, `jev/`, whose `run.json` holds jev's outcome, its tokens per model, and what the
run ran on: the OCR backend, OSWorld's provider, the machine's architecture, and where the
accessibility tree came from, with how many of jev's own fetches fell back to OSWorld's. The Luna runner
writes `usage.json` there instead, with its tokens in the same shape, the seconds its model took,
and the settings it ran with, such as the reasoning effort (see `usage.py`).

`scripts/osworld results` prints this, and after it a summary per agent: its means over the tasks it
solved and, apart, over the tasks it failed, since a run stuck until the step limit skews a mean
over both, and how long a failed run took to end. It reads files only, so it runs anywhere, with no
OSWorld checkout. Earlier runs that `scripts/osworld` moved aside live under `<results>/archive/` and
are left out; point it at one of those folders to read an earlier run.

    python -m typesafe_computer_use.osworld.results [results folder]
"""

from __future__ import annotations

import itertools
import json
import statistics
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from .usage import USAGE_FILE

ARCHIVE = "archive"  # where scripts/osworld moves earlier runs, inside the results folder
RUN_FOLDER = "jev"  # jev's run folder, inside a task's result folder (see agent.py)
TIMESTAMP = "%Y%m%d@%H%M%S%f"  # how OSWorld's runner stamps each action in traj.jsonl


@dataclass(frozen=True)
class Usage:
    requests: int = 0
    input_tokens: int = 0  # uncached
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0  # part of output_tokens; only the Responses API reports them


@dataclass(frozen=True)
class Jev:
    """What jev's run.json says about the run."""

    outcome: str | None
    seconds: float | None
    ocr: str | None
    provider: str | None
    architecture: str | None
    usage: dict[str, Usage] = field(default_factory=dict)
    tree: str | None = None  # "jev-light" when jev fetched its own trees, "osworld" when they came with the observations
    tree_fallbacks: int | None = None  # jev's own fetches that OSWorld's fetch stood in for


@dataclass(frozen=True)
class UsageFile:
    """What an agent that is not jev says about its task in usage.json."""

    usage: dict[str, Usage] = field(default_factory=dict)
    seconds: float | None = None  # the model's time only, not the task's
    settings: dict[str, str] = field(default_factory=dict)  # what the model ran with, such as its reasoning effort


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
    usage: dict[str, Usage] = field(default_factory=dict)  # tokens per model: jev's run.json, or usage.json
    model_seconds: float | None = None  # usage.json's model time; None for jev, whose run.json times the whole run
    settings: dict[str, str] = field(default_factory=dict)  # usage.json's settings, such as the reasoning effort


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
    jev = _jev(folder / RUN_FOLDER / "run.json")
    own = _usage_file(folder / USAGE_FILE) if jev is None else UsageFile()
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
        jev=jev,
        usage=jev.usage if jev is not None else own.usage,
        model_seconds=own.seconds,
        settings=own.settings,
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
    usage = _usage(summary)
    fallbacks = osworld.get("tree_fallbacks")
    return Jev(
        outcome=summary.get("outcome"),
        seconds=summary.get("seconds"),
        ocr=osworld.get("ocr"),
        provider=osworld.get("provider"),
        architecture=osworld.get("architecture"),
        usage=usage,
        tree=osworld.get("tree"),
        tree_fallbacks=fallbacks if isinstance(fallbacks, int) else None,
    )


def _usage(summary: dict) -> dict[str, Usage]:
    return {
        model: Usage(**{name: int(counts.get(name) or 0) for name in Usage.__dataclass_fields__})
        for model, counts in (summary.get("usage") or {}).items()
    }


def _usage_file(path: Path) -> UsageFile:
    """An agent's own usage.json, for an agent that is not jev; empty when it wrote none."""
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
        seconds = summary.get("seconds")
        return UsageFile(
            usage=_usage(summary),
            seconds=float(seconds) if isinstance(seconds, int | float) else None,
            settings={str(name): str(value) for name, value in (summary.get("settings") or {}).items()},
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return UsageFile()


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
        tree = ""
        if jev.tree is not None:
            tree = f", tree {jev.tree}"
            if jev.tree_fallbacks:
                tree += f" ({jev.tree_fallbacks} fell back to OSWorld's fetch)"
        row("jev", f"{ended}; ocr {jev.ocr}, provider {jev.provider}, architecture {jev.architecture}{tree}")
    agent = [f"{name} {value}" for name, value in sorted(result.settings.items())]
    if result.model_seconds is not None:
        agent.insert(0, f"{duration(result.model_seconds)} of model time")
    if agent:
        row("agent", "; ".join(agent))
    label = "tokens"
    for model, used in sorted(result.usage.items()):
        reasoning = f" ({used.reasoning_tokens:,} reasoning)" if used.reasoning_tokens else ""
        row(
            label,
            f"{model}: {used.requests:,} request{'' if used.requests == 1 else 's'}, {used.input_tokens:,} in, "
            f"{used.cached_input_tokens:,} cached in, {used.output_tokens:,} out{reasoning}",
        )
        label = ""
    lines.append(f"  {'folder':<12} {result.folder}")
    return lines


def solved(result: TaskResult) -> bool | None:
    """Whether OSWorld scored the task done: a score of 1 or more is solved, below 1 failed, and a task
    with no score, or one that is not a number, is None."""
    if result.score is None:
        return None
    try:
        return float(result.score) >= 1
    except ValueError:
        return None


TOKENS = {"input_tokens": "in", "cached_input_tokens": "cached in", "output_tokens": "out", "reasoning_tokens": "reasoning"}


def _tokens(result: TaskResult, model: str, name: str) -> int | None:
    """One count of a model's tokens in a task: 0 when the task did not call that model, and None when
    the task recorded no tokens at all."""
    return getattr(result.usage.get(model, Usage()), name) if result.usage else None


def _mean(values: list, show: Callable[[float], str]) -> str:
    """The mean of the values that are known, with how many those are when some are not."""
    known = [value for value in values if value is not None]
    if not known:
        return "-"
    text = show(sum(known) / len(known))
    return text if len(known) == len(values) else f"{text} ({len(known)} of {len(values)})"


def summarize(results: list[TaskResult]) -> list[str]:
    """One agent's tasks as a few lines: the means over the tasks it solved and, apart, over those it
    failed, since a run stuck until the step limit skews a mean over both, and the median time a
    failed task took to end. A task with no score is listed, not counted."""
    won = [r for r in results if solved(r) is True]
    lost = [r for r in results if solved(r) is False]
    unscored = [r for r in results if solved(r) is None]
    lines = [
        f"{results[0].model}  summary of {len(results)} task{'' if len(results) == 1 else 's'}: "
        f"{len(won)} solved, {len(lost)} failed, {len(unscored)} not scored"
    ]
    table = [("mean per task", f"solved ({len(won)})", f"failed ({len(lost)})")]

    def row(label: str, value: Callable[[TaskResult], float | None], show: Callable[[float], str]) -> None:
        groups = [[value(r) for r in group] for group in (won, lost)]
        known = [v for group in groups for v in group if v is not None]
        if known and (any(known) or not label.endswith(" reasoning")):
            table.append((label, *(_mean(group, show) for group in groups)))

    row("steps", lambda r: r.steps, "{:.1f}".format)
    row("first to last action", lambda r: r.seconds, duration)
    # jev's run.json times its whole run; another agent's usage.json times its model's replies only.
    agent_time = "jev's run time" if any(r.jev is not None for r in results) else "model time only"
    row(agent_time, lambda r: r.jev.seconds if r.jev is not None else r.model_seconds, duration)
    for model in sorted({model for r in won + lost for model in r.usage}):
        for name, label in TOKENS.items():
            row(f"{model} {label}", partial(_tokens, model=model, name=name), "{:,.0f}".format)
    if won or lost:
        widths = [max(len(cells[i]) for cells in table) for i in range(3)]
        for label, *cells in table:
            lines.append(f"  {label:<{widths[0]}}" + "".join(f"   {c:>{w}}" for c, w in zip(cells, widths[1:], strict=True)))
    to_failure = [r.seconds for r in lost if r.seconds is not None]
    if to_failure:
        lines.append(f"  median time to failure: {duration(statistics.median(to_failure))}, from the first action to the last")
    label = "not scored"
    for r in unscored:
        lines.append(f"  {label:<12} {r.domain}/{r.task_id}")
        label = ""
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
    blocks = [describe(r) for r in results]
    blocks += [summarize(list(tasks)) for _, tasks in itertools.groupby(results, key=lambda r: r.model)]
    print("\n\n".join("\n".join(block) for block in blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
