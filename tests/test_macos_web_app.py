"""Frontmost Safari web apps share a process name but not a displayed name."""

import subprocess

from typesafe_computer_use import macos


def test_regular_app_keeps_process_name_and_pid(monkeypatch):
    scripts = []

    def fake_script(script):
        scripts.append(script)
        return "Finder, 42" if "{name, unix id}" in script else "Finder"

    monkeypatch.setattr(macos, "osascript", fake_script)
    assert macos.frontmost_app() == "Finder"
    assert macos.frontmost_app_and_pid() == ("Finder", 42)
    assert len(scripts) == 2
    assert all("displayed name" not in script for script in scripts)


def test_web_app_reports_display_name_and_activation_succeeds(monkeypatch):
    scripts = []

    def fake_script(script):
        scripts.append(script)
        if "displayed name" in script:
            return "YouTube"
        if "{name, unix id}" in script:
            return "Web App, 84"
        if "get name of first" in script:
            return "Web App"
        return ""

    monkeypatch.setattr(macos, "osascript", fake_script)
    monkeypatch.setattr(macos, "check_abort", lambda: None)
    monkeypatch.setattr(macos.time, "monotonic", iter([0.0, 0.1]).__next__)
    assert macos.frontmost_app_and_pid() == ("YouTube", 84)
    assert macos.activate("YouTube") is True
    assert scripts[-1].startswith('tell application "System Events" to get displayed name')
    assert not any("set frontmost" in script for script in scripts)


def test_failed_web_app_lookup_and_activation_do_not_claim_success(monkeypatch):
    scripts = []

    def fake_script(script):
        scripts.append(script)
        if "displayed name" in script:
            raise subprocess.CalledProcessError(1, "osascript")
        if "{name, unix id}" in script:
            return "Web App, 84"
        if "get name of first" in script:
            return "Web App"
        return ""

    monkeypatch.setattr(macos, "osascript", fake_script)
    monkeypatch.setattr(macos, "check_abort", lambda: None)
    monkeypatch.setattr(macos.time, "monotonic", iter([0.0, 0.1, 0.2]).__next__)
    monkeypatch.setattr(macos.time, "sleep", lambda _: None)
    assert macos.frontmost_app_and_pid() == ("Web App", 84)
    assert macos.activate("YouTube", timeout=0.1) is False
    assert any("set frontmost" in script for script in scripts)
