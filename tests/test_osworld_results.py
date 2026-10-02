"""Reading OSWorld's result folders, offline, from folders laid out as its runner writes them."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from typesafe_computer_use.osworld import results

TASK = "030eeff7-b492-4218-b312-701ec99ee0cc"


def task_folder(root: Path, model: str, observation: str, task_id: str = TASK) -> Path:
    folder = root / "pyautogui" / observation / model / "chrome" / task_id
    folder.mkdir(parents=True)
    return folder


def trajectory(folder: Path, *entries: dict) -> None:
    (folder / "traj.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def action(step: int, stamp: str, code: str = "pyautogui.click(10, 20)") -> dict:
    return {"step_num": step, "action_timestamp": stamp, "action": code, "response": "", "reward": 0, "done": False}


def jev_run(folder: Path) -> None:
    (folder / "jev").mkdir()
    summary = {
        "outcome": "done",
        "seconds": 84.2,
        "usage": {
            "typesafe-classifier": {"requests": 7, "input_tokens": 21000, "cached_input_tokens": 0, "output_tokens": 70},
            "claude-haiku-4-5": {"requests": 1, "input_tokens": 900, "cached_input_tokens": 300, "output_tokens": 40},
        },
        "osworld": {"ocr": "rapidocr", "provider": "docker", "architecture": "x86_64"},
    }
    (folder / "jev" / "run.json").write_text(json.dumps(summary), encoding="utf-8")


def test_a_jev_task_reads_score_steps_time_and_tokens(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    (folder / "result.txt").write_text("1.0\n", encoding="utf-8")
    trajectory(
        folder,
        action(1, "20260925@120000000000"),
        action(2, "20260925@120030500000", "pyautogui.write('x')"),
        action(2, "20260925@120112000000", "DONE"),
    )
    jev_run(folder)

    (result,) = results.read(tmp_path)

    assert (result.model, result.domain, result.task_id, result.observation_type) == (
        "jev",
        "chrome",
        TASK,
        "screenshot_a11y_tree",
    )
    assert result.score == "1.0"
    assert (result.steps, result.actions, result.seconds) == (2, 3, 72.0)
    assert result.jev is not None
    assert (result.jev.ocr, result.jev.provider, result.jev.architecture) == ("rapidocr", "docker", "x86_64")
    assert result.jev.usage["claude-haiku-4-5"] == results.Usage(1, 900, 300, 40)

    text = "\n".join(results.describe(result))
    assert "score        1.0" in text
    assert "observation  screenshot_a11y_tree" in text
    assert "2 (3 actions), 1m 12s from the first action to the last" in text
    assert "done after 1m 24s; ocr rapidocr, provider docker, architecture x86_64" in text
    assert "claude-haiku-4-5: 1 request, 900 in, 300 cached in, 40 out" in text
    assert "typesafe-classifier: 7 requests, 21,000 in, 0 cached in, 70 out" in text


def test_a_jev_run_says_where_its_trees_came_from_and_how_many_fell_back(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    jev_run(folder)
    summary = json.loads((folder / "jev" / "run.json").read_text(encoding="utf-8"))
    summary["osworld"].update(tree="jev-light", tree_fetches=12, tree_fallbacks=2)
    (folder / "jev" / "run.json").write_text(json.dumps(summary), encoding="utf-8")

    (result,) = results.read(tmp_path)

    assert (result.jev.tree, result.jev.tree_fallbacks) == ("jev-light", 2)
    text = "\n".join(results.describe(result))
    assert "architecture x86_64, tree jev-light (2 fell back to OSWorld's fetch)" in text


def test_an_earlier_jev_run_names_no_tree(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    jev_run(folder)
    (result,) = results.read(tmp_path)
    assert (result.jev.tree, result.jev.tree_fallbacks) == (None, None)
    assert "architecture x86_64\n" in "\n".join(results.describe(result)) + "\n"


def test_another_agents_usage_json_gives_its_tokens(tmp_path):
    folder = task_folder(tmp_path, "gpt-6-luna", "screenshot")
    (folder / "result.txt").write_text("0.0\n", encoding="utf-8")
    trajectory(folder, action(1, "20260925@120000000000"))
    usage = {"requests": 3, "input_tokens": 5000, "cached_input_tokens": 9000, "output_tokens": 800, "reasoning_tokens": 600}
    summary = {"usage": {"gpt-6-luna": usage}, "seconds": 20.0, "settings": {"reasoning_effort": "xhigh"}}
    (folder / "usage.json").write_text(json.dumps(summary), encoding="utf-8")

    (result,) = results.read(tmp_path)

    assert result.jev is None
    assert result.usage == {"gpt-6-luna": results.Usage(3, 5000, 9000, 800, 600)}
    assert (result.model_seconds, result.settings) == (20.0, {"reasoning_effort": "xhigh"})
    text = "\n".join(results.describe(result))
    assert "agent        20s of model time; reasoning_effort xhigh" in text
    assert "gpt-6-luna: 3 requests, 5,000 in, 9,000 cached in, 800 out (600 reasoning)" in text


def test_an_agent_without_usage_json_has_no_tokens(tmp_path):
    folder = task_folder(tmp_path, "gpt-6-luna", "screenshot")
    (folder / "result.txt").write_text("1.0\n", encoding="utf-8")
    (result,) = results.read(tmp_path)
    assert (result.usage, result.model_seconds, result.settings) == ({}, None, {})
    assert not any(line.lstrip().startswith(("agent", "tokens")) for line in results.describe(result)[1:])


def test_another_agent_has_no_jev_lines(tmp_path):
    folder = task_folder(tmp_path, "gpt-6-luna", "screenshot")
    (folder / "result.txt").write_text("0.0", encoding="utf-8")
    trajectory(folder, action(1, "20260925@120000000000"))

    (result,) = results.read(tmp_path)

    assert result.jev is None
    assert result.observation_type == "screenshot"
    assert not any(line.lstrip().startswith(("jev", "tokens")) for line in results.describe(result)[1:])


def test_a_run_that_never_reached_evaluation_says_so_and_shows_its_error(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    trajectory(folder, {"Error": f"chrome/{TASK} - the VM did not start"})

    (result,) = results.read(tmp_path)

    assert result.score is None
    assert (result.steps, result.actions, result.seconds) == (0, 0, None)
    text = "\n".join(results.describe(result))
    assert "none (the run never reached evaluation)" in text
    assert "the VM did not start" in text


def test_the_archive_and_stray_files_are_left_out(tmp_path):
    task_folder(tmp_path, "jev", "screenshot_a11y_tree").joinpath("result.txt").write_text("1", encoding="utf-8")
    archived = task_folder(tmp_path / "archive" / "20260925-120000", "jev", "screenshot_a11y_tree")
    archived.joinpath("result.txt").write_text("0", encoding="utf-8")
    (tmp_path / "pyautogui" / "screenshot_a11y_tree" / "jev" / "args.json").write_text("{}", encoding="utf-8")
    task_folder(tmp_path, "jev", "screenshot_a11y_tree", "empty")

    assert [r.score for r in results.read(tmp_path)] == ["1"]
    assert [r.score for r in results.read(tmp_path / "archive" / "20260925-120000")] == ["0"]


def test_broken_files_do_not_stop_the_report(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    (folder / "traj.jsonl").write_text('not json\n[1]\n{"step_num": 1, "action": "WAIT", "action_timestamp": "?"}\n')
    (folder / "jev").mkdir()
    (folder / "jev" / "run.json").write_text("{", encoding="utf-8")

    (result,) = results.read(tmp_path)

    assert (result.steps, result.actions, result.seconds) == (1, 1, None)
    assert result.jev is not None and result.jev.outcome == "run.json is not valid JSON"


def test_main_prints_every_task_or_says_there_are_none(tmp_path, capsys):
    assert results.main([str(tmp_path)]) == 0
    assert f"no OSWorld results under {tmp_path}" in capsys.readouterr().out

    task_folder(tmp_path, "jev", "screenshot_a11y_tree").joinpath("result.txt").write_text("1", encoding="utf-8")
    task_folder(tmp_path, "gpt-6-luna", "screenshot").joinpath("result.txt").write_text("0", encoding="utf-8")
    assert results.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.index(f"gpt-6-luna  chrome/{TASK}") < out.index(f"jev  chrome/{TASK}")

    assert results.main(["a", "b"]) == 2


def ran(root: Path, model: str, task_id: str, score: str | None, steps: int, seconds: int) -> Path:
    """A task folder whose trajectory takes `steps` steps over `seconds` from the first action to the last."""
    folder = task_folder(root, model, "screenshot" if model == "gpt-6-luna" else "screenshot_a11y_tree", task_id)
    if score is not None:
        (folder / "result.txt").write_text(score, encoding="utf-8")
    start = datetime(2026, 9, 25, 12)
    last = (start + timedelta(seconds=seconds)).strftime(results.TIMESTAMP)
    trajectory(folder, action(1, start.strftime(results.TIMESTAMP)), action(steps, last))
    return folder


def jev_ran(root: Path, task_id: str, score: str | None, steps: int, seconds: int, usage: dict | None) -> None:
    """A jev task; with `usage`, its run.json says jev ran 4 seconds longer than its actions did."""
    folder = ran(root, "jev", task_id, score, steps, seconds)
    if usage is not None:
        (folder / "jev").mkdir()
        summary = {"outcome": "done", "seconds": seconds + 4, "usage": usage}
        (folder / "jev" / "run.json").write_text(json.dumps(summary), encoding="utf-8")


def luna_ran(root: Path, task_id: str, score: str, steps: int, seconds: int, model_seconds: int, tokens: int) -> None:
    folder = ran(root, "gpt-6-luna", task_id, score, steps, seconds)
    usage = {"requests": steps, "input_tokens": tokens, "cached_input_tokens": 2 * tokens, "output_tokens": tokens // 10}
    summary = {"usage": {"gpt-6-luna": {**usage, "reasoning_tokens": tokens // 20}}, "seconds": model_seconds}
    (folder / "usage.json").write_text(json.dumps(summary), encoding="utf-8")


def tokens(n: int, **more: int) -> dict:
    return {"jev-1.13.0": {"requests": 1, "input_tokens": n, "output_tokens": n // 10}} | {
        model: {"requests": 1, "input_tokens": count, "output_tokens": count // 10} for model, count in more.items()
    }


def table(lines: list[str]) -> dict[str, list[str]]:
    """A summary's rows by label, from columns at least three spaces apart."""
    return {cells[0]: cells[1:] for line in lines if len(cells := re.split(r" {3,}", line.strip())) > 1}


