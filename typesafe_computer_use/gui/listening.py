"""Dictation as it happens: a recorder, a rolling transcription, and sentences handed on.

Two modes share this. Push-to-talk records while a key is held and reports once, at the end.
Listen-and-go keeps recording and reports each sentence as it settles, so the loop can start on
the first one while the rest is still being spoken.

The work happens on a thread of its own: transcribing takes about a second, and the window has to
keep drawing and the run has to keep running while it does.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .. import voice
from ..settings import Voice
from ..voice import ROTATE_SECONDS, SAMPLE_RATE
from . import audio

TICK_SECONDS = 1.5  # transcribing costs about a second, whatever the buffer holds


class Listener:
    """Records, transcribes as it goes, and calls back with text as it settles.

    `on_text` receives everything dictated so far, rewritten each time the model corrects itself;
    whoever holds the goal replaces it with that rather than appending. `on_pending` is the words
    still being spoken, for showing and not for acting on. `on_trouble` is anything that went
    wrong, in words for a person.
    """

    def __init__(
        self,
        settings_voice: Voice,
        on_text: Callable[[str], None],
        on_pending: Callable[[str], None] = lambda text: None,
        on_trouble: Callable[[str], None] = lambda message: None,
        on_utterance: Callable[[str], None] | None = None,
    ):
        self.voice = settings_voice
        self.on_text = on_text
        self.on_pending = on_pending
        self.on_trouble = on_trouble
        self.on_utterance = on_utterance or on_text  # push-to-talk reports once, at the end
        self.recorder = audio.Recorder()
        self.stream = voice.Stream(settings_voice)
        self.committed = ""  # what earlier buffers held, before the recording was cut and restarted
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def listening(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- listen and go ---------------------------------------------------------------------

    def start(self) -> None:
        """Begin recording and transcribing until `finish` is called."""
        if self.listening:
            return
        self._stop.clear()
        self.committed = ""
        self.stream.rotate()
        self.recorder.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def finish(self) -> None:
        """Stop, and hand on whatever was still being said."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=8.0)
            self._thread = None

    def _run(self) -> None:
        """Tick until stopped: read what has been recorded so far and see what has settled."""
        try:
            while not self._stop.is_set():
                self._stop.wait(TICK_SECONDS)
                samples = self._samples()
                if samples is None:
                    continue
                if self._stop.is_set():
                    break
                if len(samples) / SAMPLE_RATE >= ROTATE_SECONDS:
                    self._rotate(samples)
                    continue
                self._report(self.stream.update(samples))
                self.on_pending(self.stream.pending)
            self._finish_recording()
        except Exception as e:  # a dictation that fails must not take the window with it
            self.on_trouble(str(e))
            self.recorder.cancel()

    def _rotate(self, samples) -> None:
        """Whisper reads a thirty-second window, so the recording is cut and restarted before that.

        What the finished buffer held is committed word for word: nothing that comes after can
        revise it, because the model will never see that audio again.
        """
        self.committed = f"{self.committed} {self.stream.flush(samples)}".strip()
        self._report("")
        self.recorder.cancel()
        self.stream.rotate()
        self.recorder.start()

    def _finish_recording(self) -> None:
        samples = self._samples()
        self.recorder.cancel()
        if samples is not None:
            self._report(self.stream.flush(samples))
        self.on_pending("")

    def _samples(self):
        """What has been recorded so far. None when there is nothing readable yet."""
        path = self.recorder.path
        if path is None or not path.exists():
            return None
        try:
            return voice.read_wav(path)
        except voice.VoiceError:
            return None  # the header is not written yet; the next tick will find it

    def _report(self, settled: str) -> None:
        """Hand on the whole dictation as it now reads: earlier buffers, then this one."""
        text = f"{self.committed} {settled}".strip()
        if text:
            self.on_text(text)

    # --- push to talk ----------------------------------------------------------------------

    def hold(self) -> bool:
        """Start recording for as long as a key is held. False when the microphone refused."""
        try:
            self.recorder.start()
            return True
        except audio.AudioError as e:
            self.on_trouble(str(e))
            return False

    def release(self) -> None:
        """Stop and transcribe once, on a thread, reporting the whole of what was said."""
        if not self.recorder.recording:
            return
        started = self.recorder.seconds
        try:
            path = self.recorder.stop()
        except audio.AudioError as e:
            self.on_trouble(str(e))
            return

        def work() -> None:
            try:
                said = voice.transcribe(path, self.voice)
            except voice.VoiceError as e:
                self.on_trouble(str(e))
                return
            self.on_utterance(said)

        if started < 0.3:  # a tap rather than a hold
            self.on_trouble("hold the key down while you speak")
            return
        threading.Thread(target=work, daemon=True).start()


def wait_until(predicate: Callable[[], bool], seconds: float, step: float = 0.05) -> bool:
    """Poll until true or out of time. Used by tests, where threads finish on their own schedule."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
