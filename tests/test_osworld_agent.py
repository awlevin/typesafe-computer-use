"""jev behind OSWorld's agent interface, offline: the real `runner.run` on a worker thread, a scripted
classifier, fake OCR, and the hand-written Ubuntu tree as the observation.

Every wait on the worker is bounded (`STEP_SECONDS`), and the `jev` fixture resets every agent it
made, so no test leaves a worker behind or the adapter swapped.
"""

from __future__ import annotations

import ast
import json
import platform
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from world import FakeTypeSafe, FakeWriter, scripted

from typesafe_computer_use import runner
from typesafe_computer_use.decide import Decision
from typesafe_computer_use.osworld import ocr
from typesafe_computer_use.osworld.agent import SAVE_A11Y, JevAgent, describe
from typesafe_computer_use.osworld.desktop import APP_PID, OSWorldDesktop
from typesafe_computer_use.platform_adapter import current, host

FIXTURE = (Path(__file__).parent / "fixtures" / "osworld" / "ubuntu-chrome.xml").read_text()
DISPLAY = (1920, 1080)
GOAL = "open Gmail"
STEP_SECONDS = 10.0
CLICK_GMAIL = "pyautogui.click(1660, 146)"  # the center of the Gmail link's frame, (1640, 134, 40, 24)
CLEAR = "pyautogui.hotkey('ctrl', 'a'); pyautogui.press('delete')"


def png(size: tuple[int, int] = DISPLAY) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, "white").save(out, format="PNG")
    return out.getvalue()


SCREENSHOT = png()


def obs(tree: str | None = FIXTURE) -> dict:
    """An observation as OSWorld sends one for `screenshot_a11y_tree`."""
    return {"screenshot": SCREENSHOT, "accessibility_tree": tree, "instruction": GOAL}


def with_address(text: str) -> str:
    """The fixture with the address bar holding `text`."""
    return FIXTURE.replace(">https://www.google.com/</entry>", f">{text}</entry>")


def run_json(out_root: Path) -> dict:
    return json.loads((out_root / "jev" / "run.json").read_text())


def always(kind: str, target=None):
    return lambda state, questions: (kind, target)


@pytest.fixture
def jev(monkeypatch, tmp_path):
    """Makes agents over a scripted classifier and fake OCR, and stops every one of them afterwards."""
    made: list[JevAgent] = []

    def make(policy, *, lines=(), **kwargs) -> JevAgent:
        fake = FakeTypeSafe(policy)
        monkeypatch.setattr(runner, "TypeSafeClient", lambda: fake)
        monkeypatch.setitem(ocr.BACKENDS, "fake", lambda: lambda image: list(lines))
        kwargs.setdefault("writer", None)
        agent = JevAgent("fake", out_root=tmp_path, step_seconds=STEP_SECONDS, **kwargs)
        made.append(agent)
        return agent

    yield make
    for agent in made:
        worker = agent.worker
        agent.reset()
        assert worker is None or not worker.is_alive(), "reset left a worker running"
    assert current() is host, "the host adapter is back after every run"


def finish(agent: JevAgent) -> None:
    """Wait for the worker to exit once it has handed over its last list."""
    agent.worker.join(STEP_SECONDS)
    assert not agent.worker.is_alive()


# ----- predict: what jev's loop becomes, step by step ----------------------------------------


def test_a_click_decision_becomes_one_pyautogui_click(jev, tmp_path):
    agent = jev(scripted(("click_item", "Gmail")), provider="docker")

    response, actions = agent.predict(GOAL, obs())
    assert actions == [CLICK_GMAIL]
    assert response == "click_item 'Gmail' (0.90)"
    assert isinstance(current(), OSWorldDesktop), "the worker drives the VM's adapter while the run is on"

    response, actions = agent.predict(GOAL, obs())
    assert actions == ["DONE"]
    assert response == "jev ended: done"
    finish(agent)
    assert current() is host

    summary = run_json(tmp_path)
    assert summary["outcome"] == "done"
    assert summary["history"][0].startswith("clicked 'Gmail'")
    assert summary["osworld"] == {"ocr": "fake", "provider": "docker", "architecture": platform.machine()}
    assert (tmp_path / "jev" / "step-001-raw.png").exists(), "jev's usual run folder, so a step replays"


