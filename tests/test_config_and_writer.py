import os
from types import SimpleNamespace

import pytest

from typesafe_computer_use import writer
from typesafe_computer_use.config import (
    load_dotenv,
    structured_output_mode,
    writer_api_key,
    writer_base_url,
    writer_provider,
    writer_vision,
)
from typesafe_computer_use.writer import valid_url


def test_dotenv_sets_only_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKER_TEST_PRESENT", "keep")
    monkeypatch.delenv("CLICKER_TEST_NEW", raising=False)
    (tmp_path / ".env").write_text('# comment\nCLICKER_TEST_PRESENT=override\nCLICKER_TEST_NEW="quoted value"\nbroken line\n')
    load_dotenv(tmp_path / ".env")
    assert os.environ["CLICKER_TEST_PRESENT"] == "keep"
    assert os.environ["CLICKER_TEST_NEW"] == "quoted value"


def test_dotenv_missing_file_is_fine(tmp_path):
    load_dotenv(tmp_path / "nope.env")


def test_valid_url():
    assert valid_url("https://www.cnn.com")
    assert valid_url("https://news.ycombinator.com/newest")
    assert not valid_url("http://www.cnn.com")
    assert not valid_url("https://localhost")
    assert not valid_url("https://www.cnn.com/a b")
    assert not valid_url("")


def test_openai_provider_defaults_to_vision_enabled(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_PROVIDER", "openai-compatible")
    monkeypatch.delenv("CLICKER_WRITER_VISION", raising=False)

    assert writer_provider() == "openai-compatible"
    assert writer_vision() is True


def test_openai_provider_defaults_to_auto_structured_output(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_PROVIDER", "openai-compatible")
    monkeypatch.delenv("CLICKER_STRUCTURED_OUTPUT", raising=False)

    assert structured_output_mode() == "auto"


def test_invalid_boolean_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_VISION", "maybe")

    with pytest.raises(ValueError, match="CLICKER_WRITER_VISION"):
        writer_vision()


def test_invalid_structured_output_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("CLICKER_STRUCTURED_OUTPUT", "xml")

    with pytest.raises(ValueError, match="CLICKER_STRUCTURED_OUTPUT"):
        structured_output_mode()


def test_openai_provider_requires_api_key(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_PROVIDER", "openai-compatible")
    monkeypatch.delenv("CLICKER_WRITER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CLICKER_WRITER_BASE_URL", "http://127.0.0.1:8317/v1")

    assert writer_api_key() is None
    assert writer.make_writer() is None


def test_anthropic_provider_remains_supported(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_PROVIDER", "anthropic")
    fake_client = SimpleNamespace(api_key="configured", auth_token=None)
    monkeypatch.setattr(writer.anthropic, "Anthropic", lambda: fake_client)

    result = writer.make_writer()

    assert isinstance(result, writer.AnthropicWriterBackend)
