"""The step loop, with per-stage timing. This is the thing being benchmarked.

Stage accounting is deliberate and literal:

* `perceive_ms` — cost of producing the element list for this step's decision.
  Zero when the previous action already handed us a fresh observation.
* `decide_ms` — the TypeSafe call.
* `act_ms` — the input event, the post-type verification call if any, **and** the
  observation-until-changed that waits for the action to land.

That last part is why waiting is nearly free: the observation the next decision
needs is the same observation that tells us the action worked. No extra round
trip, and no fixed sleep.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from ..writer import compose_browser_text
from . import act
from .decide import Decision, available_actions, decide, field_context, verify_typed
from .perceive import Page, perceive
from .report import RunFolder, render_payload


@dataclass
class Step:
    n: int
    action: str
    detail: str
    confidence: float
    satisfied: float
    elements: int
    changed: bool
    perceive_ms: float
    decide_ms: float
    act_ms: float
    total_ms: float
    text_source: str = ""

    def line(self) -> str:
        ch = "yes" if self.changed else "no "
        return (
            f"{self.n:>3}  {self.action:<12} {self.detail[:40]:<40} "
            f"conf={self.confidence:.2f} chg={ch} el={self.elements:>3}  "
            f"per={self.perceive_ms:>5.1f}  dec={self.decide_ms:>6.1f}  "
            f"act={self.act_ms:>6.1f}  tot={self.total_ms:>6.1f}ms" + (f"  text={self.text_source}" if self.text_source else "")
        )


@dataclass
class RunResult:
    goal: str
    url: str
    outcome: str
    steps: list[Step] = field(default_factory=list)
    wall_ms: float = 0.0
    url_after: str = ""

    def summary(self) -> dict:
        if not self.steps:
            return {"steps": 0}

        def pct(vals: list[float], p: float) -> float:
            s = sorted(vals)
            return round(s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))], 1)

        totals = [s.total_ms for s in self.steps]
        hot = [s.total_ms for s in self.steps[1:]] or totals  # drop cold-start step
        return {
            "steps": len(self.steps),
            "wall_ms": round(self.wall_ms, 1),
            "perceive_ms": {"p50": pct([s.perceive_ms for s in self.steps], 50)},
            "decide_ms": {"p50": pct([s.decide_ms for s in self.steps], 50), "p95": pct([s.decide_ms for s in self.steps], 95)},
            "act_ms": {"p50": pct([s.act_ms for s in self.steps], 50), "p95": pct([s.act_ms for s in self.steps], 95)},
            "total_ms": {"p50": pct(totals, 50), "p95": pct(totals, 95), "min": round(min(totals), 1)},
            "steps_per_sec_p50": round(1000 / pct(totals, 50), 2) if pct(totals, 50) else None,
            "steps_per_sec_excluding_cold_start": round(1000 / pct(hot, 50), 2) if pct(hot, 50) else None,
        }


def extract_text(goal: str, fallback: str = "") -> str:
    """Deterministic free-text extraction: quoted string, or after search/type.

    Stands in for the writer model so a benchmark step measures the decision loop
    and not a second model call. The writer is still the right answer in
    production; `--writer` in the original covers it.

    The capture stops at a clause boundary, because "type hello world into the
    field" must yield "hello world" and not the instructions around it.
    """
    import re

    boundaries = r"(?:,|\.|;| and\b| then\b| into\b| in the\b| in a\b| on the\b| to the\b| from the\b|\n)"
    for pattern in (
        r"[\"']([^\"']{1,80})[\"']",
        rf"(?:search|look)\s+(?:for|up)\s+(.{{1,80}}?)(?:{boundaries}|$)",
        rf"type\s+(?:in\s+)?(.{{1,80}}?)(?:{boundaries}|$)",
    ):
        m = re.search(pattern, goal, flags=re.IGNORECASE)
        if m:
            text = m.group(1).strip().strip("\"'").strip()
            if text:
                return text
    return fallback


def resolve_text(writer, goal: str, page: Page, target, history: list[str]) -> tuple[str, str]:
    """Free text for a field. The writer composes it; a regex is only a fallback.

    Returns (text, provenance). Provenance is recorded per step so a reviewer can
    see whether a step typed writer-composed text or a deterministic extraction -
    the distinction the CONTRIBUTING rules care about.
    """
    if writer is not None and target is not None:
        nearby = field_context(page, int(target.index))
        try:
            composed = compose_browser_text(
                writer,
                goal,
                field_label=target.label(),
                page_title=page.title,
                url=page.url,
                nearby_text=nearby,
                history=history,
            )
        except Exception as exc:
            return "", f"writer_error({type(exc).__name__})"
        if composed:
            return composed, "writer"
        return "", "writer_declined"
    return extract_text(goal), ("regex_fallback" if extract_text(goal) else "")


def run_goal(
    session,
    client: TypeSafeClient,
    goal: str,
    *,
    start_url: str | None = None,
    max_steps: int = 12,
    min_confidence: float = 0.4,
    allow_type: bool = True,
    change_timeout_ms: int = 300,
    verbose: bool = True,
    model: str | None = None,
    writer=None,
    runfolder: RunFolder | None = None,
) -> RunResult:
    result = RunResult(goal=goal, url=str(session.evaluate("location.href") or ""), outcome="incomplete")
    if start_url:
        act.navigate(session, start_url)
        act.wait_for_load(session)
        result.url = start_url

    pending: Page | None = None
    history: list[str] = []
    noops = 0
    started = time.perf_counter()

    for n in range(1, max_steps + 1):
        step_started = time.perf_counter()

        if pending is None:
            t0 = time.perf_counter()
            page: Page = perceive(session)
            perceive_ms = (time.perf_counter() - t0) * 1000
        else:
            page, perceive_ms = pending, 0.0
            pending = None

        regex_text = extract_text(goal)
        # Whether typing is even worth offering. With a writer available the text
        # can always be composed, so a field is enough. Without one we can only
        # offer typing when the goal actually contains something to type —
        # otherwise the classifier picks an action the loop cannot execute.
        text_available = page.has_field and (writer is not None or bool(regex_text))
        action_criteria = available_actions(page, allow_type=allow_type, text_available=text_available)
        element_criteria_map = {str(e.index): e.label() for e in page.items}

        t0 = time.perf_counter()
        decision: Decision = decide(
            client,
            goal,
            page,
            history,
            allow_type=allow_type,
            text=regex_text,
            text_available=text_available,
            model=model,
        )
        decide_ms = (time.perf_counter() - t0) * 1000

        if runfolder is not None:
            runfolder.step_payload(
                n,
                render_payload(
                    goal=goal,
                    page=page,
                    history=history,
                    state=decision.state,
                    actions=action_criteria,
                    elements=element_criteria_map,
                ),
            )
            runfolder.step_history(n, history)
            runfolder.step_state(n, decision.state)
            runfolder.step_answers(n, decision.answers)
            runfolder.step_elements(n, page)

        fp_before = act.fingerprint(page)
        kind = decision.kind.choice
        detail = ""
        changed = False
        text_source = ""

        t0 = time.perf_counter()
        touched = kind in {"click", "type_text", "press_enter", "press_escape", "back", "navigate", "scroll_down", "scroll_up"}
        if kind == "click":
            idx = decision.chosen_element
            element = next((e for e in page.items if e.index == idx), None)
            if element is None:
                detail = f"click {idx} -> element missing"
                noops += 1
            else:
                detail = act.click(session, int(element.index), element, page)
        elif kind == "type_text":
            idx = decision.chosen_element if decision.element else None
            target = next((e for e in page.items if e.index == idx), None)
            if target is None:
                fields = [e for e in page.items if e.tag in {"input", "textarea"}]
                target = fields[0] if fields else None
            # The classifier picked the action; the writer supplies the text.
            # When there is no writer, resolve_text falls back to a regex and
            # says so, and the step line records which one was used.
            text, text_source = resolve_text(writer, goal, page, target, history)
            if target is None or not text:
                detail = f"type -> {text_source or 'no field or no text'}"
                noops += 1
                touched = False
            else:
                act.type_text(session, int(target.index), text)
                value_now = act.field_value(session, int(target.index))
                ok = verify_typed(client, goal, target.label(), text, value_now, model=model)
                if ok < 0.5:
                    act.clear_field(session, int(target.index))
                    detail = f"type {text!r} -> verify {ok:.2f}, cleared"
                    noops += 1
                else:
                    detail = f"type {text!r} -> verify {ok:.2f}"
        elif kind == "press_enter":
            detail = act.press(session, "enter")
        elif kind == "press_escape":
            detail = act.press(session, "escape")
        elif kind == "scroll_down":
            detail = act.scroll(session, 3, page)
        elif kind == "scroll_up":
            detail = act.scroll(session, -3, page)
        elif kind == "back":
            detail = act.go_back(session)
        elif kind == "navigate":
            target_url = extract_text(goal)
            if target_url.startswith("http"):
                act.navigate(session, target_url)
            else:
                detail = "navigate -> no URL available"
                noops += 1
                touched = False
        elif kind == "wait":
            detail = "waited"
            noops += 1
            touched = False
        else:
            detail = kind
            touched = False

        if touched:
            # The wait for this action and the observation for the next decision
            # are the same call, so this costs nothing extra.
            next_page, _, changed = act.observe_until_changed(session, fp_before, timeout_ms=change_timeout_ms)
            pending = next_page
        act_ms = (time.perf_counter() - t0) * 1000

        total_ms = (time.perf_counter() - step_started) * 1000
        step = Step(
            n=n,
            action=kind,
            detail=detail,
            confidence=round(decision.confidence, 3),
            satisfied=round(float(decision.satisfied.noul), 3),
            elements=len(page.items),
            changed=changed,
            perceive_ms=perceive_ms,
            decide_ms=decide_ms,
            act_ms=act_ms,
            total_ms=total_ms,
        )
        result.steps.append(step)
        if verbose:
            print(step.line(), flush=True)
        history.append(f"{kind}: {detail}" + ("" if changed else " (page unchanged)"))

        if kind == "done" or decision.satisfied.noul >= 0.5:
            result.outcome = "done"
            break
        if kind == "none":
            result.outcome = "blocked"
            break
        if decision.confidence < min_confidence:
            result.outcome = f"low_confidence({decision.confidence:.2f})"
            break
        if noops >= 2:
            result.outcome = "stuck"
            break
    else:
        result.outcome = "max_steps"

    result.wall_ms = (time.perf_counter() - started) * 1000
    result.url_after = str(session.evaluate("location.href") or "")
    if runfolder is not None:
        runfolder.finish(
            {
                "goal": goal,
                "outcome": result.outcome,
                "url": result.url,
                "url_after": result.url_after,
                "wall_ms": result.wall_ms,
                "summary": result.summary(),
                "steps": [asdict(s) for s in result.steps],
            }
        )
        runfolder.log(f"run folder: {runfolder.root}")
    return result


def save(result: RunResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "goal": result.goal,
                "outcome": result.outcome,
                "url_after": result.url_after,
                "wall_ms": result.wall_ms,
                "summary": result.summary(),
                "steps": [asdict(s) for s in result.steps],
            },
            indent=2,
        )
    )