def test_the_raw_trees_are_saved_only_when_asked(jev, tmp_path, monkeypatch):
    monkeypatch.delenv(SAVE_A11Y, raising=False)
    agent = jev(scripted(("click_item", "Gmail")))
    agent.predict(GOAL, obs())
    agent.predict(GOAL, obs())
    finish(agent)
    assert not list((tmp_path / "jev").glob("obs-*")), "nothing is saved by default"


def test_the_raw_trees_go_into_the_run_folder_one_per_observation(jev, tmp_path, monkeypatch):
    monkeypatch.setenv(SAVE_A11Y, "1")
    after = with_address("https://mail.google.com/")
    agent = jev(scripted(("click_item", "Gmail")))
    agent.predict(GOAL, obs())
    assert agent.predict(GOAL, obs(after))[1] == ["DONE"]
    finish(agent)
    saved = sorted(path.name for path in (tmp_path / "jev").glob("obs-*"))
    assert saved == ["obs-000-a11y.xml", "obs-001-a11y.xml"]
    assert (tmp_path / "jev" / "obs-000-a11y.xml").read_text(encoding="utf-8") == FIXTURE
    assert (tmp_path / "jev" / "obs-001-a11y.xml").read_text(encoding="utf-8") == after


def test_a_missing_tree_saves_nothing_but_still_counts(tmp_path):
    handed: list = []

    def next_obs(actions):
        handed.append(actions)
        return obs()

    desktop = OSWorldDesktop(obs(None), lambda image: [], next_obs, save_a11y=tmp_path)
    desktop.click_at((1.0, 2.0))
    desktop.screenshot()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["obs-001-a11y.xml"]


def test_typing_and_its_check_split_into_two_steps_at_the_read(jev, tmp_path):
    agent = jev(scripted(("type_text", None)), writer=FakeWriter(text="hello world"))

    response, actions = agent.predict(GOAL, obs())
    assert actions == [CLEAR, "pyautogui.write('hello world', interval=0.02)"]
    assert response == "type_text (0.90)"

    # The check after typing reads this observation, and so does the next step's capture.
    response, actions = agent.predict(GOAL, obs(with_address("hello world")))
    assert actions == ["DONE"]
    assert response.startswith("jev ended: done")

    summary = run_json(tmp_path)
    assert summary["history"] == ["typed 'hello world' into 'Address and search bar' via keystrokes (verified 0.95)"]
    second = json.loads((tmp_path / "jev" / "step-002-answers.json").read_text())
    assert second["url"] == "hello world"


def test_a_wait_is_a_wait_step_and_a_stall_ends_in_done(jev, tmp_path):
    agent = jev(always("wait"))

    for _ in range(3):
        assert agent.predict(GOAL, obs()) == ("wait (0.90)", ["WAIT"])
    response, actions = agent.predict(GOAL, obs())

    assert actions == ["DONE"]
    assert response == "jev ended: stalled"
    assert run_json(tmp_path)["outcome"] == "stalled"


def test_done_is_done_at_once(jev, tmp_path):
    agent = jev(scripted(("done", None)))

    assert agent.predict(GOAL, obs()) == ("jev ended: done", ["DONE"])
    assert agent.predict(GOAL, obs()) == ("jev ended: done", ["DONE"]), "a run that ended stays ended"
    assert run_json(tmp_path)["steps_taken"] == 0


def test_an_exception_in_the_loop_is_raised_from_predict(jev, tmp_path):
    def broken(state, questions):
        raise RuntimeError("the classifier broke")

    agent = jev(broken)

    with pytest.raises(RuntimeError, match="the classifier broke"):
        agent.predict(GOAL, obs())
    summary = run_json(tmp_path)
    assert summary["outcome"] == "crashed"
    assert summary["osworld"]["ocr"] == "fake"
    assert agent.predict(GOAL, obs())[1] == ["DONE"]


