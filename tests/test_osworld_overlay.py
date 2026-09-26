"""The files `scripts/osworld setup` copies into OSWorld, and the script itself, checked offline.

OSWorld is not installed here, so the overlay is read as source, never imported. The script runs
only as a copy in a temporary folder, with no OSWorld checkout and no .env beside it, so only its
own argument checks run: nothing it could start is there to start.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "osworld"
OVERLAY = REPO / "osworld_overlay"
AGENT = OVERLAY / "mm_agents" / "jev_agent.py"
RUNNER = OVERLAY / "scripts" / "python" / "run_multienv_jev.py"
POPEN = subprocess.Popen  # the real one, before tests/conftest.py refuses it for every test


@pytest.fixture
def processes(monkeypatch):
    """These tests run bash, on files only; tests/conftest.py refuses every process by default."""
    monkeypatch.setattr(subprocess, "Popen", POPEN)


def tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def pinned_commit() -> str:
    match = re.search(r"^OSWORLD_COMMIT=([0-9a-f]{40})$", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "scripts/osworld pins OSWORLD_COMMIT to a full commit hash"
    return match.group(1)


def test_both_overlay_files_parse():
    for path in (AGENT, RUNNER):
        tree(path)


def test_the_agent_module_only_imports_jev_agent():
    imports = [node for node in ast.walk(tree(AGENT)) if isinstance(node, ast.Import | ast.ImportFrom)]
    assert len(imports) == 1
    (only,) = imports
    assert isinstance(only, ast.ImportFrom)
    assert only.module == "typesafe_computer_use.osworld.agent"
    assert [alias.name for alias in only.names] == ["JevAgent"]


def calls(module: ast.Module, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
        if (node.func.id if isinstance(node.func, ast.Name) else node.func.attr) == name
    ]


def test_the_runner_builds_jev_agent_with_the_required_ocr():
    module = tree(RUNNER)
    (agent,) = calls(module, "JevAgent")
    assert {kw.arg: ast.unparse(kw.value) for kw in agent.keywords} == {
        "ocr": "args.ocr",
        "max_steps": "args.max_steps",
        "provider": "args.provider_name",
    }
    (ocr,) = [call for call in calls(module, "add_argument") if ast.literal_eval(call.args[0]) == "--ocr"]
    assert {kw.arg: ast.unparse(kw.value) for kw in ocr.keywords}["required"] == "True"
    assert "PromptAgent" not in RUNNER.read_text(encoding="utf-8")


def test_the_runner_points_the_agent_at_each_task_folder_before_running_it():
    source = RUNNER.read_text(encoding="utf-8")
    point = source.index("agent.out_root = Path(example_result_dir)")
    assert source.index("example_result_dir = os.path.join(") < point < source.index("lib_run_single.run_single_example(")


def test_the_runner_reads_the_aws_image_map_only_for_aws():
    """Importing OSWorld's AWS manager raises without AWS_REGION, which a Docker host never sets."""
    module = tree(RUNNER)
    (aws,) = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module == "desktop_env.providers.aws.manager"
    ]
    guards = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.If) and aws in ast.walk(node) and ast.unparse(node.test) == "args.provider_name == 'aws'"
    ]
    assert guards, "the AWS image map is read only under `if args.provider_name == 'aws'`"


def test_the_runner_names_the_commit_setup_checks_out():
    header = RUNNER.read_text(encoding="utf-8").split("from __future__", 1)[0]
    assert "scripts/python/run_multienv.py" in header
    assert pinned_commit() in header, "re-copy the runner from the commit scripts/osworld pins"


def test_the_script_parses(processes):
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_the_script_passes_shellcheck(processes):
    subprocess.run(["shellcheck", str(SCRIPT)], check=True)


@pytest.fixture
def script(tmp_path: Path, processes):
    """Run a copy of the script in an empty repo: no OSWorld checkout, no .env."""
    (tmp_path / "scripts").mkdir()
    copy = tmp_path / "scripts" / "osworld"
    shutil.copy2(SCRIPT, copy)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(copy), *args], capture_output=True, text=True, env=env, timeout=30)

    return run


def test_no_command_prints_the_usage(script):
    result = script()
    assert result.returncode == 2
    assert "run-jev DOMAIN/ID --ocr OCR" in result.stdout


def test_run_jev_refuses_to_start_without_ocr(script):
    result = script("run-jev", "chrome/some-task")
    assert result.returncode == 2
    assert "run-jev needs --ocr" in result.stderr
    assert "Backends:" in result.stderr


@pytest.mark.parametrize("task", ["chrome", "../chrome/x", "chrome/x/y", "chrome/.."])
def test_a_task_is_one_domain_and_one_id(script, task):
    result = script("run-jev", task, "--ocr", "rapidocr")
    assert result.returncode == 2
    assert "a task is DOMAIN/ID" in result.stderr


def test_a_run_needs_setup_first(script):
    result = script("run-luna", "chrome/some-task")
    assert result.returncode == 2
    assert "run scripts/osworld setup first" in result.stderr