def test_a_summary_averages_solved_and_failed_tasks_apart_and_lists_the_unscored(tmp_path, capsys):
    jev_ran(tmp_path, "a", "1.0", 4, 40, tokens(1000, **{"claude-haiku-4-5": 200}))
    jev_ran(tmp_path, "b", "1", 6, 60, tokens(3000))
    jev_ran(tmp_path, "c", "0.0", 15, 180, tokens(9000))
    jev_ran(tmp_path, "d", "0.5", 3, 20, tokens(1000))  # partly right is failed
    jev_ran(tmp_path, "f", "0", 6, 40, None)  # no run.json: its run time and tokens are unknown
    jev_ran(tmp_path, "e", None, 50, 900, tokens(90000))  # never scored: listed, not averaged
    luna_ran(tmp_path, "a", "1.0", 5, 30, 12, 4000)
    luna_ran(tmp_path, "c", "0.0", 8, 60, 20, 6000)

    assert results.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    luna, jev = (block.splitlines() for block in out.strip().split("\n\n")[-2:])

    assert jev[0] == "jev  summary of 6 tasks: 2 solved, 3 failed, 1 not scored"
    assert table(jev) == {
        "mean per task": ["solved (2)", "failed (3)"],
        "steps": ["5.0", "8.0"],
        "first to last action": ["50s", "1m 20s"],
        "jev's run time": ["54s", "1m 44s (2 of 3)"],
        "claude-haiku-4-5 in": ["100", "0 (2 of 3)"],
        "claude-haiku-4-5 cached in": ["0", "0 (2 of 3)"],
        "claude-haiku-4-5 out": ["10", "0 (2 of 3)"],
        "jev-1.13.0 in": ["2,000", "5,000 (2 of 3)"],
        "jev-1.13.0 cached in": ["0", "0 (2 of 3)"],
        "jev-1.13.0 out": ["200", "500 (2 of 3)"],
        "not scored": ["chrome/e"],
    }
    assert jev[-2:] == ["  median time to failure: 40s, from the first action to the last", "  not scored   chrome/e"]

    assert luna[0] == "gpt-6-luna  summary of 2 tasks: 1 solved, 1 failed, 0 not scored"
    assert table(luna) == {
        "mean per task": ["solved (1)", "failed (1)"],
        "steps": ["5.0", "8.0"],
        "first to last action": ["30s", "1m 00s"],
        "model time only": ["12s", "20s"],
        "gpt-6-luna in": ["4,000", "6,000"],
        "gpt-6-luna cached in": ["8,000", "12,000"],
        "gpt-6-luna out": ["400", "600"],
        "gpt-6-luna reasoning": ["200", "300"],
    }
    assert luna[-1] == "  median time to failure: 1m 00s, from the first action to the last"

    # Every task's own lines come first, as before.
    assert out.index("jev  chrome/f") < out.index("gpt-6-luna  summary")
    assert "\n".join(results.describe(results.task(tmp_path / "pyautogui/screenshot_a11y_tree/jev/chrome/c"))) in out


def test_a_summary_of_unscored_tasks_has_no_means(tmp_path):
    jev_ran(tmp_path, "a", None, 2, 10, tokens(1000))

    assert results.summarize(results.read(tmp_path)) == [
        "jev  summary of 1 task: 0 solved, 0 failed, 1 not scored",
        "  not scored   chrome/a",
    ]


def test_solved_is_a_score_of_one(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    for score, expected in (("1.0\n", True), ("1", True), ("0.99", False), ("0", False), ("", None), ("error", None)):
        (folder / "result.txt").write_text(score, encoding="utf-8")
        assert results.solved(results.task(folder)) is expected, score
    (folder / "result.txt").unlink()
    assert results.solved(results.task(folder)) is None


def test_duration():
    assert results.duration(0.4) == "0s"
    assert results.duration(59.6) == "1m 00s"
    assert results.duration(3725) == "62m 05s"
