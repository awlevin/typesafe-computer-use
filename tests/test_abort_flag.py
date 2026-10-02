"""Hotkey abort shares the same stop path as the top-left corner."""

from __future__ import annotations

import pytest

from typesafe_computer_use import macos
from typesafe_computer_use.models import Abort


def test_request_abort_is_seen_by_check_abort(monkeypatch):
    monkeypatch.setattr(macos, "mouse_location", lambda: (100.0, 100.0))
    macos.clear_abort()
    macos.request_abort()
    with pytest.raises(Abort, match="hotkey abort"):
        macos.check_abort()
    macos.check_abort()


def test_clear_abort_cancels_a_pending_stop(monkeypatch):
    monkeypatch.setattr(macos, "mouse_location", lambda: (100.0, 100.0))
    macos.request_abort()
    macos.clear_abort()
    macos.check_abort()
