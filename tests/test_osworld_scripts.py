"""scripts/osworld and scripts/osworld-gcp take several tasks per run, and never reach a machine here.

scripts/osworld runs only where OSWorld is set up, so its task list is checked by running that one
function alone. scripts/osworld-gcp is checked through --dry-run, which prints each command and runs
none.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OSWORLD = ROOT / "scripts" / "osworld"
OSWORLD_GCP = ROOT / "scripts" / "osworld-gcp"

POPEN = subprocess.Popen  # the real one, before tests/conftest.py refuses it for every test

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="the scripts are bash")


@pytest.fixture(autouse=True)
def processes(monkeypatch):
    """These tests run bash on files only; tests/conftest.py refuses every process by default."""
    monkeypatch.setattr(subprocess, "Popen", POPEN)


def function(script: Path, name: str) -> str:
    """One shell function's source, from its `name() {` line to the closing brace at column one."""
    match = re.search(rf"^{name}\(\) {{\n.*?^}}\n", script.read_text(), re.S | re.M)
    assert match, f"{script.name} has no {name}()"
    return match.group(0)


def task_list(tmp_path: Path, *tasks: str) -> tuple[Path, dict]:
    source = function(OSWORLD, "task_meta")
    out = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; REPO="$1"; shift; {source}\ntask_meta "$@"', "bash", str(tmp_path), *tasks],
        capture_output=True,
        text=True,
        check=True,
    )
    meta = Path(out.stdout.strip())
    return meta, json.loads(meta.read_text())


def test_one_task_is_its_own_list(tmp_path):
    meta, tasks = task_list(tmp_path, "chrome/bb5e4c0d")
    assert meta == tmp_path / ".osworld" / "tasks" / "chrome__bb5e4c0d.json"
    assert tasks == {"chrome": ["bb5e4c0d"]}


def test_several_tasks_group_by_domain_in_the_order_given(tmp_path):
    meta, tasks = task_list(tmp_path, "chrome/b", "os/x", "chrome/a")
    assert meta.name.startswith("list-")
    assert list(tasks.items()) == [("chrome", ["b", "a"]), ("os", ["x"])]


def test_a_different_list_gets_a_different_file(tmp_path):
    first, _ = task_list(tmp_path, "chrome/a", "chrome/b")
    second, _ = task_list(tmp_path, "chrome/a", "chrome/c")
    assert first != second