def test_with_no_tree_ocr_alone_carries_the_decision(jev, tmp_path):
    agent = jev(scripted(("click_item", "Sign in")), lines=[("Sign in", 0.99, (800.0, 500.0, 900.0, 530.0))])

    response, actions = agent.predict(GOAL, obs(tree=None))

    assert actions == ["pyautogui.click(850, 515)"]
    assert response == "click_item 'Sign in' (0.90)"
    first = json.loads((tmp_path / "jev" / "step-001-answers.json").read_text())
    assert (first["app"], first["url"], first["field"]) == ("", None, None)
    assert [it["text"] for it in first["items"]] == ["Sign in"]


# ----- the step budget and reset --------------------------------------------------------------


@pytest.mark.parametrize(
    ("writer", "outcome"),
    [
        (None, "step limit"),  # jev's own budget, the same number, ends the loop first
        (FakeWriter(), "aborted (OSWorld step limit)"),  # the answer's capture would need one more step
    ],
    ids=["no-writer", "writer"],
)
def test_the_step_budget_hands_out_max_steps_lists_and_ends_in_done(jev, tmp_path, writer, outcome):
    agent = jev(always("click_item", "Gmail"), max_steps=2, writer=writer)

    assert agent.predict(GOAL, obs()) == ("click_item 'Gmail' (0.90)", [CLICK_GMAIL])
    response, actions = agent.predict(GOAL, obs())

    assert actions == [CLICK_GMAIL, "DONE"], "the last list still runs, and says the run is over"
    assert response == f"click_item 'Gmail' (0.90); jev ended: {outcome}"
    finish(agent)
    summary = run_json(tmp_path)
    assert summary["outcome"] == outcome
    assert "osworld" in summary


def test_reset_stops_a_waiting_worker_and_the_next_task_starts_clean(jev, tmp_path):
    agent = jev(always("click_item", "Gmail"))
    agent.predict(GOAL, obs())
    worker = agent.worker
    assert worker.is_alive()

    agent.reset()

    assert not worker.is_alive() and agent.worker is None
    assert current() is host
    assert run_json(tmp_path)["outcome"] == "aborted (reset)"

    agent.out_root = tmp_path / "task-2"
    assert agent.predict("the next task", obs())[1] == [CLICK_GMAIL]
    assert agent.worker is not worker
    assert (tmp_path / "task-2" / "jev" / "run.log").exists()


# ----- construction and OCR backends ----------------------------------------------------------


def test_jev_agent_needs_a_known_ocr_backend_before_any_run(tmp_path):
    with pytest.raises(TypeError):
        JevAgent()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match=r"unknown OCR backend 'tesseract'; known backends: .*vision"):
        JevAgent("tesseract", out_root=tmp_path, writer=None)
    assert list(tmp_path.iterdir()) == []


def test_the_writer_comes_from_the_environment_by_default(clean_env):
    clean_env.setitem(ocr.BACKENDS, "fake", lambda: lambda image: [])
    assert JevAgent("fake").writer is None


def test_vision_is_the_hosts_ocr_and_only_on_macos(monkeypatch):
    assert set(ocr.backends()) >= {"vision"}
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ocr.backend("vision") == host.recognize_text
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ocr.Unavailable, match="macOS's Vision OCR, and this machine runs linux"):
        ocr.backend("vision")


def test_describe_names_the_target_of_each_kind_of_decision():
    def answer(choice: str, confidence: float = 0.8) -> SimpleNamespace:
        return SimpleNamespace(choice=choice, confidence=confidence, probabilities={choice: confidence})

    state = {"screen_items_in_reading_order": [{"i": 3, "text": "Buy"}], "offscreen_controls": [{"k": 0, "label": "Privacy"}]}
    assert describe(Decision(answer("click_item"), answer("3", 0.6), answer("none")), state) == "click_item 'Buy' (0.60)"
    assert (
        describe(Decision(answer("press_offscreen"), None, answer("none"), answer("0")), state)
        == "press_offscreen 'Privacy' (0.80)"
    )
    assert describe(Decision(answer("use_browser"), None, answer("google")), state) == "use_browser google (0.80)"
    assert describe(Decision(answer("go_back", 0.7), None, answer("none")), state) == "go_back (0.70)"


