"""Microphone capture, through AVFoundation, straight to the format Whisper wants.

The recorder writes 16 kHz mono 16-bit WAV, which is exactly what the speech model reads, so
nothing has to be resampled or handed to ffmpeg afterwards. Everything here is macOS API calls
and no model, so it stays usable whether or not the `voice` extra is installed.

The microphone is part of the window, not of the machine the loop drives, so it lives here rather
than in an adapter. The two calls that reach it, `request_permission` and `_open_recorder`, are the
ones the test guard refuses.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

SAMPLE_RATE = 16_000  # Whisper's own rate: recording at it avoids a resample later
MAX_SECONDS = 180.0  # a dictated goal is a sentence; this is only a guard against a forgotten recorder

# 'lpcm' as a four-character code. CoreAudio spells it as a constant, which is not always installed.
LINEAR_PCM = 0x6C70636D


class AudioError(RuntimeError):
    """The microphone is unavailable, refused, or produced nothing."""


def _av():
    try:
        import AVFoundation
    except ImportError as e:  # pragma: no cover - the framework ships with the app's own dependencies
        raise AudioError("AVFoundation is not available; reinstall with `uv sync`") from e
    return AVFoundation


def permission() -> str:
    """ "granted", "denied", or "unknown" - what macOS currently says about the microphone.

    A denied app still records: it just records silence, which would come back as an empty or
    nonsense transcript. Asking first is how the GUI can say what is actually wrong.
    """
    AV = _av()
    status = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)
    return {3: "granted", 2: "denied", 1: "denied"}.get(status, "unknown")


def request_permission() -> None:
    """Ask for the microphone, if macOS has not decided yet. Returns as soon as the prompt is up."""
    AV = _av()
    if AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio) == 0:
        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeAudio, lambda granted: None)


def _open_recorder(url, settings: dict):
    """Start recording to `url`: the microphone goes live here."""
    AV = _av()
    recorder, error = AV.AVAudioRecorder.alloc().initWithURL_settings_error_(url, settings, None)
    if recorder is None:
        raise AudioError(f"the microphone could not be opened ({error})")
    recorder.setMeteringEnabled_(True)
    if not recorder.recordForDuration_(MAX_SECONDS):
        raise AudioError("the microphone refused to start; check Privacy & Security > Microphone")
    return recorder


class Recorder:
    """One dictation. Start it, stop it, and it hands back a WAV file on disk."""

    def __init__(self) -> None:
        self._recorder = None
        self._path: Path | None = None
        self._started = 0.0

    @property
    def recording(self) -> bool:
        return self._recorder is not None

    @property
    def path(self) -> Path | None:
        """The file being written, so a recording can be read while it is still being made."""
        return self._path

    @property
    def seconds(self) -> float:
        return time.monotonic() - self._started if self.recording else 0.0

    def start(self) -> None:
        AV = _av()
        from Foundation import NSURL

        if self.recording:
            return
        request_permission()
        self._path = Path(tempfile.mkdtemp(prefix="clicker-voice-")) / "dictation.wav"
        settings = {
            AV.AVFormatIDKey: LINEAR_PCM,
            AV.AVSampleRateKey: float(SAMPLE_RATE),
            AV.AVNumberOfChannelsKey: 1,
            AV.AVLinearPCMBitDepthKey: 16,
            AV.AVLinearPCMIsFloatKey: False,
            AV.AVLinearPCMIsBigEndianKey: False,
        }
        self._recorder = _open_recorder(NSURL.fileURLWithPath_(str(self._path)), settings)
        self._started = time.monotonic()

    def level(self) -> float:
        """Loudness right now, 0 to 1, for a meter. Silence and a muted input both read 0."""
        if self._recorder is None:
            return 0.0
        self._recorder.updateMeters()
        decibels = self._recorder.averagePowerForChannel_(0)  # -160 (silent) to 0 (peak)
        return max(0.0, min(1.0, (decibels + 50.0) / 50.0))

    def stop(self) -> Path:
        """Stop and return the recording. Raises AudioError when nothing was captured."""
        if self._recorder is None or self._path is None:
            raise AudioError("nothing is being recorded")
        self._recorder.stop()
        self._recorder = None
        path, self._path = self._path, None
        if not path.exists() or path.stat().st_size < 1024:
            raise AudioError("the recording is empty; check Privacy & Security > Microphone")
        return path

    def cancel(self) -> None:
        """Stop and throw the recording away."""
        if self._recorder is not None:
            self._recorder.stop()
            self._recorder = None
        if self._path is not None and self._path.exists():
            self._path.unlink()
        self._path = None