def dry_run(tmp_path: Path, *args: str) -> str:
    """scripts/osworld-gcp --dry-run in a bare copy of the repo, so no cache or result of this checkout is read.

    A dry run runs nothing, and should it ever try, gcloud, terraform, and rsync here only fail.
    """
    tmp_path.mkdir(exist_ok=True)
    shims = tmp_path / "bin"
    shims.mkdir()
    for tool in ("gcloud", "terraform", "rsync"):
        (shims / tool).write_text(f"#!/bin/sh\necho 'a dry run ran {tool}' >&2\nexit 99\n")
        (shims / tool).chmod(0o755)
    (tmp_path / "scripts").mkdir()
    shutil.copy(OSWORLD_GCP, tmp_path / "scripts" / "osworld-gcp")
    (tmp_path / ".osworld").mkdir()
    (tmp_path / ".osworld" / "gcp.json").write_text(
        '{\n  "project": "p",\n  "zone": "z",\n  "instance": "osworld",\n  "ssh": "iap"\n}\n'
    )
    out = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "osworld-gcp"), "--dry-run", *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={"PATH": f"{shims}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    return out.stderr


def test_the_cloud_run_passes_every_task_to_the_machine(tmp_path):
    printed = dry_run(tmp_path, "run-jev", "chrome/a", "chrome/b", "--ocr", "rapidocr")
    assert "osworld-exec scripts/osworld run-jev chrome/a chrome/b --ocr rapidocr" in printed


def test_the_cloud_run_lives_on_the_machine_not_in_the_connection(tmp_path):
    printed = dry_run(tmp_path, "run-jev", "chrome/a", "--ocr", "rapidocr")
    assert "setsid nohup bash -c" in printed, "the run is its own session on the machine"
    assert ".osworld/runs/" in printed and ".status" in printed, "its log and exit status stay on the machine"
    assert re.search(r"tail -n \+1 -F --pid=4242 /opt/typesafe-computer-use/\.osworld/runs/\d{8}T\d{6}Z\.log", printed), (
        "and this end follows that run's own log"
    )
    assert "ServerAliveInterval=15" in printed, "noticing a stalled stream instead of hanging on it"


def test_the_detached_start_returns_at_once_and_leaves_its_pid_log_and_status(tmp_path):
    """The start command, run for real in a folder of its own with an osworld-exec that takes a moment."""
    bin_dir, repo = tmp_path / "bin", tmp_path / "repo"
    bin_dir.mkdir()
    repo.mkdir()
    (bin_dir / "osworld-exec").write_text('#!/bin/sh\necho "ran $*"\nsleep 1\nexit 3\n')
    (bin_dir / "osworld-exec").chmod(0o755)
    if shutil.which("setsid") is None:  # macOS has none; the Linux machine does
        (bin_dir / "setsid").write_text('#!/bin/sh\nexec "$@"\n')
        (bin_dir / "setsid").chmod(0o755)
    source = "\n".join(function(OSWORLD_GCP, name) for name in ("quote", "detach_command"))
    command = subprocess.run(
        [
            "bash",
            "-c",
            f'RUNS_DIR=.osworld/runs\n{source}\ndetach_command "$1" run1 "scripts/osworld run-jev chrome/a"',
            "bash",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    started = time.monotonic()
    out = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True, env=env, timeout=10)
    assert time.monotonic() - started < 0.9, "the start returns before the run ends"
    runs = repo / ".osworld" / "runs"
    assert out.stdout.strip() == (runs / "run1.pid").read_text().strip()
    deadline = time.monotonic() + 10
    while not (runs / "run1.status").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert (runs / "run1.status").read_text().strip() == "3"
    assert (runs / "run1.log").read_text().strip() == "ran scripts/osworld run-jev chrome/a"


def test_the_cloud_run_records_its_rows_with_the_git_state_it_synced(tmp_path):
    printed = dry_run(tmp_path, "run-jev", "chrome/a", "--ocr", "rapidocr")
    (line,) = [line for line in printed.splitlines() if "typesafe_computer_use.osworld.record" in line]
    assert "benchmarks/osworld/" in line and " results jev " in line and line.endswith(" chrome/a")
    assert '"git_commit"' in line and '"git_dirty"' in line and '"command": "run-jev chrome/a --ocr rapidocr"' in line


def provenance(repo: Path) -> dict:
    """scripts/osworld-gcp's provenance(), run alone in `repo`, as the JSON it prints."""
    unsynced = re.search(r"^UNSYNCED=\(.*\)$", OSWORLD_GCP.read_text(), re.M)
    assert unsynced, "osworld-gcp has no UNSYNCED"
    source = "\n".join(function(OSWORLD_GCP, name) for name in ("provenance", "diff_digest"))
    out = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; INSTANCE=m\n{unsynced.group(0)}\n{source}\nprovenance run1 'run-luna chrome/a'"],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo,
    )
    return json.loads(out.stdout)


def test_a_run_reads_as_changed_for_new_code_but_not_for_the_rows_of_earlier_runs(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, check=True)

    git("init", "-q")
    (tmp_path / "code.py").write_text("x = 1\n")
    git("add", "code.py")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "code")
    rows = tmp_path / "benchmarks" / "osworld"
    rows.mkdir(parents=True)
    (rows / "20260928T060716Z.jsonl").write_text('{"score": 1.0}\n')

    clean = provenance(tmp_path)
    assert (clean["git_dirty"], clean["git_diff_sha256"]) == (False, None), "a rows file is a record, not code"
    assert clean["command"] == "run-luna chrome/a" and clean["machine"] == "m"

    (tmp_path / "code.py").write_text("x = 2\n")
    changed = provenance(tmp_path)
    assert changed["git_dirty"] is True and re.fullmatch(r"[0-9a-f]{64}", changed["git_diff_sha256"])


def test_every_row_says_how_long_osworld_waited_after_each_action(tmp_path):
    """Runs up to 2026-09-29 waited 2 s after each action, later ones none: the rows keep them apart."""
    script = tmp_path / "scripts" / "osworld"
    script.parent.mkdir()
    shutil.copy(OSWORLD, script)
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "s"]):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false", *args], capture_output=True, check=True)
    assert isinstance(provenance(tmp_path)["sleep_after_execution"], int | float), "the wait scripts/osworld sets"
    script.write_text("MAX_STEPS=50\nSLEEP_AFTER_EXECUTION=2.0\n")
    assert provenance(tmp_path)["sleep_after_execution"] == 2.0
    script.write_text("SLEEP_AFTER_EXECUTION=${OSWORLD_WAIT:-2}\n")
    assert provenance(tmp_path)["sleep_after_execution"] is None, "a value it cannot read is not a guess"


