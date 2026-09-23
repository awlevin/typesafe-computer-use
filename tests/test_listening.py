"""The listener that records and transcribes as it goes, and the key that starts and stops it (macOS window only)."""

from pathlib import Path

import pytest
from test_voice import growing_wav

from typesafe_computer_use import voice
from typesafe_computer_use.settings import Shortcut, Voice

pytest.importorskip("AppKit")  # the listener lives in the macOS window

from typesafe_computer_use.gui.listening import Listener, wait_until

# --- sentences settling out of a growing recording ----------------------------------------------


def test_a_correction_replaces_what_it_corrects_rather_than_being_added_to_it(tmp_path, monkeypatch):
    """The failure this was built for: a first pass of 'then...' that the next pass rewrites."""
    passes = iter(["Open the notes app, then...", "Open the notes app, then write a summary. Save it."])
    monkeypatch.setattr(
        voice, "transcribe_samples", lambda samples, v: next(passes, "Open the notes app, then write a summary. Save it.")
    )
    monkeypatch.setattr("typesafe_computer_use.gui.listening.TICK_SECONDS", 0.05)
    said: list[str] = []
    listener = Listener(Voice(), on_text=said.append)
    listener.recorder = FakeRecorder(tmp_path)

    listener.start()
    wait_until(lambda: len(said) >= 1, seconds=3.0)
    listener.finish()

    assert said[-1] == "Open the notes app, then write a summary. Save it."
    assert not any("then... Open" in text for text in said)  # never the guess and the correction both


# --- reading a recording that is still being written --------------------------------------------


# --- the listener, with the microphone stood in for ----------------------------------------------


class FakeRecorder:
    """Writes a little more audio each time it is asked, the way a real recording grows."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path
        self._path: Path | None = None
        self.recording = False
        self.seconds = 1.0
        self.cancelled = 0

    @property
    def path(self):
        if self._path is not None:
            growing_wav(self._path, seconds=1.0)
        return self._path

    def start(self):
        self._path = self.dir / f"take-{self.cancelled}.wav"
        self.recording = True

    def stop(self):
        self.recording = False
        return self.path

    def cancel(self):
        self.cancelled += 1
        self.recording = False
        self._path = None


def test_the_goal_grows_as_sentences_finish_while_the_recording_continues(tmp_path, monkeypatch):
    said = ["Open the notes app. Then", "Open the notes app. Then write a summary. And"]
    monkeypatch.setattr(voice, "transcribe_samples", lambda samples, v: said[min(len(heard), len(said) - 1)])
    monkeypatch.setattr("typesafe_computer_use.gui.listening.TICK_SECONDS", 0.05)
    heard: list[str] = []
    listener = Listener(Voice(), on_text=heard.append)
    listener.recorder = FakeRecorder(tmp_path)

    listener.start()
    assert wait_until(lambda: len(heard) >= 2, seconds=3.0)
    listener.finish()

    assert heard[0] == "Open the notes app."
    assert heard[1] == "Open the notes app. Then write a summary."
    assert not listener.listening


def test_the_last_words_land_when_the_listening_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "transcribe_samples", lambda samples, v: "Save it and close the window")
    monkeypatch.setattr("typesafe_computer_use.gui.listening.TICK_SECONDS", 0.05)
    heard: list[str] = []
    listener = Listener(Voice(), on_text=heard.append)
    listener.recorder = FakeRecorder(tmp_path)

    listener.start()
    wait_until(lambda: False, seconds=0.3)
    listener.finish()

    assert heard[-1] == "Save it and close the window"  # no full stop, and still handed on at the end


def test_a_microphone_that_fails_reports_it_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr("typesafe_computer_use.gui.listening.TICK_SECONDS", 0.05)
    trouble: list[str] = []
    listener = Listener(Voice(), on_text=lambda t: None, on_trouble=trouble.append)
    listener.recorder = FakeRecorder(tmp_path)
    monkeypatch.setattr(voice, "transcribe_samples", lambda samples, v: (_ for _ in ()).throw(RuntimeError("no model")))

    listener.start()
    assert wait_until(lambda: bool(trouble), seconds=3.0)
    listener.finish()

    assert "no model" in trouble[0]


# --- the key held to dictate ----------------------------------------------------------------------


def test_a_chord_is_recognised_only_with_exactly_its_modifiers():
    import AppKit

    from typesafe_computer_use.gui.hotkey import matches

    class FakeEvent:
        def __init__(self, code, flags):
            self._code, self._flags = code, flags

        def keyCode(self):
            return self._code

        def modifierFlags(self):
            return self._flags

    control_option = AppKit.NSEventModifierFlagControl | AppKit.NSEventModifierFlagOption
    shortcut = Shortcut()

    assert matches(FakeEvent(2, control_option), shortcut)
    assert not matches(FakeEvent(2, AppKit.NSEventModifierFlagControl), shortcut)  # one modifier short
    assert not matches(FakeEvent(2, control_option | AppKit.NSEventModifierFlagCommand), shortcut)  # one too many
    assert not matches(FakeEvent(3, control_option), shortcut)  # the wrong key


class FakeEvent:
    """One keyboard event, as much of it as the watcher looks at."""

    def __init__(self, kind, code=2, flags=None):
        import AppKit

        self._kind, self._code = kind, code
        self._flags = AppKit.NSEventModifierFlagControl | AppKit.NSEventModifierFlagOption if flags is None else flags

    def type(self):
        return self._kind

    def keyCode(self):
        return self._code

    def modifierFlags(self):
        return self._flags


def watcher(toggles: list):
    from typesafe_computer_use.gui.hotkey import Hotkey

    watching = Hotkey(lambda: toggles.append(1))
    watching.shortcut = Shortcut()
    return watching


def test_one_press_is_one_toggle_however_often_the_key_repeats():
    import AppKit

    toggles: list = []
    watching = watcher(toggles)

    for _ in range(4):  # a held key repeats
        watching._handle(FakeEvent(AppKit.NSEventTypeKeyDown))

    assert toggles == [1]


def test_pressing_it_again_toggles_again():
    import AppKit

    toggles: list = []
    watching = watcher(toggles)

    watching._handle(FakeEvent(AppKit.NSEventTypeKeyDown))
    watching._handle(FakeEvent(AppKit.NSEventTypeKeyUp))
    watching._handle(FakeEvent(AppKit.NSEventTypeKeyDown))

    assert toggles == [1, 1]  # start, then stop


def test_letting_go_of_the_key_toggles_nothing_by_itself():
    """Dictation runs until it is stopped, so a run of speech is not cut off by a finger lifting."""
    import AppKit

    toggles: list = []
    watching = watcher(toggles)

    watching._handle(FakeEvent(AppKit.NSEventTypeKeyDown))
    watching._handle(FakeEvent(AppKit.NSEventTypeKeyUp))

    assert toggles == [1]
