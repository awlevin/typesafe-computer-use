"""Reading OSWorld's result folders, offline, from folders laid out as its runner writes them."""

from __future__ import annotations

import json
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


def test_duration():
    assert results.duration(0.4) == "0s"
    assert results.duration(59.6) == "1m 00s"
    assert results.duration(3725) == "62m 05s"
