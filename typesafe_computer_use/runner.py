"""The step loop and the run folder."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from . import macos
from .actions import Context, is_noop, perform
from .config import DEFAULT_DELAY, DEFAULT_MIN_CONFIDENCE, DEFAULT_STEPS, MAX_OPTIONS
from .decide import Decision, decide
from .models import Abort, Item, Screen
from .perception import capture, perceive
from .report import Log, annotate, ax_count, render_payload, top
from .timing import format_timing, phase, summarize

MAX_CONSECUTIVE_NOOPS = 2


@dataclass
class RunConfig:
    goal: str
    out: Path
    act: bool = False
    steps: int = DEFAULT_STEPS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    delay: float = DEFAULT_DELAY
    image: Path | None = None  # replay a saved capture (never acts)
    app: str | None = None  # frontmost app to report during replay
    url: str | None = None  # browser URL to report during replay

    @property
    def replay(self) -> bool:
        return self.image is not None


@dataclass
class RunState:
    history: list[str] = field(default_factory=list)
    timings: list[dict[str, float]] = field(default_factory=list)
    consecutive_noops: int = 0
    last_url: str | None = None
    outcome: str = "completed"


def run(cfg: RunConfig, ctx_factory) -> RunState:
    """Drive the loop. ctx_factory(typesafe, history) builds the action Context."""
    cfg.out.mkdir(parents=True, exist_ok=True)
    log = Log(cfg.out / "run.log")
    log(f"run folder: {cfg.out}")
    if cfg.act:
        log("driving the machine. abort: Ctrl-C, or slam the mouse into the top-left corner.")

    state = RunState()
    started = time.time()
    try:
        with TypeSafeClient() as typesafe:
            ctx = ctx_factory(typesafe, state.history)
            for step in range(1, cfg.steps + 1):
                if not run_step(cfg, ctx, state, step, log):
                    break
            else:
                log(f"\nstopped after {cfg.steps} steps")
                state.outcome = "step limit"
    except (KeyboardInterrupt, Abort) as e:
        state.outcome = f"aborted ({e or 'Ctrl-C'})"
        log(f"\n{state.outcome} after {len(state.history)} actions")
    finally:
        summary = {
            "goal": cfg.goal,
            "act": cfg.act,
            "steps_taken": len(state.history),
            "outcome": state.outcome,
            "seconds": round(time.time() - started, 1),
            "timing": summarize(state.timings),
            "history": state.history,
            "config": {k: str(v) for k, v in asdict(cfg).items()},
        }
        (cfg.out / "run.json").write_text(json.dumps(summary, indent=2))
        log(f"run folder: {cfg.out}")
    return state


def run_step(cfg: RunConfig, ctx: Context, state: RunState, step: int, log: Log) -> bool:
    macos.check_abort()
    timing: dict[str, float] = {}
    started = time.perf_counter()
    with phase(timing, "capture"):
        screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser, timing)
    items = perceive(screen, MAX_OPTIONS, cfg.goal, timing)
    prefix = cfg.out / f"step-{step:02d}"
    screen.image.save(prefix.with_name(prefix.name + "-raw.png"))
    prefix.with_name(prefix.name + "-payload.txt").write_text(
        render_payload(cfg.goal, screen, items, state.history, ctx.browser, ctx.email)
    )

    with phase(timing, "decide"):
        decision = decide(ctx.typesafe, cfg.goal, screen, items, state.history, ctx.browser, ctx.email)
    by_index = {str(it.index): it for it in items}
    annotate(screen, items, decision.chosen, prefix.with_suffix(".png"))

    field_desc = f" field={screen.field.role}:{screen.field.label!r}" if screen.field else ""
    log(
        f"\nstep {step}: app={screen.app!r}{field_desc} url={screen.url!r} items={len(items)} ax={ax_count(items)} "
        f"kind={decision.kind.choice} ({decision.kind.confidence:.2f}) site={decision.site.choice}"
    )
    for key, p in top(decision.kind, 4):
        log(f"  {p:5.2f}  {key}")
    if decision.item is not None:
        log(f"  item ({decision.item.confidence:.2f}):")
        for key, p in top(decision.item, 4):
            log(f"  {p:5.2f}  [{key}] {by_index[key].text!r}")

    keep_going = resolve(cfg, ctx, state, screen, items, decision, timing, log)
    timing.setdefault("act", 0.0)
    timing["total"] = round(time.perf_counter() - started, 3)
    state.timings.append(timing)

    prefix.with_name(prefix.name + "-answers.json").write_text(json.dumps(answers(decision, screen, items, timing), indent=2))
    log(f"  files: {prefix.name}-raw.png, {prefix.name}.png, {prefix.name}-payload.txt, {prefix.name}-answers.json")
    log(format_timing(timing))

    if not keep_going:
        return False
    macos.sleep_watching(cfg.delay)
    return True


def resolve(
    cfg: RunConfig,
    ctx: Context,
    state: RunState,
    screen: Screen,
    items: list[Item],
    decision: Decision,
    timing: dict[str, float],
    log: Log,
) -> bool:
    """Apply the stop rules, then the action. True to keep looping."""
    if decision.stops:
        log(f"  model says {decision.kind.choice!r}; stopping")
        return False
    if decision.confidence < cfg.min_confidence:
        log(f"  confidence {decision.confidence:.2f} below {cfg.min_confidence}; stopping")
        return False
    if not cfg.act or cfg.replay:
        log(f"  would do: {decision.chosen}. dry run (pass --act without --image to drive the machine)")
        return False

    with phase(timing, "act"):
        what = perform(decision, screen, items, ctx)
    repeated = bool(state.history) and state.history[-1] == what and screen.url == state.last_url
    state.last_url = screen.url
    state.history.append(what)
    log(f"  did: {what}")
    if is_noop(what) or repeated:
        state.consecutive_noops += 1
        if state.consecutive_noops >= MAX_CONSECUTIVE_NOOPS:
            log(f"  {MAX_CONSECUTIVE_NOOPS} consecutive no-ops; stopping")
            state.outcome = "stalled"
            return False
    else:
        state.consecutive_noops = 0
    return True


def answers(decision: Decision, screen: Screen, items: list[Item], timing: dict[str, float]) -> dict:
    """What the classifier returned for this step, plus what it cost."""
    return {
        "kind": decision.kind.choice,
        "kind_confidence": decision.kind.confidence,
        "kind_probabilities": decision.kind.probabilities,
        "item": decision.item.choice if decision.item else None,
        "item_confidence": decision.item.confidence if decision.item else None,
        "item_probabilities": decision.item.probabilities if decision.item else None,
        "site": decision.site.choice,
        "site_probabilities": decision.site.probabilities,
        "chosen": decision.chosen,
        "confidence": decision.confidence,
        "timing": timing,
        "items": [asdict(it) for it in items],
        "field": screen.field.record() if screen.field else None,
        "app": screen.app,
        "url": screen.url,
    }
