"""Dictation, transcribed on this machine by MLX Whisper.

Nothing is uploaded: the model runs on the Mac's own GPU through MLX, and the only network traffic
is the one-off download of the weights from Hugging Face the first time a model is named. The
import is deferred because those weights and the MLX runtime are the `voice` extra, which most
installs will not have.
"""

from __future__ import annotations

import re
from pathlib import Path

from .settings import Voice


class VoiceError(RuntimeError):
    """Speech-to-text is unavailable, or transcribed nothing."""


INSTALL_HINT = "local dictation needs the voice extra: run `uv sync --extra voice`"


def available() -> bool:
    """Whether the local speech model can be loaded at all."""
    from importlib.util import find_spec

    return find_spec("mlx_whisper") is not None


def read_wav(path: Path):
    """The 16 kHz mono WAV the recorder wrote, as the float array MLX Whisper takes.

    Reading it here rather than letting the model open the file is what keeps ffmpeg out of the
    dependency list: mlx-whisper shells out to ffmpeg for a path, but takes samples directly.

    The chunks are walked by hand rather than with the `wave` module because this is also how a
    recording still being written is read: the header of such a file says the data is zero bytes
    long, since the length is only filled in when the recorder closes it, so the data chunk is read
    to the end of the file instead of to the length it claims.
    """
    import numpy as np

    raw = path.read_bytes()
    frames, channels, width = pcm_chunk(raw)
    if width != 2:
        raise VoiceError(f"expected 16-bit audio, got {width * 8}-bit")
    usable = len(frames) - len(frames) % (2 * channels)
    samples = np.frombuffer(frames[:usable], dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples


def pcm_chunk(raw: bytes) -> tuple[bytes, int, int]:
    """The samples, channel count and sample width of a WAVE file, complete or still being written."""
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise VoiceError("that recording is not a WAVE file")
    channels, width, at = 1, 2, 12
    while at + 8 <= len(raw):
        kind, size = raw[at : at + 4], int.from_bytes(raw[at + 4 : at + 8], "little")
        body = at + 8
        if kind == b"fmt " and body + 16 <= len(raw):
            channels = int.from_bytes(raw[body + 2 : body + 4], "little") or 1
            width = int.from_bytes(raw[body + 14 : body + 16], "little") // 8 or 2
        elif kind == b"data":
            end = body + size if 0 < size <= len(raw) - body else len(raw)
            return raw[body:end], channels, width
        at = body + size + (size % 2)
        if size <= 0:
            break
    raise VoiceError("that recording holds no audio yet")


MIN_SAMPLES = 1600  # under a tenth of a second is a mis-click, not speech


def transcribe(path: Path, voice: Voice) -> str:
    """What was said, as one line. Raises VoiceError when the model is missing or heard nothing."""
    if not available():
        raise VoiceError(INSTALL_HINT)  # said before the file is opened, so the advice is the first thing
    text = transcribe_samples(read_wav(path), voice)
    if not text:
        raise VoiceError("nothing was said, or the microphone heard only silence")
    return text


def transcribe_samples(samples, voice: Voice) -> str:
    """The same, from samples already in hand. Empty when the model heard nothing worth reporting."""
    if not available():
        raise VoiceError(INSTALL_HINT)
    try:
        import mlx_whisper
    except ImportError as e:  # pragma: no cover - guarded by available()
        raise VoiceError(INSTALL_HINT) from e
    if len(samples) < MIN_SAMPLES:
        raise VoiceError("that was too short to transcribe")
    options = {"path_or_hf_repo": voice.model}
    if voice.language.strip():
        options["language"] = voice.language.strip()
    try:
        result = mlx_whisper.transcribe(samples, **options)
    except Exception as e:
        raise VoiceError(f"{voice.model} could not transcribe that ({e})") from e
    return " ".join(str(result.get("text", "")).split())


# A full stop, and not an ellipsis: Whisper writes "then..." for an utterance that is still in
# progress, and treating that as a finished sentence is what makes it settle too early.
SENTENCE_END = re.compile(r"(?<!\.)[.!?](?!\.)(?=\s|$)")
ROTATE_SECONDS = 25.0  # Whisper reads a thirty-second window; the recording is cut before that
SAMPLE_RATE = 16_000


class Stream:
    """Turns a recording that is still growing into sentences, as they settle.

    Whisper is not a streaming model: it reads a window of audio and returns the whole of it, and
    it revises what it thought earlier when more arrives. So the buffer is transcribed again every
    tick, and only the part up to the last full stop is treated as settled and handed on. The tail
    - the sentence being spoken now - is offered separately, to be shown and not acted on.

    Transcribing takes about a second whatever the buffer holds, because the model pads to its
    window either way, so the cost of a tick does not grow as the person keeps talking.
    """

    def __init__(self, voice: Voice):
        self.voice = voice
        self.settled = ""  # everything handed on so far, as the model last wrote it
        self.pending = ""  # the sentence in progress

    def update(self, samples) -> str:
        """Transcribe the buffer again. Returns the finished sentences in it, as they now read.

        The whole of it, not what is new, because the model rewrites: the first pass at a sentence
        may be half a word, and the next pass corrects it. Whoever holds the goal replaces it with
        this, so a correction reaches the goal instead of being appended to its own earlier guess.
        """
        if len(samples) < MIN_SAMPLES:
            return self.settled
        return self._settle(transcribe_samples(samples, self.voice))

    def _settle(self, text: str) -> str:
        """Split what was heard into what is finished and what is still being said."""
        ends = list(SENTENCE_END.finditer(text))
        cut = ends[-1].end() if ends else 0
        self.settled, self.pending = text[:cut].strip(), text[cut:].strip()
        return self.settled

    def flush(self, samples) -> str:
        """The end of the recording: whatever is left counts as said, full stop or not."""
        if len(samples) < MIN_SAMPLES:
            return self.settled
        self.settled, self.pending = transcribe_samples(samples, self.voice).strip(), ""
        return self.settled

    def rotate(self) -> None:
        """Start a new buffer. What was settled belongs to the goal already, not to this stream."""
        self.settled, self.pending = "", ""
