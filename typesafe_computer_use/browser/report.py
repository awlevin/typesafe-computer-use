"""Browser run folder: the same replayability contract as the original, minus pixels.

The original replays a stall from `step-NN-raw.png`. A browser step has no pixels
to replay, so the captured artefact is the element list itself. Everything else
matches: the run log, the exact payload sent to TypeSafe, and every probability
that came back. `load_step` reads a folder back so a decision can be re-run
offline without touching a browser at all.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .perceive import Element, Page, TextBlock

RULE = "=" * 78


class RunFolder:
    """One directory per run, shaped like the original's `runs/<timestamp>/`."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "run.log"
        self.lines: list[str] = []

    @classmethod
    def create(cls, base: str | Path = "runs", *, stamp: str | None = None) -> RunFolder:
        import time

        name = stamp or time.strftime("%Y%m%d-%H%M%S")
        path = Path(base) / name
        suffix = 1
        while path.exists():
            path = Path(base) / f"{name}-{suffix}"
            suffix += 1
        return cls(path)

    # -- logging -----------------------------------------------------------
    def log(self, msg: str = "") -> None:
        print(msg, flush=True)
        self.lines.append(msg)
        with self.log_path.open("a") as f:
            f.write(msg + "\n")

    # -- per-step artefacts -------------------------------------------------
    def step_payload(self, n: int, text: str) -> None:
        (self.root / f"step-{n:02d}-payload.txt").write_text(text)

    def step_history(self, n: int, history: list[str]) -> None:
        (self.root / f"step-{n:02d}-history.json").write_text(json.dumps(history, indent=2))

    def step_state(self, n: int, state: dict) -> None:
        (self.root / f"step-{n:02d}-state.json").write_text(json.dumps(state, indent=2, default=str))

    def step_answers(self, n: int, answers: dict[str, Any]) -> None:
        (self.root / f"step-{n:02d}-answers.json").write_text(json.dumps(answers, indent=2, default=str))

    def step_elements(self, n: int, page: Page, *, can_write: bool) -> None:
        (self.root / f"step-{n:02d}-elements.json").write_text(
            json.dumps(
                {
                    # Whether a writer was there to compose text, which decides the action set
                    # offered; replay needs it to offer the same one.
                    "can_write": can_write,
                    "url": page.url,
                    "title": page.title,
                    "vw": page.vw,
                    "vh": page.vh,
                    "scroll_y": page.scroll_y,
                    "can_scroll": page.can_scroll,
                    "history_len": page.history_len,
                    "element_count": len(page.items),
                    "candidates": page.candidates,
                    "below_fold": page.below_fold,
                    "items": [asdict(e) for e in page.items],
                    # The visible text the classifier also saw: replay rebuilds the same
                    # state only if the evidence blocks come back with the elements.
                    "text": [asdict(tb) for tb in page.text],
                },
                indent=2,
            )
        )

    def finish(self, payload: dict) -> None:
        (self.root / "run.json").write_text(json.dumps(payload, indent=2, default=str))

    def path(self, name: str) -> Path:
        return self.root / name


def render_payload(
    *,
    goal: str,
    page: Page,
    history: list[str],
    state: dict,
    actions: dict[str, str],
    elements: dict[str, str],
) -> str:
    """Exactly what goes to TypeSafe this step, plus the element list it was built from."""
    parts = [
        RULE,
        "STATE  (sent as `state`)",
        RULE,
        json.dumps(state, indent=2, default=str),
        "",
        RULE,
        "QUESTION kind  (Choice criteria)",
        RULE,
        json.dumps(actions, indent=2),
        "",
    ]
    if elements:
        parts += [
            RULE,
            "QUESTION element  (Choice criteria)",
            RULE,
            json.dumps(elements, indent=2),
            "",
        ]
    parts += [
        RULE,
        f"ELEMENTS  ({len(page.items)} of {page.candidates} candidates on screen; "
        f"{page.below_fold} below the fold, scroll_y={page.scroll_y})",
        RULE,
    ]
    for it in page.items:
        parts.append(f"[{it.index:3d}] {it.x:5d},{it.y:5d} {it.w:4d}x{it.h:<4d} {it.tag:8s} {it.name!r}")
    if page.text:
        parts += [
            "",
            RULE,
            f"PAGE TEXT  ({len(page.text)} visible blocks; evidence only, never click targets)",
            RULE,
        ]
        for tb in page.text:
            parts.append(f"[{tb.evidence_id:>4s}] {tb.x:5d},{tb.y:5d} {tb.w:4d}x{tb.h:<4d} {tb.text!r}")
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


