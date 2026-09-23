"""The two notes that say the microphone just went live, and just went quiet.

Dictation now starts and stops on the same key, and the window it belongs to is often hidden or
behind something, so the only reliable answer to "is it listening?" is one you can hear. macOS
ships these sounds, so nothing is bundled and they already sound like the machine.
"""

from __future__ import annotations

import AppKit

STARTED = "Tink"  # short and high: the microphone is live
STOPPED = "Bottle"  # lower, and clearly not the first one: it has stopped

_sounds: dict[str, object] = {}


def play(name: str) -> None:
    """Play one of the system sounds. A missing sound is silence, never an error."""
    sound = _sounds.get(name)
    if sound is None:
        sound = AppKit.NSSound.soundNamed_(name)
        if sound is None:  # pragma: no cover - these ship with macOS
            return
        _sounds[name] = sound
    _play(sound)


def _play(sound) -> None:
    """Out of the speakers. The one call here that reaches the machine."""
    sound.stop()  # so a quick off-and-on is two notes rather than one swallowed
    sound.play()


def started() -> None:
    play(STARTED)


def stopped() -> None:
    play(STOPPED)
