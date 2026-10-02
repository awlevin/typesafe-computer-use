"""Always-on voice front door: hotkey, speech, app commands, then the clicker.

macOS-only: Quartz hotkey tap, AVFoundation mic, and Apple Speech / Dictation.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

from . import config, macos
from .actions import Context
from .runner import RunConfig, run
from .speech import SpeechError, listen_utterance, record_utterance, resolve_stt, transcribe, warmup
from .voice import dispatch, parse_utterance
from .writer import make_writer

DOTENV = Path.cwd() / ".env"


class VoiceListener:
    """State machine driven by hold-to-speak: idle -> recording (while held) -> running."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._mode = "idle"
        self._lock = threading.Lock()
        self._stop_recording = threading.Event()
        self._worker: threading.Thread | None = None

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _fail_idle(self, log_message: str, spoken: str | None = None) -> None:
        self.log(log_message)
        macos.speak(spoken if spoken is not None else log_message)
        self._set_mode("idle")

    def on_hotkey_down(self) -> None:
        with self._lock:
            mode = self._mode
            if mode == "idle":
                self._stop_recording.clear()
                self._mode = "recording"
                start = True
            else:
                start = False
        if start:
            thread = threading.Thread(target=self._record_and_handle, name="clicker-voice", daemon=True)
            self._worker = thread
            thread.start()
        elif mode == "running":
            self.log("abort requested")
            macos.request_abort()

    def on_hotkey_up(self) -> None:
        with self._lock:
            mode = self._mode
        if mode == "recording":
            self._stop_recording.set()

    def _set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode

    def _record_and_handle(self) -> None:
        path: Path | None = None
        try:
            macos.beep()
            macos.notify("Clicker", "Listening… hold to speak")
            self.log("listening (hold hotkey)")
            started = time.perf_counter()
            if resolve_stt() == "macos":
                text = listen_utterance(self._stop_recording)
            else:
                path = record_utterance(self._stop_recording, silence_seconds=None)
                text = transcribe(path)
            self._set_mode("busy")
            self.log(f"heard: {text!r} ({time.perf_counter() - started:.2f}s)")
            macos.notify("Clicker", text)
            self._handle_utterance(text)
        except SpeechError as e:
            self._fail_idle(f"speech error: {e}", str(e))
        except Exception as e:
            self._fail_idle(f"unexpected error: {e}\n{traceback.format_exc()}", "Something went wrong")
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def _handle_utterance(self, text: str) -> None:
        command = parse_utterance(text)
        result = dispatch(command)
        if result.spoken:
            self.log(result.spoken)
            macos.speak(result.spoken)
        if not result.screen_goal:
            self._set_mode("idle")
            return
        if not os.environ.get("TYPESAFE_API_KEY"):
            self._fail_idle("Screen goals need TYPESAFE_API_KEY")
            return
        if not macos.accessibility_trusted():
            self._fail_idle("Grant Accessibility permission to drive the screen")
            return
        self._run_clicker(result.screen_goal)

    def _run_clicker(self, goal: str) -> None:
        self._set_mode("running")
        macos.clear_abort()
        out = Path("runs") / f"voice-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            writer = make_writer()
            macos.speak(f"Working on {goal}")
            self.log(f"clicker goal: {goal!r}")
            cfg = RunConfig(
                goal=goal,
                out=out,
                act=True,
                steps=config.voice_steps(),
                min_confidence=config.DEFAULT_MIN_CONFIDENCE,
                delay=config.DEFAULT_DELAY,
            )

            def ctx_factory(typesafe, history):
                return Context(
                    goal=goal,
                    browser=config.browser(),
                    email=config.email(),
                    typesafe=typesafe,
                    writer=writer,
                    history=history,
                    ask=None,
                )

            state = run(cfg, ctx_factory)
            if state.answer and state.answer.text:
                self.log(f"answer: {state.answer.text}")
                macos.speak(state.answer.text)
            elif state.outcome.startswith("aborted"):
                self.log(state.outcome)
                macos.speak("Stopped")
            else:
                summary = f"Finished with {state.outcome}"
                self.log(summary)
                macos.speak(summary)
        except ValueError as e:
            # make_writer raises when CLICKER_WRITER_API=openai lacks a base URL, etc.
            self.log(str(e))
            macos.speak(str(e))
        except Exception as e:
            self.log(f"clicker failed: {e}\n{traceback.format_exc()}")
            macos.speak("The clicker failed")
        finally:
            macos.clear_abort()
            self._set_mode("idle")
            self.log(f"idle (run folder {out})")


def listen() -> None:
    """Entry point for `clicker-listen`."""
    if sys.platform != "darwin":
        sys.exit("clicker-listen is macOS-only (hotkey tap, mic, and Apple Dictation)")
    config.load_dotenv(DOTENV)
    if not macos.accessibility_trusted():
        sys.exit(
            "this terminal lacks Accessibility permission; grant it in System Settings > Privacy & Security "
            "so the hotkey can be seen while other apps are focused"
        )
    try:
        stt = resolve_stt()
        warmup()
    except SpeechError as e:
        sys.exit(str(e))

    runs = Path("runs")
    runs.mkdir(exist_ok=True)
    log_path = runs / f"voice-{time.strftime('%Y%m%d-%H%M%S')}.log"
    listener = VoiceListener(log_path)
    hotkey = config.hotkey()
    listener.log(f"clicker-listen ready; hotkey={hotkey} stt={stt} log={log_path}")
    listener.log("no app window — keep this terminal running")
    listener.log("hold the hotkey to speak, release to send; press during a run to abort")
    listener.log("live Dictation runs while you hold — release should feel near-instant")
    try:
        macos.run_hotkey_tap(hotkey, listener.on_hotkey_down, listener.on_hotkey_up)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
