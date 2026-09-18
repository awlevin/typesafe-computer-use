"""Benchmark: browser-only computer use, DOM perception vs the original OCR path.

    uv run python -m typesafe_computer_use.browser.bench perception --url https://news.ycombinator.com
    uv run python -m typesafe_computer_use.browser.bench perception --fixture --n 8
    uv run python -m typesafe_computer_use.browser.bench loop --fixture

`perception` is the headline: the same page, the same machine, the same moment.
One path reads the DOM over CDP. The other does what the original does —
`Page.captureScreenshot` -> Apple Vision OCR -> merge blocks -> reading order.
The TypeSafe decision that follows is identical in both, so the difference is
purely perception.

`loop` runs the full end-to-end step loop and reports per-stage latency.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import statistics
import sys
import time
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from . import act
from .cdp import Chrome
from .decide import decide
from .perceive import perceive
from .report import RunFolder
from .runner import extract_text, run_goal, save

FIXTURE = Path(__file__).resolve().parents[2] / "bench" / "fixture.html"

TASK = (
    "Search for 'invoice automation' in the search box and submit the search. "
    "Then open the guide whose title mentions automation."
)


def _pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))], 1)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())[:60]


# --------------------------------------------------------------------------- #
def ocr_perception(session, *, budget: int = 120) -> dict:
    """The original pipeline, isolated and timed: screenshot -> Vision OCR -> blocks."""
    from ocrmac import ocrmac
    from PIL import Image

    t0 = time.perf_counter()
    shot = session.call("Page.captureScreenshot", {"format": "png"})
    capture_ms = (time.perf_counter() - t0) * 1000
    image = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")

    t0 = time.perf_counter()
    raw = ocrmac.OCR(image, recognition_level="accurate").recognize(px=True)
    ocr_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    from ..perception import merge_blocks, to_items

    lines = [(t.strip(), c, b) for t, c, b in raw if t.strip() and c >= 0.0]
    items = to_items(merge_blocks(lines), budget)
    post_ms = (time.perf_counter() - t0) * 1000

    return {
        "capture_ms": capture_ms,
        "ocr_ms": ocr_ms,
        "post_ms": post_ms,
        "total_ms": capture_ms + ocr_ms + post_ms,
        "lines": len(raw),
        "items": len(items),
        "names": [it.text for it in items],
    }


def benchmark_perception(args: argparse.Namespace) -> int:
    url = FIXTURE.as_uri() if args.fixture else args.url
    with Chrome(headed=args.headed) as chrome:
        session = chrome.attach()
        act.navigate(session, url)
        act.wait_for_load(session)

        dom, ocr = [], []
        dom_names, ocr_names = [], []
        for i in range(args.n):
            p = perceive(session)
            dom.append(p.elapsed_ms)
            if i == 0:
                dom_names = [it.name for it in p.items]
            if args.with_ocr:
                r = ocr_perception(session)
                ocr.append(r)
                if i == 0:
                    ocr_names = r["names"]
            print(
                f"  pass {i + 1}/{args.n}: dom={p.elapsed_ms:7.1f}ms "
                f"({len(p.items)} elements)"
                + (f"  ocr={ocr[-1]['total_ms']:7.1f}ms ({ocr[-1]['items']} blocks)" if args.with_ocr else ""),
                flush=True,
            )

        dom_p50 = statistics.median(dom)
        out: dict = {
            "url": url,
            "dom": {
                "p50_ms": round(dom_p50, 1),
                "p95_ms": _pct(dom, 95),
                "min_ms": round(min(dom), 1),
                "items": len(dom_names),
            },
            "title": session.evaluate("document.title"),
            "dom_elapsed_for_decision": round(dom_p50, 1),
        }

        print(
            f"\nDOM perception:   p50 {out['dom']['p50_ms']:>7.1f}ms  p95 {out['dom']['p95_ms']:>7.1f}ms  "
            f"({out['dom']['items']} elements)"
        )

        if args.with_ocr and ocr:
            totals = [r["total_ms"] for r in ocr]
            out["ocr"] = {
                "p50_ms": round(statistics.median(totals), 1),
                "p95_ms": _pct(totals, 95),
                "min_ms": round(min(totals), 1),
                "capture_ms": round(statistics.median([r["capture_ms"] for r in ocr]), 1),
                "vision_ocr_ms": round(statistics.median([r["ocr_ms"] for r in ocr]), 1),
                "merge_ms": round(statistics.median([r["post_ms"] for r in ocr]), 1),
                "items": ocr[0]["items"],
                "raw_lines": ocr[0]["lines"],
            }
            o = out["ocr"]
            print(f"OCR perception:   p50 {o['p50_ms']:>7.1f}ms  p95 {o['p95_ms']:>7.1f}ms  ({o['items']} blocks)")
            print(
                f"                  = capture {o['capture_ms']}ms + Vision OCR {o['vision_ocr_ms']}ms + merge {o['merge_ms']}ms"
            )
            speedup = o["p50_ms"] / out["dom"]["p50_ms"] if out["dom"]["p50_ms"] else 0
            out["speedup"] = round(speedup, 1)
            print(f"                  DOM is {speedup:.1f}x faster")

            # Accuracy: does each path even surface the text the task needs?
            probe = args.probe
            dom_hit = any(probe.lower() in _norm(n) for n in dom_names)
            ocr_joined = " | ".join(_norm(n) for n in ocr_names)
            ocr_hit = probe.lower() in ocr_joined
            out["probe"] = {"text": probe, "dom_found": dom_hit, "ocr_found": ocr_hit}
            print(f"\nprobe {probe!r}: DOM found={dom_hit}   OCR found={ocr_hit}")

            dom_text = {_norm(n) for n in dom_names}
            ocr_text = _norm(ocr_joined)
            covered = sum(1 for n in dom_text if n and n in ocr_text)
            out["recall"] = {
                "dom_elements_found_verbatim_by_ocr": covered,
                "dom_elements": len(dom_text),
                "fraction": round(covered / max(1, len(dom_text)), 3),
            }
            print(f"OCR reproduces {covered}/{len(dom_text)} DOM labels verbatim ({out['recall']['fraction'] * 100:.0f}%)")

        session.close()
    print("\n" + json.dumps(out, indent=2, default=str))
    return 0


def benchmark_loop(args: argparse.Namespace) -> int:
    url = FIXTURE.as_uri() if args.fixture else args.url
    goal = args.goal or TASK

    writer = None
    if args.writer:
        from ..writer import make_writer

        writer = make_writer()
        if writer is None:
            print("no Anthropic credentials — free text falls back to the regex extractor", file=sys.stderr)

    runfolder = RunFolder.create(args.runs) if args.runs else None
    if runfolder is not None:
        print(f"run folder: {runfolder.root}")

    with Chrome(headed=args.headed) as chrome:
        session = chrome.attach()
        client = TypeSafeClient()
        print(f"goal: {goal}\nurl:  {url}\n")
        print(f"{'#':>3}  {'action':<12} {'detail':<40} {'conf':<9} {'perceive':>8}  {'decide':>8}  {'act':>8}  {'total':>9}")
        print("-" * 118)
        result = run_goal(
            session,
            client,
            goal,
            start_url=url,
            max_steps=args.steps,
            min_confidence=args.min_confidence,
            model=args.model,
            writer=writer,
            runfolder=runfolder,
        )
        session.close()

    s = result.summary()
    print("-" * 118)
    print(f"outcome: {result.outcome}   steps: {s.get('steps')}   wall: {result.wall_ms:.0f}ms")
    if s.get("steps"):
        print(
            f"per-step  perceive p50 {s['perceive_ms']['p50']}ms   decide p50 {s['decide_ms']['p50']}ms   "
            f"act p50 {s['act_ms']['p50']}ms   TOTAL p50 {s['total_ms']['p50']}ms"
        )
        print(
            f"          -> {s['steps_per_sec_p50']} steps/sec (p50)   p95 {s['total_ms']['p95']}ms   min {s['total_ms']['min']}ms"
        )
        print(f"          -> {s['steps_per_sec_excluding_cold_start']} steps/sec excluding the cold-start step")
    if args.out:
        save(result, Path(args.out))
        print(f"saved: {args.out}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-decide a saved step without touching a browser.

    This is the run-folder contract: same input, and you can see whether the
    decision changed. It also proves the replay is faithful by rebuilding the
    state from the saved elements and comparing it to what was originally sent.
    """
    from .report import load_step, render_answers

    step = load_step(args.run, args.step)
    page = step["page"]
    print(f"run:  {args.run}")
    print(f"step: {args.step}   goal: {step['goal']!r}")
    print(f"page: {page.title!r}  {page.url}")
    print(f"      {len(page.items)} elements, scroll_y={page.scroll_y}, {page.below_fold} below the fold")
    print(f"      history: {len(step['history'])} prior action(s)\n")

    with TypeSafeClient() as client:
        # History is part of the state that was sent, so it has to come back
        # too or the reconstruction will not match.
        # Mirror the runner exactly: history and the goal-derived text are both
        # part of the state that was sent, so both have to be restored.
        decision = decide(
            client,
            step["goal"],
            page,
            history=step["history"],
            text=extract_text(step["goal"]),
            model=args.model,
        )

    if step["state"] and decision.state != step["state"]:
        print("WARNING: reconstructed state differs from the saved one — replay is NOT faithful\n")
    else:
        print("replay is faithful: reconstructed state == saved state\n")

    print("SAVED ANSWERS")
    print(render_answers(step["answers"]))
    print("REPLAYED NOW")
    print(render_answers(decision.answers))
    saved_kind = (step["answers"].get("kind") or {}).get("choice")
    verdict = "identical" if saved_kind == decision.kind.choice else "CHANGED"
    print(f"decision: saved={saved_kind}  now={decision.kind.choice}  {verdict}")
    return 0


