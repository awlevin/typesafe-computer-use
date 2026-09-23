"""A goal that can still be arriving while the loop is already acting on it.

Ordinarily a run is given a sentence and then left alone with it. Listen-and-go starts the loop on
the first sentence and lets the rest land as they are spoken, which means two things have to be
true at once: whatever reads the goal must see the latest version, and the loop must not conclude
that a half-spoken goal is finished. The flag is what says the difference.
"""

from __future__ import annotations

import threading


class LiveGoal:
    """The goal as it stands, plus whether more of it is still coming.

    Shared between the thread doing the listening and the thread doing the driving, so every read
    and write is behind the same lock.
    """

    def __init__(self, text: str = "", listening: bool = False):
        self._lock = threading.Lock()
        self._text = text.strip()
        self._listening = listening

    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    @property
    def listening(self) -> bool:
        """True while the user may still be talking. The loop holds instead of stopping."""
        with self._lock:
            return self._listening

    def add(self, said: str) -> str:
        """Append what was just said. Returns the goal as it now reads."""
        said = " ".join(said.split())
        if not said:
            return self.text
        with self._lock:
            self._text = f"{self._text} {said}".strip()
            return self._text

    def set(self, text: str) -> None:
        with self._lock:
            self._text = text.strip()

    def listen(self) -> None:
        with self._lock:
            self._listening = True

    def finish(self) -> None:
        """The user has stopped talking: the goal is whatever it says now."""
        with self._lock:
            self._listening = False
