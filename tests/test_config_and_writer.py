import os

import pytest

from typesafe_computer_use.config import load_dotenv, writer_base_url
from typesafe_computer_use.writer import make_writer, parse_json, valid_url


def test_dotenv_sets_only_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKER_TEST_PRESENT", "keep")
    monkeypatch.delenv("CLICKER_TEST_NEW", raising=False)
    (tmp_path / ".env").write_text('# comment\nCLICKER_TEST_PRESENT=override\nCLICKER_TEST_NEW="quoted value"\nbroken line\n')
    load_dotenv(tmp_path / ".env")
    assert os.environ["CLICKER_TEST_PRESENT"] == "keep"
    assert os.environ["CLICKER_TEST_NEW"] == "quoted value"


def test_dotenv_missing_file_is_fine(tmp_path):
    load_dotenv(tmp_path / "nope.env")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://localhost:8081", "http://localhost:8081"),
        ("http://localhost:8081/", "http://localhost:8081"),
        ("http://localhost:8081/v1", "http://localhost:8081"),
        ("http://localhost:8081/v1/messages", "http://localhost:8081"),
        ("https://proxy.example.com/v1/messages/", "https://proxy.example.com"),
    ],
)
def test_the_writer_endpoint_accepts_any_of_its_spellings(given, expected, monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_BASE_URL", given)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert writer_base_url() == expected


def test_the_writer_endpoint_falls_back_to_the_anthropic_variable(monkeypatch):
    monkeypatch.delenv("CLICKER_WRITER_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8081/v1/messages")
    assert writer_base_url() == "http://localhost:8081"


def test_no_endpoint_variable_means_the_default_provider(monkeypatch):
    for name in ("CLICKER_WRITER_BASE_URL", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert writer_base_url() is None


def test_an_endpoint_of_its_own_needs_no_key(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_BASE_URL", "http://localhost:8081/v1/messages")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    writer = make_writer()
    assert str(writer.base_url) == "http://localhost:8081"


def test_without_credentials_or_an_endpoint_there_is_no_writer(monkeypatch):
    for name in ("CLICKER_WRITER_BASE_URL", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert make_writer() is None


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"fill": true}', {"fill": True}),
        ('```json\n{"fill": false}\n```', {"fill": False}),
        ('Sure thing:\n{"fill": true, "text": "hi"}\nHope that helps.', {"fill": True, "text": "hi"}),
    ],
)
def test_the_writer_reply_is_read_whether_or_not_it_came_plain(reply, expected):
    assert parse_json(reply) == expected


def test_a_reply_without_any_json_says_so():
    with pytest.raises(ValueError, match="without usable JSON"):
        parse_json("I am not able to help with that.")


def test_valid_url():
    assert valid_url("https://www.cnn.com")
    assert valid_url("https://news.ycombinator.com/newest")
    assert not valid_url("http://www.cnn.com")
    assert not valid_url("https://localhost")
    assert not valid_url("https://www.cnn.com/a b")
    assert not valid_url("")