def test_attach_follows_the_newest_run_and_cancel_stops_its_whole_session(tmp_path):
    assert "tail -n +1 -F --pid=4242 /opt/typesafe-computer-use/.osworld/runs/20260101T000000Z.log" in dry_run(
        tmp_path / "a", "attach"
    )
    assert "kill -TERM -- -4242" in dry_run(tmp_path / "b", "cancel")


def test_the_cloud_run_moves_aside_the_local_copy_of_each_task(tmp_path):
    for task in ("chrome/a", "chrome/b"):
        (tmp_path / "results" / "pyautogui" / "screenshot" / "jev" / task).mkdir(parents=True)
    printed = dry_run(tmp_path, "run-jev", "chrome/a", "chrome/b", "--ocr", "rapidocr")
    moved = [line for line in printed.splitlines() if line.startswith("+ mv ")]
    assert len(moved) == 2 and all("results/archive/" in line for line in moved)


def test_the_cloud_run_needs_a_task(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as failed:
        dry_run(tmp_path, "run-jev", "--ocr", "rapidocr")
    assert "usage: scripts/osworld-gcp run-jev DOMAIN/ID... --ocr OCR" in failed.value.stderr


def one_line(script: Path, name: str) -> str:
    """A shell function written on one line, such as `say() { ...; }`."""
    match = re.search(rf"^{name}\(\) {{ .* }}$", script.read_text(), re.M)
    assert match, f"{script.name} has no one-line {name}()"
    return match.group(0)


def stopped_under_run() -> str:
    match = re.search(r"^STOPPED_UNDER_RUN=(\d+)", OSWORLD_GCP.read_text(), re.M)
    assert match, "osworld-gcp has no STOPPED_UNDER_RUN"
    return match.group(1)


def follow_with(tmp_path: Path, machine: str) -> subprocess.CompletedProcess:
    """follow() alone, against a machine SSH cannot reach, which Compute Engine describes as `machine`.

    An empty `machine` is a describe that fails too, as it does with no network here.
    """
    shims = tmp_path / "bin"
    shims.mkdir()
    describe = f"echo {machine}" if machine else "echo 'network unreachable' >&2; exit 1"
    (shims / "gcloud").write_text(
        f'#!/bin/sh\ncase "$*" in\n  *"instances describe"*) {describe} ;;\n  *) echo "connection timed out" >&2; exit 255 ;;\nesac\n'
    )
    (shims / "gcloud").chmod(0o755)
    source = "\n".join(
        [one_line(OSWORLD_GCP, name) for name in ("say", "show")]
        + [function(OSWORLD_GCP, name) for name in ("die", "quote", "cmdline", "query", "machine_status", "alive", "follow")]
    )
    setup = (
        "set -euo pipefail; DRY_RUN=false; INSTANCE=osworld; GC=(--project p --zone z); SSH=(gcloud compute ssh osworld)\n"
        "REMOTE_USER=osworld; REMOTE_DIR=/opt/repo; RUNS_DIR=.osworld/runs; KEEPALIVE=(); RECONNECTS=0\n"
        f"STOPPED_UNDER_RUN={stopped_under_run()}\n"
    )
    return subprocess.run(
        ["bash", "-c", f'{setup}{source}\nstatus=0; follow run1 4242 || status=$?; echo "follow returned $status"'],
        capture_output=True,
        text=True,
        env={"PATH": f"{shims}:/usr/bin:/bin", "HOME": str(tmp_path)},
        timeout=30,
    )


@pytest.mark.parametrize("machine", ["TERMINATED", "STOPPING", "SUSPENDED"])
def test_a_machine_stopped_under_the_run_ends_the_following_at_once(tmp_path, machine):
    """Google stopped the Spot machine nine minutes into run 20260928T175847Z, and the run with it.
    A stopped machine cannot be reached, which the follower took for a dropped connection: it tried
    again for twenty minutes, then gave up without bringing back the tasks that had finished."""
    out = follow_with(tmp_path, machine)
    assert out.stdout.strip() == f"follow returned {stopped_under_run()}"
    assert "the machine stopped during the run" in out.stderr
    assert "reconnecting" not in out.stderr and "lost the run's output" not in out.stderr


@pytest.mark.parametrize("machine", ["RUNNING", ""], ids=["running", "describe fails too"])
def test_a_machine_that_is_up_or_unknown_is_reconnected_to(tmp_path, machine):
    """A running machine that does not answer, or no answer about it at all, is a dropped connection."""
    out = follow_with(tmp_path, machine)
    assert out.returncode == 1 and "lost the run's output 0 times" in out.stderr
    assert "the machine stopped during the run" not in out.stderr


def test_a_run_the_machine_stopped_under_brings_back_what_finished():
    """run-jev and attach start the machine again after such a run, before they pull its results."""
    assert "start_if_stopped" in function(OSWORLD_GCP, "restart_if_stopped_under")
    attach = re.search(r"^  attach\)\n.*?;;\n", OSWORLD_GCP.read_text(), re.S | re.M)
    assert attach, "osworld-gcp has no attach command"
    for body in (function(OSWORLD_GCP, "run_task"), attach.group(0)):
        assert body.index("restart_if_stopped_under") < body.index("pull_results")


def test_a_pull_the_tunnel_drops_is_tried_again(tmp_path):
    """IAP dropped the pull after run 20260928T184152Z ("rsync: unexpected end of file"), and every
    one of its ten scored tasks was recorded as no result folder came back."""
    shims = tmp_path / "bin"
    shims.mkdir()
    tries = tmp_path / "tries"
    (shims / "rsync").write_text(f'#!/bin/sh\necho x >> {tries}\n[ "$(wc -l < {tries})" -ge 3 ]\n')
    (shims / "rsync").chmod(0o755)
    source = "\n".join(
        [one_line(OSWORLD_GCP, name) for name in ("say", "show")]
        + [function(OSWORLD_GCP, name) for name in ("quote", "cmdline", "run", "pull_results")]
    )
    setup = "set -euo pipefail; DRY_RUN=false; INSTANCE=osworld; REMOTE_DIR=/opt/repo; RSYNC=(rsync); sleep() { :; }\n"

    def pull(limit: int) -> subprocess.CompletedProcess:
        tries.write_text("")
        return subprocess.run(
            ["bash", "-c", f'{setup}PULL_TRIES={limit}\n{source}\npull_results && echo pulled || echo "not pulled"'],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={"PATH": f"{shims}:/usr/bin:/bin", "HOME": str(tmp_path)},
            timeout=30,
        )

    out = pull(4)
    assert out.stdout.strip() == "pulled" and len(tries.read_text().split()) == 3
    assert "trying again (2 of 4)" in out.stderr
    out = pull(2)
    assert out.stdout.strip() == "not pulled" and len(tries.read_text().split()) == 2


def test_a_run_whose_results_did_not_come_back_records_no_rows(tmp_path):
    """Rows saying no task came back would be false: the tasks are scored on the machine. The run
    says how to record them once pull-results has brought them, and ends as failed."""
    stubs = (
        "load() { :; }; start_if_stopped() { :; }; wait_for_machine() { :; }; sync_repo() { :; }\n"
        "archive_local() { :; }; start_detached() { echo 4242; }; follow() { :; }; restart_if_stopped_under() { :; }\n"
        'run_status() { echo 0; }; provenance() { echo \'{"run": "r"}\'; }; pull_results() { return 1; }\n'
        "record() { echo RECORDED; }; run() { :; }; SSH=(gcloud); REMOTE_EXEC=osworld-exec\n"
    )
    record_line = re.search(r"^RECORD=\(.*\)$", OSWORLD_GCP.read_text(), re.M)
    assert record_line, "osworld-gcp has no RECORD"
    source = "\n".join(
        [one_line(OSWORLD_GCP, name) for name in ("say", "agent_model")]
        + [record_line.group(0)]
        + [function(OSWORLD_GCP, name) for name in ("die", "quote", "cmdline", "run_task")]
    )
    out = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; DRY_RUN=false; TASK_PATTERN='^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'\n{source}\n{stubs}"
            'status=0; run_task run-jev chrome/a --ocr rapidocr || status=$?; echo "run_task returned $status"',
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        timeout=30,
    )
    assert out.stdout.strip() == "run_task returned 1", out.stderr
    assert "the run's rows were not recorded" in out.stderr
    assert re.search(
        r"python -m typesafe_computer_use\.osworld\.record benchmarks/osworld/\d{8}T\d{6}Z\.jsonl results jev .*chrome/a$",
        out.stderr,
        re.M,
    )


def test_pulling_results_starts_a_stopped_machine(tmp_path):
    printed = dry_run(tmp_path, "pull-results")
    assert printed.index("instances start osworld") < printed.index("rsync")
