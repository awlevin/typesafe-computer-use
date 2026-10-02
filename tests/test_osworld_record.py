"""A run's rows: one per task it was asked to run, from the result folders and the git state it synced."""

import json

from test_osworld_results import action, jev_run, task_folder, trajectory

from typesafe_computer_use.osworld import record

SETTINGS = {"run": "20260928T054105Z", "git_commit": "abc123", "git_dirty": False, "git_diff_sha256": None}


def test_a_scored_task_row_carries_the_run_the_score_and_the_tokens(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    (folder / "result.txt").write_text("1.0\n", encoding="utf-8")
    trajectory(folder, action(1, "20260925@120000000000"), action(2, "20260925@120030000000"))
    jev_run(folder)

    (row,) = record.rows(tmp_path, "jev", ["chrome/030eeff7-b492-4218-b312-701ec99ee0cc"], SETTINGS)

    assert row["schema"] == record.SCHEMA
    assert (row["run"], row["git_commit"], row["git_dirty"]) == ("20260928T054105Z", "abc123", False)
    assert (row["agent"], row["task"], row["score"]) == ("jev", "chrome/030eeff7-b492-4218-b312-701ec99ee0cc", 1.0)
    assert (row["steps"], row["actions"], row["seconds"], row["outcome"]) == (2, 2, 30.0, "done")
    assert row["usage"]["claude-haiku-4-5"]["cached_input_tokens"] == 300


def test_a_luna_row_carries_its_model_time_and_reasoning_effort(tmp_path):
    folder = task_folder(tmp_path, "gpt-6-luna", "screenshot")
    (folder / "result.txt").write_text("0.0\n", encoding="utf-8")
    usage = {"requests": 4, "input_tokens": 11119, "cached_input_tokens": 18049, "output_tokens": 258}
    summary = {"usage": {"gpt-6-luna": usage}, "seconds": 10.714, "settings": {"reasoning_effort": "xhigh"}}
    (folder / "usage.json").write_text(json.dumps(summary), encoding="utf-8")

    (row,) = record.rows(tmp_path, "gpt-6-luna", ["chrome/030eeff7-b492-4218-b312-701ec99ee0cc"], SETTINGS)

    assert (row["agent_seconds"], row["model_seconds"]) == (None, 10.714)
    assert row["agent_settings"] == {"reasoning_effort": "xhigh"}
    assert row["usage"]["gpt-6-luna"]["cached_input_tokens"] == 18049


def test_a_jev_row_has_no_model_time_or_agent_settings(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    (folder / "result.txt").write_text("1.0\n", encoding="utf-8")
    jev_run(folder)
    (row,) = record.rows(tmp_path, "jev", ["chrome/030eeff7-b492-4218-b312-701ec99ee0cc"], SETTINGS)
    assert (row["agent_seconds"], row["model_seconds"], row["agent_settings"]) == (84.2, None, {})


def test_a_jev_row_says_where_its_trees_came_from(tmp_path):
    folder = task_folder(tmp_path, "jev", "screenshot_a11y_tree")
    jev_run(folder)
    summary = json.loads((folder / "jev" / "run.json").read_text(encoding="utf-8"))
    summary["osworld"].update(tree="jev-light", tree_fallbacks=0)
    (folder / "jev" / "run.json").write_text(json.dumps(summary), encoding="utf-8")
    (row,) = record.rows(tmp_path, "jev", ["chrome/030eeff7-b492-4218-b312-701ec99ee0cc"], SETTINGS)
    assert (row["tree"], row["tree_fallbacks"]) == ("jev-light", 0)


def test_a_luna_row_names_no_tree(tmp_path):
    folder = task_folder(tmp_path, "gpt-6-luna", "screenshot")
    (folder / "result.txt").write_text("1.0\n", encoding="utf-8")
    (row,) = record.rows(tmp_path, "gpt-6-luna", ["chrome/030eeff7-b492-4218-b312-701ec99ee0cc"], SETTINGS)
    assert (row["tree"], row["tree_fallbacks"]) == (None, None)


def test_a_task_that_left_no_folder_still_gets_a_scoreless_row(tmp_path):
    (row,) = record.rows(tmp_path, "jev", ["chrome/missing"], SETTINGS)
    assert row["score"] is None and row["error"] == "no result folder came back"


def test_another_agents_folder_is_not_read_as_this_ones(tmp_path):
    folder = task_folder(tmp_path, "gpt-6-luna", "screenshot")
    (folder / "result.txt").write_text("1.0\n", encoding="utf-8")
    (row,) = record.rows(tmp_path, "jev", ["chrome/030eeff7-b492-4218-b312-701ec99ee0cc"], SETTINGS)
    assert row["score"] is None


def test_main_appends_one_line_per_task(tmp_path):
    out = tmp_path / "benchmarks" / "osworld" / "run.jsonl"
    for _ in range(2):
        assert record.main([str(out), str(tmp_path), "jev", json.dumps(SETTINGS), "chrome/a", "chrome/b"]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["task"] for line in lines] == ["chrome/a", "chrome/b", "chrome/a", "chrome/b"]
