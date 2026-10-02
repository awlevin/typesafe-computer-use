"""Record from the microphone and turn speech into text."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import AVFoundation as AV
import Foundation
import objc
import openai
import Speech

from .config import stt_provider

MAX_RECORD_SECONDS = 20.0
SILENCE_SECONDS = 1.5
SILENCE_DB = -35.0  # averagePowerForChannel; below this counts as quiet
MIN_SPEECH_SECONDS = 0.5  # ignore leading hush / key-click before the user starts talking
METER_WARMUP_SECONDS = 0.4  # metering is unreliable for the first fraction of a second
# After release, live recognition is almost done; only wait briefly for isFinal.
LIVE_FINALIZE_SECONDS = 0.8
OPENAI_STT_MODELS = ("gpt-4o-mini-transcribe", "whisper-1")
DEBUG_AUDIO_DIR = Path("runs") / "last-voice"

_speech_auth: bool | None = None  # cached Speech Recognition permission
_mic_ok = False


class SpeechError(Exception):
    """Recording or transcription failed."""


def resolve_stt() -> str:
    """Which transcriber to use.

    `auto` and `macos` use Apple's Speech framework (same stack as system Dictation).
    OpenAI is only used when `CLICKER_STT=openai`.
    """
    forced = stt_provider()
    if forced == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise SpeechError("CLICKER_STT=openai but OPENAI_API_KEY is not set")
        return "openai"
    if forced in ("macos", "auto"):
        return "macos"
    raise SpeechError(f"CLICKER_STT must be auto, openai, or macos (got {forced!r})")


def warmup() -> None:
    """Ask for mic + Speech permissions up front so the first hold is not delayed."""
    _ensure_microphone()
    if resolve_stt() == "macos":
        _ensure_speech_auth()
        # Touch the recognizer once so the first live session starts colder-cache-warm.
        _recognizer()


def _ensure_microphone() -> None:
    """Prompt for Microphone access when macOS has not granted it yet."""
    global _mic_ok
    if _mic_ok:
        return
    app_cls = getattr(AV, "AVAudioApplication", None)
    if app_cls is None:
        _mic_ok = True
        return
    app = app_cls.sharedInstance() if hasattr(app_cls, "sharedInstance") else app_cls
    status = getattr(app, "recordPermission", None)
    if callable(status):
        status = status()
    denied = getattr(AV, "AVAudioApplicationRecordPermissionDenied", 1)
    granted = getattr(AV, "AVAudioApplicationRecordPermissionGranted", 2)
    if status == denied:
        raise SpeechError(
            "Microphone permission denied; enable it for this terminal in System Settings > Privacy & Security > Microphone"
        )
    if status == granted:
        _mic_ok = True
        return
    if not hasattr(app, "requestRecordPermissionWithCompletionHandler_"):
        _mic_ok = True
        return
    done = threading.Event()
    granted_box: list[bool] = []

    def handler(ok: bool) -> None:
        granted_box.append(bool(ok))
        done.set()

    app.requestRecordPermissionWithCompletionHandler_(handler)
    _spin_run_loop(done, timeout=60)
    if not granted_box or not granted_box[0]:
        raise SpeechError(
            "Microphone permission denied; enable it for this terminal in System Settings > Privacy & Security > Microphone"
        )
    _mic_ok = True


def _ensure_speech_auth() -> None:
    global _speech_auth
    if _speech_auth is True:
        return
    if _speech_auth is False:
        raise SpeechError(
            "Speech Recognition permission denied; enable it for this terminal in "
            "System Settings > Privacy & Security > Speech Recognition"
        )
    status_now = Speech.SFSpeechRecognizer.authorizationStatus()
    authorized = Speech.SFSpeechRecognizerAuthorizationStatusAuthorized
    if status_now == authorized:
        _speech_auth = True
        return
    if status_now == Speech.SFSpeechRecognizerAuthorizationStatusDenied:
        _speech_auth = False
        raise SpeechError(
            "Speech Recognition permission denied; enable it for this terminal in "
            "System Settings > Privacy & Security > Speech Recognition"
        )

    done = threading.Event()
    status_box: list[int] = []

    def auth_handler(status: int) -> None:
        status_box.append(status)
        done.set()

    Speech.SFSpeechRecognizer.requestAuthorization_(auth_handler)
    _spin_run_loop(done, timeout=15)
    if status_box and status_box[0] == authorized:
        _speech_auth = True
        return
    _speech_auth = False
    raise SpeechError(
        "Speech Recognition permission denied; enable it for this terminal in "
        "System Settings > Privacy & Security > Speech Recognition"
    )


def _recognizer():
    locale = Foundation.NSLocale.currentLocale()
    recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(locale)
    if recognizer is None or not recognizer.isAvailable():
        raise SpeechError("macOS speech recognition is unavailable for this locale")
    return recognizer


def _on_device(recognizer) -> bool:
    support = getattr(recognizer, "supportsOnDeviceRecognition", False)
    return bool(support() if callable(support) else support)


def listen_utterance(stop_event: threading.Event, max_seconds: float = MAX_RECORD_SECONDS) -> str:
    """Hold-to-speak with live Dictation. Recognizes while held; returns text on release.

    Much faster than record-then-transcribe: work happens during the hold, and release
    only waits briefly for the final segment.
    """
    _ensure_microphone()
    _ensure_speech_auth()
    recognizer = _recognizer()
    on_device = _on_device(recognizer)

    request = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
    if request is None:
        raise SpeechError("could not create a live speech request")
    request.setShouldReportPartialResults_(True)
    if hasattr(request, "setRequiresOnDeviceRecognition_") and on_device:
        request.setRequiresOnDeviceRecognition_(True)
    # Restrict to the device's English locale when possible — smaller search space.
    if hasattr(request, "setTaskHint_"):
        request.setTaskHint_(Speech.SFSpeechRecognitionTaskHintDictation)

    latest = ""
    final = ""
    err_text: str | None = None
    finished = threading.Event()
    lock = threading.Lock()

    def result_handler(result, error) -> None:
        nonlocal latest, final, err_text
        with lock:
            if error is not None:
                err_text = str(error.localizedDescription())
                finished.set()
                return
            if result is None:
                return
            text = str(result.bestTranscription().formattedString())
            latest = text
            if result.isFinal():
                final = text
                finished.set()

    task = recognizer.recognitionTaskWithRequest_resultHandler_(request, result_handler)
    engine = AV.AVAudioEngine.alloc().init()
    input_node = engine.inputNode()
    fmt = input_node.outputFormatForBus_(0)
    if fmt is None or fmt.sampleRate() == 0:
        task.cancel()
        raise SpeechError("no microphone input format; check Microphone permission")

    def tap(buffer, when) -> None:
        request.appendAudioPCMBuffer_(buffer)

    input_node.installTapOnBus_bufferSize_format_block_(0, 1024, fmt, tap)
    engine.prepare()
    started_ok, start_err = engine.startAndReturnError_(None)
    if not started_ok:
        input_node.removeTapOnBus_(0)
        task.cancel()
        detail = start_err.localizedDescription() if start_err else "unknown error"
        raise SpeechError(f"could not start the microphone ({detail})")

    started = time.monotonic()
    try:
        while not stop_event.is_set() and time.monotonic() - started < max_seconds:
            # Keep Cocoa timers alive for audio + speech callbacks on this thread.
            Foundation.NSRunLoop.currentRunLoop().runUntilDate_(Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))
    finally:
        engine.stop()
        input_node.removeTapOnBus_(0)
        request.endAudio()

    # Partials already ran during the hold; finalize should be near-instant.
    _spin_run_loop(finished, timeout=LIVE_FINALIZE_SECONDS)
    with lock:
        text = (final or latest or "").strip()
        err = err_text
    if not finished.is_set():
        task.cancel()
    if text:
        return text
    if err:
        raise SpeechError(f"macOS Dictation failed ({err})")
    raise SpeechError("macOS Dictation returned no text — hold the hotkey and speak clearly")


def record_utterance(
    stop_event: threading.Event,
    max_seconds: float = MAX_RECORD_SECONDS,
    silence_seconds: float | None = SILENCE_SECONDS,
) -> Path:
    """Record until stop_event, optional trailing silence, or max_seconds.

    Pass ``silence_seconds=None`` for hold-to-speak (stop only on release / max).
    Used by the OpenAI STT path; macOS hold-to-speak uses ``listen_utterance`` instead.
    """
    _ensure_microphone()
    fd, raw = tempfile.mkstemp(suffix=".m4a", prefix="clicker-voice-")
    os.close(fd)
    path = Path(raw)
    url = Foundation.NSURL.fileURLWithPath_(str(path))
    settings = {
        AV.AVFormatIDKey: AV.kAudioFormatMPEG4AAC,
        AV.AVSampleRateKey: 16000,
        AV.AVNumberOfChannelsKey: 1,
        AV.AVEncoderAudioQualityKey: AV.AVAudioQualityHigh,
    }
    recorder, error = AV.AVAudioRecorder.alloc().initWithURL_settings_error_(url, settings, None)
    if recorder is None:
        path.unlink(missing_ok=True)
        detail = error.localizedDescription() if error else "unknown error"
        raise SpeechError(f"could not open the microphone ({detail})")
    recorder.setMeteringEnabled_(True)
    if not recorder.prepareToRecord() or not recorder.record():
        path.unlink(missing_ok=True)
        raise SpeechError("could not start recording; grant Microphone permission to this terminal")

    started = time.monotonic()
    heard_speech_at: float | None = None
    silence_since: float | None = None
    peak = -160.0
    try:
        while time.monotonic() - started < max_seconds:
            if stop_event.is_set():
                break
            recorder.updateMeters()
            power = float(recorder.averagePowerForChannel_(0))
            peak = max(peak, power)
            now = time.monotonic()
            if silence_seconds is None:
                time.sleep(0.05)
                continue
            if now - started < METER_WARMUP_SECONDS:
                time.sleep(0.05)
                continue
            if power >= SILENCE_DB:
                if heard_speech_at is None:
                    heard_speech_at = now
                silence_since = None
            elif heard_speech_at is not None and now - heard_speech_at >= MIN_SPEECH_SECONDS:
                if silence_since is None:
                    silence_since = now
                elif now - silence_since >= silence_seconds:
                    break
            time.sleep(0.05)
    finally:
        recorder.stop()
        time.sleep(0.05)
    size = path.stat().st_size if path.exists() else 0
    if size < 1024 or peak < SILENCE_DB:
        path.unlink(missing_ok=True)
        raise SpeechError(
            "no speech captured (mic silent or permission missing). "
            "Hold the hotkey while speaking. Check System Settings > Privacy & Security > Microphone"
        )
    _keep_debug_copy(path)
    return path


def _keep_debug_copy(path: Path) -> None:
    """Save the last recording under runs/ so a bad transcription can be inspected."""
    try:
        DEBUG_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, DEBUG_AUDIO_DIR / "utterance.m4a")
    except OSError:
        pass


def transcribe(path: Path) -> str:
    """Turn a recorded file into text using the configured STT provider."""
    if resolve_stt() == "openai":
        return _transcribe_openai(path)
    return _transcribe_macos(path)


def _transcribe_openai(path: Path) -> str:
    client = openai.OpenAI()
    last_empty = False
    errors: list[str] = []
    for model in OPENAI_STT_MODELS:
        try:
            with path.open("rb") as audio:
                result = client.audio.transcriptions.create(
                    model=model,
                    file=(path.name, audio, "audio/mp4"),
                    language="en",
                )
            text = (getattr(result, "text", None) or "").strip()
            if text:
                return text
            last_empty = True
        except openai.APIError as e:
            errors.append(f"{model}: {e}")
            continue
    if last_empty:
        raise SpeechError(
            "transcription returned no text — speak while holding the hotkey, or check "
            f"Microphone permission. Last clip saved at {DEBUG_AUDIO_DIR / 'utterance.m4a'}"
        )
    raise SpeechError("OpenAI transcription failed (" + "; ".join(errors) + ")")


def _spin_run_loop(done: threading.Event, timeout: float) -> None:
    """Pump the Cocoa run loop until done is set or timeout elapses."""
    deadline = time.monotonic() + timeout
    while not done.is_set() and time.monotonic() < deadline:
        Foundation.NSRunLoop.currentRunLoop().runUntilDate_(Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))


def _transcribe_macos(path: Path) -> str:
    """File-based fallback (slower). Prefer ``listen_utterance`` for hold-to-speak."""
    _ensure_speech_auth()
    recognizer = _recognizer()
    on_device = _on_device(recognizer)
    text = _recognize_file(recognizer, path, on_device=on_device)
    if text:
        return text
    if on_device:
        text = _recognize_file(recognizer, path, on_device=False)
        if text:
            return text
    raise SpeechError(
        f"macOS Dictation returned no text — hold the hotkey and speak clearly. Last clip: {DEBUG_AUDIO_DIR / 'utterance.m4a'}"
    )


def _recognize_file(recognizer, path: Path, *, on_device: bool) -> str:
    url = Foundation.NSURL.fileURLWithPath_(str(path))
    request = Speech.SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
    if hasattr(request, "setRequiresOnDeviceRecognition_"):
        request.setRequiresOnDeviceRecognition_(on_device)
    if hasattr(request, "setShouldReportPartialResults_"):
        request.setShouldReportPartialResults_(False)

    done = threading.Event()
    holder: list[tuple[str | None, str | None]] = []

    def result_handler(result, error) -> None:
        if error is not None:
            holder.append((None, str(error.localizedDescription())))
            done.set()
            return
        if result is not None and result.isFinal():
            holder.append((str(result.bestTranscription().formattedString()), None))
            done.set()

    recognizer.recognitionTaskWithRequest_resultHandler_(request, result_handler)
    _spin_run_loop(done, timeout=15)
    if not holder:
        return ""
    text, err = holder[0]
    if err:
        return ""
    return (text or "").strip()


# Keep objc imported so the bridge stays loaded for Speech callbacks.
_ = objc
