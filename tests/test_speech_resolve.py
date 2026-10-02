"""STT provider selection (no microphone)."""

from __future__ import annotations

import pytest

from typesafe_computer_use.speech import SpeechError, resolve_stt


def test_auto_uses_macos_not_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("CLICKER_STT", raising=False)
    assert resolve_stt() == "macos"


def test_auto_uses_macos_without_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLICKER_STT", raising=False)
    assert resolve_stt() == "macos"


def test_forced_openai_requires_key(monkeypatch):
    monkeypatch.setenv("CLICKER_STT", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SpeechError, match="OPENAI_API_KEY"):
        resolve_stt()


def test_forced_macos(monkeypatch):
    monkeypatch.setenv("CLICKER_STT", "macos")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert resolve_stt() == "macos"


def test_forced_openai_when_key_present(monkeypatch):
    monkeypatch.setenv("CLICKER_STT", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert resolve_stt() == "openai"
