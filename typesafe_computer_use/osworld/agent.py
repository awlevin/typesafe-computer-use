"""jev as an OSWorld agent: `reset()` and `predict(instruction, obs)`, with jev's loop unchanged inside.

OSWorld's runner calls `predict` once per step and runs the actions it returns. jev's `runner.run`
owns its own loop. So `JevAgent` runs `runner.run` as it ships, on a worker thread, inside
`platform_adapter.using(OSWorldDesktop(...))`, and the adapter turns the loop's reads and inputs
into OSWorld steps (see `desktop.py`): the inputs of one step accumulate, and the first read after
them hands them to `predict` and waits for the observation OSWorld takes after running them.

The worker never blocks forever. It is a daemon; once it has handed out `max_steps` action lists,
OSWorld asks for no more, so its next wait raises `Abort("OSWorld step limit")` instead, and
`reset()` makes a stale worker's wait raise `Abort("reset")`. Either way `runner.run` records the
outcome and writes jev's usual run folder, so `clicker --image` replays any step. When the loop
ends, whatever the outcome, `predict` answers `DONE`: never `FAIL`, which OSWorld scores as a claim
that the task is infeasible.
"""

from __future__ import annotations

import json
import platform
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import config, runner
from ..actions import Context
from ..decide import Decision
from ..models import Abort
from ..platform_adapter import using
from ..runner import RunConfig
from ..writer import Writer, make_writer
from . import ocr as ocr_backends
from .desktop import OSWorldDesktop

BROWSER = "Google Chrome"  # the browser OSWorld's Chrome tasks run
RUN_FOLDER = "jev"  # jev's run folder, inside the task's result folder
STEP_SECONDS = 600.0  # how long `predict` waits for one step before it gives up on the worker
RESET_JOIN_SECONDS = 5.0  # how long `reset` waits for a stopped worker to write its run folder
DONE = "DONE"
WAIT = "WAIT"


class _FromEnv:
    def __repr__(self) -> str:
        return "FROM_ENV"


FROM_ENV = _FromEnv()  # the writer the environment configures, as `clicker` builds it


class JevAgent:
    """jev behind OSWorld's agent interface.

    `ocr` names the OCR backend (see `ocr.py`) and is resolved here, so a bad name fails before any
    VM starts. The run folder is `<out_root>/jev`; point `out_root` at the task's result folder
    before each task. `provider` names OSWorld's VM provider, for the record. `writer` is the
    Anthropic-style client for free text and answers: by default whatever the environment
    configures, as `clicker` builds it, and None runs without one.
    """

    def __init__(
        self,
        ocr: str,
        max_steps: int = 50,
        out_root: Path = Path("jev-runs"),
        *,
        provider: str | None = None,
        writer: Writer | _FromEnv | None = FROM_ENV,
        step_seconds: float = STEP_SECONDS,
    ) -> None:
        if max_steps < 1:
            raise ValueError(f"max_steps must be at least 1, not {max_steps}")
        self.recognize_text = ocr_backends.backend(ocr)
        self.ocr = ocr
        self.max_steps = max_steps
        self.out_root = Path(out_root)
        self.provider = provider
        if isinstance(writer, _FromEnv):
            writer = make_writer()
            config.writer_vision()  # a bad value stops here, not at the run's first stop
        self.writer = writer
        self.email = config.email()
        self.step_seconds = step_seconds
        self._run: _Run | None = None
        self._stale: threading.Thread | None = None  # a stopped worker that had not finished yet

    def reset(self, runtime_logger=None, vm_ip=None) -> None:
        """Stop the previous task's worker, if any, and start the next task clean."""
        if self._run is not None:
            thread = self._run.stop(RESET_JOIN_SECONDS)
            self._stale = thread if thread is not None and thread.is_alive() else None
            self._run = None

    def predict(self, instruction: str, obs: dict) -> tuple[str, list[str]]:
        """One OSWorld step: hand `obs` to jev's loop and return its next actions.

        `response` is jev's decision for the step, the chosen action and its confidence, or the
        run's outcome once it ended. An exception in the loop is raised here.
        """
        if self._run is None:
            self._run = _Run(self, instruction, self._stale)
            self._stale = None
        return self._run.predict(obs)

    @property
    def worker(self) -> threading.Thread | None:
        """The current task's worker thread, once `predict` started it."""
        return self._run.thread if self._run is not None else None


@dataclass(frozen=True)
class _Step:
    response: str
    actions: list[str]
    last: bool = False


@dataclass(frozen=True)
class _Failed:
    error: BaseException


_STOP = object()  # handed to a waiting worker in place of an observation: the run is over