# ----- the adapter on its own -----------------------------------------------------------------


def over(observation: dict, handed: list | None = None, then: dict | None = None) -> OSWorldDesktop:
    """An adapter whose steps land in `handed` and whose next observation is `then`."""

    def next_obs(actions: list[str]) -> dict:
        if handed is None:
            raise AssertionError("no step should end here")
        handed.append(actions)
        return then

    return OSWorldDesktop(observation, lambda image: [("text", 1.0, (0.0, 0.0, 1.0, 1.0))], next_obs)


def test_reads_before_input_use_the_obs_in_hand_and_the_first_read_after_input_ends_the_step():
    handed: list = []
    desktop = over(obs(), handed, then=obs(with_address("example.com/next")))

    assert desktop.browser_url("Google Chrome") == "https://www.google.com/"
    assert desktop.frontmost_app_and_pid() == ("Google Chrome", APP_PID)
    assert desktop.frontmost_window_bounds() == (70.0, 27.0, 1850.0, 1053.0)
    assert desktop.focused_field().label == "Address and search bar"
    assert "Gmail" in {node.label for node in desktop.actionable_elements(APP_PID, *DISPLAY)[0]}
    assert handed == []

    desktop.click_at((1660.2, 145.8))
    desktop.press("return")
    assert desktop.screenshot().size == DISPLAY

    assert handed == [[CLICK_GMAIL, "pyautogui.press('enter')"]]
    assert desktop.browser_url("Google Chrome") == "example.com/next"
    assert len(handed) == 1 and desktop.actions == []


def test_keys_map_onto_pyautogui():
    desktop = over(obs())
    desktop.press("return")
    desktop.press("escape")
    desktop.press("[", command=True)
    desktop.press("a", command=True)
    desktop.press("tab")
    desktop.press("delete")
    assert desktop.actions == [
        "pyautogui.press('enter')",
        "pyautogui.press('esc')",
        "pyautogui.hotkey('alt', 'left')",
        "pyautogui.hotkey('ctrl', 'a')",
        "pyautogui.press('tab')",
        "pyautogui.press('backspace')",
    ]


PREFIX = "import pyautogui; import time; import platform; "  # how OSWorld's prefix starts every action


def in_fake_vm(code: str, window: bool) -> list[tuple]:
    """Run one action as the VM would, after OSWorld's prefix, against stand-ins that record calls:
    wmctrl finds the browser's window when `window` is true."""
    calls: list[tuple] = []
    done = SimpleNamespace(returncode=0 if window else 1)
    modules = {
        "pyautogui": SimpleNamespace(
            hotkey=lambda *keys: calls.append(("hotkey", *keys)),
            write=lambda text, interval: calls.append(("write", text)),
            press=lambda key: calls.append(("press", key)),
        ),
        "time": SimpleNamespace(sleep=lambda seconds: calls.append(("sleep", seconds))),
        "platform": SimpleNamespace(),
        "subprocess": SimpleNamespace(
            DEVNULL=-3,
            run=lambda args, **kwargs: calls.append(("run", args, kwargs)) or done,
            Popen=lambda args, **kwargs: calls.append(("Popen", args, kwargs)),
        ),
    }
    exec(PREFIX + code, {"__builtins__": {"__import__": lambda name, *rest: modules[name]}})
    return calls


DETACHED = {"stdin": -3, "stdout": -3, "stderr": -3, "start_new_session": True}