def _ensure_key() -> None:
    """The SDK reads TYPESAFE_API_KEY. Fall back to the macOS Keychain so this
    repo runs on a machine that stores the key properly instead of in a dotfile."""
    import os
    import subprocess

    if os.environ.get("TYPESAFE_API_KEY"):
        return
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "openclaw/typesafe-ai/api-key", "-a", "typesafe", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            os.environ["TYPESAFE_API_KEY"] = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="clicker-bench",
        description=(
            "Browser-only computer-use benchmarks: DOM perception vs the OCR path, the "
            "end-to-end step loop, and offline replay of a saved step."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  clicker-bench perception --url https://news.ycombinator.com\n"
            "  clicker-bench perception --fixture --n 8\n"
            "  clicker-bench loop --fixture --runs bench/example-run\n"
            "  clicker-bench replay --run bench/example-run/<ts> --step 2"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("perception", help="DOM vs OCR perception, same page")
    p.add_argument("--url", default="https://news.ycombinator.com")
    p.add_argument("--fixture", action="store_true", help="use the local deterministic fixture page")
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--with-ocr", action="store_true", default=True)
    p.add_argument("--no-ocr", dest="with_ocr", action="store_false")
    p.add_argument("--probe", default="Search", help="text the task needs; checked in both paths")
    p.add_argument("--headed", action="store_true")
    p.set_defaults(func=benchmark_perception)

    q = sub.add_parser("loop", help="end-to-end step loop")
    q.add_argument("--url", default="https://en.wikipedia.org/wiki/Singapore")
    q.add_argument("--fixture", action="store_true")
    q.add_argument("--goal", default=None)
    q.add_argument("--steps", type=int, default=10)
    q.add_argument("--min-confidence", type=float, default=0.4)
    q.add_argument("--model", default=None)
    q.add_argument("--headed", action="store_true")
    q.add_argument("--out", default=None)
    q.add_argument("--writer", action="store_true", help="compose typed text with the writer model")
    q.add_argument("--runs", default=None, help="write a replayable run folder under this directory")
    q.set_defaults(func=benchmark_loop)

    r = sub.add_parser("replay", help="re-decide a saved step offline, from a run folder")
    r.add_argument("--run", required=True, help="runs/<timestamp> directory")
    r.add_argument("--step", type=int, required=True)
    r.add_argument("--model", default=None)
    r.set_defaults(func=cmd_replay)

    args = ap.parse_args(argv)
    _ensure_key()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