def page_from_elements(data: dict) -> Page:
    """Rebuild a Page from a saved `step-NN-elements.json`."""
    items = [
        Element(
            index=int(it["index"]),
            tag=str(it["tag"]),
            role=str(it.get("role", "")),
            name=str(it["name"]),
            x=int(it["x"]),
            y=int(it["y"]),
            w=int(it["w"]),
            h=int(it["h"]),
            in_view=bool(it.get("in_view", True)),
            covered=bool(it.get("covered", False)),
            href=str(it.get("href", "")),
            field=bool(it.get("field", False)),
            secret=bool(it.get("secret", False)),
        )
        for it in data.get("items", [])
    ]
    text = [
        TextBlock(
            evidence_id=str(tb.get("evidence_id", f"t{i}")),
            text=str(tb.get("text", "")),
            x=int(tb.get("x", 0)),
            y=int(tb.get("y", 0)),
            w=int(tb.get("w", 0)),
            h=int(tb.get("h", 0)),
        )
        for i, tb in enumerate(data.get("text", []))
    ]
    return Page(
        url=str(data.get("url", "")),
        title=str(data.get("title", "")),
        vw=int(data.get("vw", 0)),
        vh=int(data.get("vh", 0)),
        items=items,
        elapsed_ms=0.0,
        raw_count=int(data.get("element_count", len(items))),
        can_scroll=bool(data.get("can_scroll", True)),
        history_len=int(data.get("history_len", 1)),
        field_count=sum(1 for e in items if e.typeable),
        scroll_y=int(data.get("scroll_y", 0)),
        candidates=int(data.get("candidates", len(items))),
        below_fold=int(data.get("below_fold", 0)),
        text=text,
    )


def load_step(run_dir: str | Path, n: int) -> dict:
    """Read one step back out of a run folder, for offline re-decision."""
    run = Path(run_dir)
    meta_path = run / "run.json"
    elements_path = run / f"step-{n:02d}-elements.json"
    if not elements_path.exists():
        raise FileNotFoundError(f"no {elements_path.name} in {run}")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    elements = json.loads(elements_path.read_text())
    payload_path = run / f"step-{n:02d}-payload.txt"
    answers_path = run / f"step-{n:02d}-answers.json"
    state_path = run / f"step-{n:02d}-state.json"
    history_path = run / f"step-{n:02d}-history.json"
    return {
        "goal": meta.get("goal", ""),
        "page": page_from_elements(elements),
        "can_write": bool(elements.get("can_write", False)),
        "payload": payload_path.read_text() if payload_path.exists() else "",
        "state": json.loads(state_path.read_text()) if state_path.exists() else {},
        "history": json.loads(history_path.read_text()) if history_path.exists() else [],
        "answers": json.loads(answers_path.read_text()) if answers_path.exists() else {},
    }


def render_answers(answers: dict[str, Any]) -> str:
    """Every probability the classifier returned, in a readable form."""
    lines = []
    for key, ans in answers.items():
        if not isinstance(ans, dict):
            lines.append(f"{key}: {ans}")
            continue
        kind = ans.get("type")
        if kind == "noul":
            lines.append(f"{key}: noul={ans.get('noul')}")
        elif kind == "choice":
            probs = sorted((ans.get("probabilities") or {}).items(), key=lambda kv: -kv[1])
            ranked = "  ".join(f"{k}={v:.3f}" for k, v in probs[:6])
            lines.append(f"{key}: choice={ans.get('choice')} confidence={ans.get('confidence'):.3f}  {ranked}")
        elif kind == "score":
            lines.append(f"{key}: score={ans.get('score')} confidence={ans.get('confidence'):.3f}")
        else:
            lines.append(f"{key}: {ans}")
    return "\n".join(lines) + "\n"