def test_text_goes_in_only_as_a_string_literal():
    hostile = "it's \"quoted\"'); import os; os.system('true') #\nsecond line"
    desktop = over(obs())
    desktop.type_text(hostile)
    desktop.open_url("Google Chrome", hostile)

    allowed = {"pyautogui.write", "pyautogui.press", "pyautogui.hotkey", "subprocess.run", "subprocess.Popen", "time.sleep"}
    for code in desktop.actions:
        calls = [node for node in ast.walk(ast.parse(PREFIX + code)) if isinstance(node, ast.Call)]
        assert {ast.unparse(call.func) for call in calls} <= allowed
        written = [call.args[0].value for call in calls if ast.unparse(call.func) == "pyautogui.write"]
        assert written == [hostile]
    assert in_fake_vm(desktop.actions[0], window=True) == [("write", hostile)]
    assert ("write", hostile) in in_fake_vm(desktop.actions[1], window=True)
    assert in_fake_vm(desktop.actions[1], window=False)[-1] == ("Popen", ["google-chrome", hostile], DETACHED)


def test_open_url_types_into_the_raised_browser_or_starts_it_on_the_url():
    desktop = over(obs())
    desktop.open_url("Google Chrome", "https://www.bing.com/")
    (code,) = desktop.actions
    raise_it = ("run", ["wmctrl", "-xa", "google-chrome"], {})
    assert in_fake_vm(code, window=True) == [
        raise_it,
        ("sleep", 0.5),
        ("hotkey", "ctrl", "l"),
        ("write", "https://www.bing.com/"),
        ("press", "enter"),
    ]
    assert in_fake_vm(code, window=False) == [raise_it, ("Popen", ["google-chrome", "https://www.bing.com/"], DETACHED)]


def test_activate_raises_the_browser_or_starts_it_and_no_other_app():
    desktop = over(obs())
    assert desktop.activate("Google Chrome") is True
    assert desktop.activate("Files") is False
    (code,) = desktop.actions
    raise_it = ("run", ["wmctrl", "-xa", "google-chrome"], {})
    assert in_fake_vm(code, window=True) == [raise_it, ("sleep", 0.5)]
    assert in_fake_vm(code, window=False) == [raise_it, ("Popen", ["google-chrome"], DETACHED)]


def test_the_other_inputs_and_waits():
    desktop = over(obs())
    desktop.click_at((12.4, 99.6))
    desktop.clear_field()
    desktop.scroll(-10)
    desktop.sleep_watching(0)
    desktop.sleep_watching(3.0)

    assert desktop.actions == [
        "pyautogui.click(12, 100)",
        CLEAR,
        "pyautogui.scroll(-10, x=995, y=554)",  # over the active window's center
        "WAIT",
    ]
    for code in desktop.actions[:3]:
        ast.parse(PREFIX + code)


def test_with_no_tree_nothing_is_known_but_the_screenshot_and_ocr():
    desktop = over(obs(tree=None))
    assert desktop.frontmost_app_and_pid() == ("", APP_PID)
    assert desktop.frontmost_window_bounds() is None
    assert desktop.focused_field() is None
    assert desktop.browser_url("Google Chrome") is None
    assert desktop.actionable_elements(APP_PID, *DISPLAY) == ([], [], False)
    image = desktop.screenshot()
    assert image.size == DISPLAY and image.mode == "RGB"
    assert desktop.display_scale(image) == 1.0
    assert desktop.recognize_text(image) == [("text", 1.0, (0.0, 0.0, 1.0, 1.0))]
    desktop.scroll(5)
    assert desktop.actions == ["pyautogui.scroll(5)"]


def test_nothing_can_be_pressed_through_the_tree_and_nothing_opens_here():
    desktop = over(obs())
    assert desktop.ax_press(object()) is False
    assert desktop.ax_focus(object()) is False
    assert desktop.ax_set_value(object(), "hi") is False
    assert desktop.ax_value(object()) is None
    assert desktop.accessibility_trusted() is True
    assert desktop.check_abort() is None
    with pytest.raises(NotImplementedError):
        desktop.open_path(Path("state.txt"))
    assert desktop.actions == []


def test_an_observation_without_a_screenshot_says_so():
    desktop = over({"screenshot": None, "accessibility_tree": FIXTURE})
    with pytest.raises(RuntimeError, match="no screenshot"):
        desktop.screenshot()
