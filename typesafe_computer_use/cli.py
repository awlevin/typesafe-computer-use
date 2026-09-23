"""Command-line entry points: `clicker` and `clicker-inspect`."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import config, session
from .perception import capture, perceive
from .platform_adapter import desktop
from .report import annotate, ax_count, render_payload
from .runner import RunConfig, run
from .settings import DECISION_PROVIDERS, TEXT_PROVIDERS, Settings
from .settings import load as load_settings
from .timing import format_timing

DOTENV = Path.cwd() / ".env"


def _provider_flags(parser: argparse.ArgumentParser) -> None:
    """Point one run at other providers without touching the saved settings."""
    parser.add_argument("--classifier", choices=sorted(DECISION_PROVIDERS), help="who answers each step's decision")
    parser.add_argument("--classifier-model", help="model for the classifier")
    parser.add_argument("--writer", choices=sorted(TEXT_PROVIDERS), help="who writes field text and URLs")
    parser.add_argument("--writer-model", help="model for the writer")
    parser.add_argument("--answer", choices=sorted(TEXT_PROVIDERS), help="who reads the screen when the classifier stops")
    parser.add_argument("--answer-model", help="model for the answer")


def apply_provider_flags(settings: Settings, args: argparse.Namespace) -> Settings:
    """The saved settings with this run's flags on top. A new provider starts from its own defaults."""
    for section, flag in (("decisions", "classifier"), ("writer", "writer"), ("answer", "answer")):
        endpoint = getattr(settings, section)
        if getattr(args, flag, None):
            endpoint.provider, endpoint.model, endpoint.base_url = getattr(args, flag), "", ""
        if getattr(args, f"{flag}_model", None):
            endpoint.model = getattr(args, f"{flag}_model")
    return settings


def ask_user(question: str) -> str:
    """Read the user's reply to a question the run has just printed. An empty reply declines to answer.

    The bell is for a user who is watching the browser, not this terminal.
    """
    try:
        return input("\a  > ")
    except EOFError:
        return ""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="clicker",
        description="Drive this computer toward a goal: screen OCR, a TypeSafe classifier, deterministic actions.",
    )
    parser.add_argument("goal", help="what you want done on this computer")
    parser.add_argument("--act", action="store_true", help="actually click and type (default: dry run, one step)")
    parser.add_argument("--steps", type=int, default=config.DEFAULT_STEPS, help="max actions before stopping")
    parser.add_argument("--min-confidence", type=float, default=config.DEFAULT_MIN_CONFIDENCE, help="stop below this confidence")
    parser.add_argument("--delay", type=float, default=config.DEFAULT_DELAY, help="seconds to wait after each action")
    parser.add_argument(
        "--handoffs",
        type=int,
        default=config.DEFAULT_HANDOFFS,
        help="times the writer may send a stopped run back to the classifier with a new focus (0: every stop is final)",
    )
    parser.add_argument("--out", type=Path, default=Path("runs") / time.strftime("%Y%m%d-%H%M%S"), help="run folder")
    parser.add_argument("--image", type=Path, help="replay a saved capture instead of the live screen (never acts)")
    parser.add_argument("--app", help="frontmost app to report during replay")
    parser.add_argument("--url", help="browser URL to report during replay")
    parser.add_argument(
        "--share-clipboard",
        action="store_true",
        help="show the classifier the clipboard's text each step, to carry text between apps (off by default: it may hold a password)",
    )
    _provider_flags(parser)
    args = parser.parse_args(argv)

    config.load_dotenv(DOTENV)
    settings = apply_provider_flags(load_settings(), args)
    if args.act and not desktop.accessibility_trusted():
        sys.exit("this terminal lacks Accessibility permission; grant it in System Settings > Privacy & Security")
    try:
        services = session.build(settings)
        config.writer_vision()  # a bad value stops the run here, not at its first stop
    except ValueError as e:
        sys.exit(str(e))
    for line in services.describe():
        print(line)

    cfg = RunConfig(
        goal=args.goal,
        out=args.out,
        act=args.act,
        steps=args.steps,
        min_confidence=args.min_confidence,
        delay=args.delay,
        handoffs=args.handoffs,
        image=args.image,
        app=args.app,
        url=args.url,
        clipboard=args.share_clipboard or settings.share_clipboard,
    )

    ctx_factory = session.context_factory(settings, args.goal, services, ask_user if sys.stdin.isatty() else None)
    state = run(cfg, ctx_factory, services.classifier)
    if state.outcome.startswith("aborted"):
        sys.exit(130)


def inspect(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="clicker-inspect",
        description="Count down, capture the screen, and show exactly what the clicker would send to TypeSafe.",
    )
    parser.add_argument("goal", nargs="?", default="(no goal given)")
    parser.add_argument("--countdown", type=int, default=3)
    parser.add_argument("--no-open", action="store_true", help="write files without opening them")
    parser.add_argument("--out", type=Path, default=Path("inspections") / time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args(argv)
    config.load_dotenv(DOTENV)
    settings = load_settings()
    args.out.mkdir(parents=True, exist_ok=True)

    for n in range(args.countdown, 0, -1):
        print(f"{n}...", end=" ", flush=True)
        time.sleep(1)
    print("capture")

    browser = settings.resolved_browser()
    timing: dict[str, float] = {}
    screen = capture(browser=browser, timing=timing)
    items = perceive(screen, config.MAX_OPTIONS, args.goal, timing)
    annotated = args.out / "annotated.png"
    text = args.out / "state.txt"
    screen.image.save(args.out / "raw.png")
    annotate(screen, items, chosen="", out=annotated)
    text.write_text(
        render_payload(args.goal, screen, items, [], browser, settings.resolved_email(), apps=desktop.installed_apps()),
        encoding="utf-8",
    )

    print(
        f"app={screen.app!r} url={screen.url!r} items={len(items)} ax={ax_count(items)} "
        f"offscreen={len(screen.offscreen)} field={screen.field.role if screen.field else None}"
    )
    print(format_timing(timing))
    print(f"  {annotated}\n  {text}")
    if not args.no_open:
        desktop.open_path(annotated)
        desktop.open_path(text, as_text=True)
