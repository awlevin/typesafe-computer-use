"""Dictation turned into a goal: sentences settling out of a recording that is still growing.

Pure logic, runnable anywhere. The recorder and the key that starts it are in test_listening.py.
"""

import struct
import wave
from pathlib import Path

import pytest

from typesafe_computer_use import voice
from typesafe_computer_use.settings import Shortcut, Voice
from typesafe_computer_use.voice import Stream


def test_the_finished_sentences_are_handed_on_whole_each_time():
    """Whole, not what is new: the model rewrites, and a correction has to replace its own guess."""
    stream = Stream(Voice())

    assert stream._settle("Open the notes app. Then write") == "Open the notes app."
    assert stream.pending == "Then write"
    assert stream._settle("Open the notes app. Then write a summary.") == "Open the notes app. Then write a summary."
    assert stream._settle("Open the notes app. Then write a summary. And") == "Open the notes app. Then write a summary."


def test_an_unfinished_utterance_is_not_mistaken_for_a_finished_sentence():
    """Whisper writes an ellipsis when someone is still talking; it is not a full stop."""
    stream = Stream(Voice())

    assert stream._settle("Open the notes app, then...") == ""
    assert stream.pending == "Open the notes app, then..."
    assert stream._settle("Open the notes app, then write a summary.") == "Open the notes app, then write a summary."


def test_the_tail_counts_as_said_when_the_recording_ends(monkeypatch):
    stream = Stream(Voice())
    monkeypatch.setattr(voice, "transcribe_samples", lambda samples, v: "Open the notes app. And save it")
    stream._settle("Open the notes app. And save it")

    assert stream.flush([0] * 16000) == "Open the notes app. And save it"
    assert stream.pending == ""


def test_a_rotated_buffer_starts_again_without_repeating_itself():
    stream = Stream(Voice())
    stream._settle("The first half is done.")

    stream.rotate()

    assert stream.settled == "" and stream._settle("The second half is done.") == "The second half is done."


def growing_wav(path: Path, seconds: float, header_claims_zero: bool = True) -> None:
    """A WAV as AVAudioRecorder leaves it mid-recording: samples present, length not yet filled in."""
    frames = int(16_000 * seconds)
    body = b"\x00\x40" * frames
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(body)
    if header_claims_zero:
        raw = bytearray(path.read_bytes())
        at = raw.find(b"data")
        raw[at + 4 : at + 8] = struct.pack("<I", 0)
        path.write_bytes(bytes(raw))


def test_a_recording_still_being_written_reads_as_far_as_it_goes(tmp_path):
    path = tmp_path / "dictation.wav"
    growing_wav(path, seconds=1.5)

    samples = voice.read_wav(path)

    assert len(samples) == 24_000  # a second and a half, despite the header saying zero


def test_a_finished_recording_reads_the_same_way(tmp_path):
    path = tmp_path / "done.wav"
    growing_wav(path, seconds=1.0, header_claims_zero=False)

    assert len(voice.read_wav(path)) == 16_000


def test_a_file_that_is_not_a_recording_says_so(tmp_path):
    path = tmp_path / "nonsense.wav"
    path.write_bytes(b"not audio at all")

    with pytest.raises(voice.VoiceError, match="not a WAVE file"):
        voice.read_wav(path)


def test_the_default_chord_is_not_one_the_mac_already_uses():
    shortcut = Shortcut()

    assert shortcut.describe() == "⌃⌥D"
    assert shortcut.control and shortcut.option and not shortcut.command
    assert (shortcut.key_code, shortcut.character) != (49, "space")  # never Command-Space or Option-Space


def test_the_recorded_wav_is_read_as_the_float_samples_whisper_takes(tmp_path):
    path = tmp_path / "take.wav"
    growing_wav(path, seconds=0.5, header_claims_zero=False)

    samples = voice.read_wav(path)

    assert len(samples) == 8_000 and abs(float(samples[0]) - 0.5) < 1e-6  # 0x4000 of 0x8000


def test_dictation_says_how_to_install_itself_when_it_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "available", lambda: False)

    with pytest.raises(voice.VoiceError, match="uv sync --extra voice"):
        voice.transcribe(tmp_path / "nothing.wav", Voice())
