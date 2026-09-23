"""A goal still arriving while the loop acts on it, and the hooks a window follows and stops a run with."""

import threading
from types import SimpleNamespace

import pytest

from typesafe_computer_use import runner
from typesafe_computer_use.actions import Context
from typesafe_computer_use.goal import LiveGoal
from typesafe_computer_use.models import Abort
from typesafe_computer_use.platform_adapter import desktop
from typesafe_computer_use.runner import RunConfig, RunHooks, RunState, resolve

GOAL = "open the notes app"

# ------------------------------------------------------------------ a goal that is still arriving


def test_a_live_goal_grows_and_says_when_it_is_finished():
    live = LiveGoal(GOAL, listening=True)

    assert live.listening and live.text == GOAL
    assert live.add("  Then write a summary. ") == "open the notes app Then write a summary."
    live.finish()
    assert not live.listening


def test_a_correction_replaces_the_goal_rather_than_being_added_to_it():
    live = LiveGoal("Open the notes app. Then", listening=True)
    live.set("Open the notes app. Then write a summary.")
    assert live.text == "Open the notes app. Then write a summary."


def test_an_empty_thing_said_changes_nothing():
    live = LiveGoal("start here")
    assert live.add("   ") == "start here" and live.add("") == "start here"


def test_a_live_goal_is_safe_to_write_from_the_listening_thread():
    live = LiveGoal(listening=True)
    done = threading.Event()

    def talk() -> None:
        for i in range(50):
            live.add(f"sentence {i}.")
        done.set()

    threading.Thread(target=talk, daemon=True).start()
    seen = []
    while not done.is_set():
        seen.append(live.text)  # the driving thread reading while the other writes
    assert live.text.count("sentence") == 50
    assert all(text.startswith("sentence 0.") or text == "" for text in seen)


# ------------------------------------------------------------------ the loop waits instead of concluding


def context() -> Context:
    return Context(goal=GOAL, browser="Google Chrome", email=None, typesafe=None, writer=None, history=[])


def decision(kind: str, confidence: float = 0.9):
    return SimpleNamespace(kind=SimpleNamespace(choice=kind), stops=kind in ("done", "none"), confidence=confidence, chosen=kind)


@pytest.fixture
def waits(monkeypatch):
    slept = []
    monkeypatch.setattr(desktop, "sleep_watching", slept.append)
    return slept


@pytest.mark.parametrize("kind", ["done", "none"])
def test_a_half_spoken_goal_is_not_declared_finished(kind, tmp_path, screen, waits):
    state = RunState(live=LiveGoal(GOAL, listening=True))
    lines: list[str] = []

    keep_going = resolve(RunConfig(goal=GOAL, out=tmp_path), context(), state, screen, [], decision(kind), {}, lines.append)

    assert keep_going and state.holding and state.holds == 1
    assert state.outcome == "crashed"  # untouched: the run has not ended
    assert "still being spoken" in lines[0] and waits == [runner.HOLD_SECONDS]


def test_the_same_answer_ends_the_run_once_the_goal_is_complete(tmp_path, screen, waits):
    live = LiveGoal(GOAL, listening=True)
    live.finish()
    state = RunState(live=live)

    keep_going = resolve(RunConfig(goal=GOAL, out=tmp_path), context(), state, screen, [], decision("done"), {}, lambda m: None)

    assert not keep_going and state.outcome == "done" and not state.holding


def test_an_unsure_step_waits_too_rather_than_giving_up_on_half_a_sentence(tmp_path, screen, waits):
    state = RunState(live=LiveGoal(GOAL, listening=True))
    lines: list[str] = []

    keep_going = resolve(
        RunConfig(goal=GOAL, out=tmp_path), context(), state, screen, [], decision("click_item", 0.2), {}, lines.append
    )

    assert keep_going and state.holding and "still being spoken" in lines[0]


def test_a_hold_is_not_counted_as_an_action_that_changed_nothing(tmp_path, screen, waits):
    state = RunState(live=LiveGoal(GOAL, listening=True))
    state.last = ("Notes", None, None, ())

    resolve(RunConfig(goal=GOAL, out=tmp_path), context(), state, screen, [], decision("done"), {}, lambda m: None)

    assert state.last is None  # so the next capture is compared with nothing, and idle stays where it was


def test_a_run_with_no_live_goal_reads_the_one_it_was_given(tmp_path):
    state = RunState()
    assert state.goal(RunConfig(goal="typed instead", out=tmp_path)) == "typed instead"
    assert not state.listening


# ------------------------------------------------------------------ following and stopping a run


def test_a_stop_is_seen_within_a_wait_not_at_its_end(monkeypatch):
    slept: list[float] = []
    stop = threading.Event()

    def sleep(seconds):
        slept.append(seconds)
        stop.set()  # the user presses Stop during the first slice

    monkeypatch.setattr(desktop, "sleep_watching", sleep)

    with pytest.raises(Abort, match="stopped"):
        runner.settle(10.0, RunHooks(should_stop=stop.is_set))
    assert slept == [runner.STOP_POLL_SECONDS]


def test_with_nothing_to_watch_a_wait_is_the_one_it_always_was(waits):
    runner.settle(2.0, RunHooks())
    assert waits == [2.0]


def test_a_window_follows_the_run_through_its_hooks_and_can_stop_it(monkeypatch, tmp_path):
    from world import FakeTypeSafe, FakeWriter, Page, World, scripted

    world = World(
        [Page(name="home", items=["Home", "Tickets"], on={"click:Tickets": "tickets"}), Page(name="tickets", items=["Buy"])]
    )
    world.install(monkeypatch)
    monkeypatch.setattr(runner, "TypeSafeClient", lambda: FakeTypeSafe(scripted(("click_item", "Tickets"), ("done", None))))
    lines, steps, answers = [], [], []
    hooks = RunHooks(log=lines.append, on_step=steps.append, on_answer=answers.append, quiet=True)

    state = runner.run(
        RunConfig(goal="buy a ticket", out=tmp_path / "run", act=True, delay=0),
        lambda typesafe, history: Context(
            goal="buy a ticket", browser="Safari", email=None, typesafe=typesafe, writer=FakeWriter(), history=history
        ),
        hooks=hooks,
    )

    assert state.outcome == "done"
    assert [(s.kind, s.item_text, s.did) for s in steps] == [("click_item", "Tickets", "clicked 'Tickets'"), ("done", None, None)]
    assert steps[0].annotated.exists()
    assert answers == [state.answer] and any(line.strip().startswith("step 1:") for line in lines)

    stopped = runner.run(
        RunConfig(goal="buy a ticket", out=tmp_path / "stopped", act=True, delay=0),
        lambda typesafe, history: Context(
            goal="buy a ticket", browser="Safari", email=None, typesafe=typesafe, writer=None, history=history
        ),
        hooks=RunHooks(should_stop=lambda: True, quiet=True),
    )
    assert stopped.outcome == "aborted (stopped)" and stopped.history == []
