"""Pure router: short app commands before anything reaches the clicker."""

from __future__ import annotations

import pytest

from typesafe_computer_use import macos
from typesafe_computer_use.voice import AppCommand, ScreenGoal, dispatch, parse_utterance


@pytest.mark.parametrize(
    ("text", "verb", "app"),
    [
        ("open Spotify", "open", "Spotify"),
        ("Open Spotify", "open", "Spotify"),
        ("launch Google Chrome", "open", "Google Chrome"),
        ("start Notes", "open", "Notes"),
        ("switch to Slack", "open", "Slack"),
        ("quit Spotify", "quit", "Spotify"),
        ("close Calendar", "quit", "Calendar"),
    ],
)
def test_app_commands(text, verb, app):
    assert parse_utterance(text) == AppCommand(verb=verb, app=app)


@pytest.mark.parametrize(
    "text",
    [
        "go to techcrunch and find the cheapest tickets",
        "open the Playground",
        "log in",
        "what time is it on the screen",
        "open",
        "quit",
        "",
        "   ",
    ],
)
def test_everything_else_is_a_screen_goal(text):
    result = parse_utterance(text)
    assert isinstance(result, ScreenGoal)
    assert result.goal == text.strip()


def test_open_with_no_app_name_is_a_screen_goal():
    assert parse_utterance("open the settings panel") == ScreenGoal(goal="open the settings panel")


def test_dispatch_opens_a_known_app(monkeypatch):
    monkeypatch.setattr(macos, "open_app", lambda app: app == "Spotify")
    result = dispatch(AppCommand(verb="open", app="Spotify"))
    assert result.spoken == "Opening Spotify"
    assert result.screen_goal is None


def test_dispatch_falls_through_when_open_cannot_find_the_app(monkeypatch):
    monkeypatch.setattr(macos, "open_app", lambda app: False)
    result = dispatch(AppCommand(verb="open", app="Gmail"))
    assert result.screen_goal == "open Gmail"
    assert result.spoken is None


def test_dispatch_quits_an_app(monkeypatch):
    monkeypatch.setattr(macos, "quit_app", lambda app: True)
    result = dispatch(AppCommand(verb="quit", app="Spotify"))
    assert result.spoken == "Quitting Spotify"
    assert result.screen_goal is None


def test_dispatch_reports_a_failed_quit(monkeypatch):
    monkeypatch.setattr(macos, "quit_app", lambda app: False)
    result = dispatch(AppCommand(verb="quit", app="Missing"))
    assert result.spoken == "Could not quit Missing"
    assert result.screen_goal is None


def test_dispatch_passes_a_screen_goal_through():
    result = dispatch(ScreenGoal(goal="log in"))
    assert result.screen_goal == "log in"
    assert result.spoken is None