class _Run:
    """One task: the worker running jev's loop, and the two queues between it and `predict`."""

    def __init__(self, agent: JevAgent, instruction: str, after: threading.Thread | None) -> None:
        self.agent = agent
        self.cfg = RunConfig(goal=instruction, out=agent.out_root / RUN_FOLDER, act=True, steps=agent.max_steps, delay=0)
        self.after = after  # the adapter stack is the process's, so a stale worker must leave it first
        self.to_worker: queue.Queue = queue.Queue()
        self.to_predict: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.handed = 0  # action lists handed to OSWorld
        self.final: list[str] = []  # the list the step budget ran out on, handed over with DONE
        self.decision = ""  # the latest decision, as the response line
        self.ended: str | None = None  # the final response, once the run ended
        self.stopped = False

    # ----- the OSWorld side ------------------------------------------------------------------

    def predict(self, obs: dict) -> tuple[str, list[str]]:
        if self.ended is not None:
            return self.ended, [DONE]
        if self.thread is None:
            self.thread = threading.Thread(target=self._work, args=(obs,), name="jev-osworld", daemon=True)
            self.thread.start()
        else:
            self.to_worker.put(obs)
        try:
            message = self.to_predict.get(timeout=self.agent.step_seconds)
        except queue.Empty:
            self.ended = f"jev took longer than {self.agent.step_seconds:.0f}s over one step"
            self.stop(0)
            raise TimeoutError(self.ended) from None
        if isinstance(message, _Failed):
            self.ended = f"jev failed: {message.error!r}"
            raise message.error
        if message.last:
            self.ended = message.response
        return message.response, message.actions

    def stop(self, join_seconds: float) -> threading.Thread | None:
        """End the run: a worker waiting for an observation, or the next time it waits, raises
        `Abort("reset")`. Returns the worker after joining it for up to `join_seconds`."""
        self.stopped = True
        self.to_worker.put(_STOP)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(join_seconds)
        return self.thread

    # ----- the worker side -------------------------------------------------------------------

    def _work(self, obs: dict) -> None:
        message: _Step | _Failed
        try:
            if self.after is not None:
                self.after.join()
            adapter = OSWorldDesktop(obs, self.agent.recognize_text, self._next_obs)
            with using(adapter):
                state = runner.run(self.cfg, self._context)
            answer = f"; answer: {state.answer.text}" if state.answer is not None else ""
            ended = f"jev ended: {state.outcome}{answer}"
            final = [*self.final, *adapter.actions]  # what ran out the budget, or input no read ended
            response = f"{self.decision}; {ended}" if final else ended
            message = _Step(response, [*final, DONE], last=True)
        except BaseException as error:
            message = _Failed(error)
        try:
            self._record()
        except Exception as error:
            message = message if isinstance(message, _Failed) else _Failed(error)
        self.to_predict.put(message)

    def _context(self, typesafe, history: list[str]) -> Context:
        return Context(
            goal=self.cfg.goal,
            browser=BROWSER,
            email=self.agent.email,
            typesafe=_Watching(typesafe, self._decided),
            writer=self.agent.writer,
            history=history,
            ask=None,
        )

    def _decided(self, line: str) -> None:
        self.decision = line

    def _next_obs(self, actions: list[str]) -> dict:
        """Hand one step's actions to `predict` and wait for the observation after them."""
        if self.stopped:
            raise Abort("reset")
        self.handed += 1
        if self.handed >= self.agent.max_steps:
            self.final = actions  # OSWorld asks for no more after this list: it goes out with DONE
            raise Abort("OSWorld step limit")
        self.to_predict.put(_Step(self.decision, actions))
        obs = self.to_worker.get()
        if obs is _STOP:
            raise Abort("reset")
        return obs

    def _record(self) -> None:
        """Add what the run ran on to jev's run.json: the OCR backend, OSWorld's provider, and the
        machine's architecture, so results from different machines are never compared by accident."""
        path = self.cfg.out / "run.json"
        if not path.is_file():
            return
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary["osworld"] = {"ocr": self.agent.ocr, "provider": self.agent.provider, "architecture": platform.machine()}
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


class _Watching:
    """The classifier, passed through, telling the run each decision it makes, as `predict`'s response.

    Only a decision asks `kind`; the check after typing asks something else and is not one.
    """

    def __init__(self, client, decided: Callable[[str], None]) -> None:
        self._client = client
        self._decided = decided

    def system_one(self, **request):
        reply = self._client.system_one(**request)
        answers = getattr(reply, "answers", None) or {}
        if "kind" in answers:
            decision = Decision(
                kind=answers["kind"], item=answers.get("item"), site=answers.get("site"), offscreen=answers.get("offscreen")
            )
            self._decided(describe(decision, request.get("state") or {}))
        return reply


def describe(decision: Decision, state: dict) -> str:
    """A decision as one line: the action, what it acts on, and its confidence."""
    kind = decision.kind.choice
    target = ""
    if decision.clicking:
        items = {str(it.get("i")): it.get("text") for it in state.get("screen_items_in_reading_order", [])}
        target = f" {items.get(decision.item.choice, decision.item.choice)!r}"
    elif decision.pressing_offscreen:
        controls = {str(c.get("k")): c.get("label") for c in state.get("offscreen_controls", [])}
        target = f" {controls.get(decision.offscreen.choice, decision.offscreen.choice)!r}"
    elif kind == "use_browser" and decision.site is not None:
        target = f" {decision.site.choice}"
    return f"{kind}{target} ({decision.confidence:.2f})"
