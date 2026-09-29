"""Every Safari web app ("Add to Dock") runs as a process named "Web App"; only its
"displayed name" carries the site's own name. `frontmost_app` and `activate` must resolve
that, or activating a web app always looks like activating "Web App" instead.
"""

from typesafe_computer_use import macos


def test_frontmost_app_reports_the_web_apps_own_name(monkeypatch):
    def fake_osascript(script: str) -> str:
        if "displayed name" in script:
            return "YouTube"
        return "Web App"

    monkeypatch.setattr(macos, "osascript", fake_osascript)
    assert macos.frontmost_app() == "YouTube"


def test_frontmost_app_is_unaffected_for_an_ordinary_app(monkeypatch):
    monkeypatch.setattr(macos, "osascript", lambda script: "Finder")
    assert macos.frontmost_app() == "Finder"


def test_frontmost_app_and_pid_reports_the_web_apps_own_name(monkeypatch):
    def fake_osascript(script: str) -> str:
        if "displayed name" in script:
            return "YouTube"
        return "Web App, 4242"

    monkeypatch.setattr(macos, "osascript", fake_osascript)
    assert macos.frontmost_app_and_pid() == ("YouTube", 4242)


def test_activate_recognizes_a_safari_web_app_by_its_displayed_name(monkeypatch):
    calls = []

    def fake_osascript(script: str) -> str:
        calls.append(script)
        if "to activate" in script:
            return ""
        if "displayed name" in script:
            return "YouTube"
        return "Web App"

    monkeypatch.setattr(macos, "osascript", fake_osascript)
    assert macos.activate("YouTube") is True
    # no fallback "set frontmost of process" AppleScript should be needed
    assert not any("set frontmost" in script for script in calls)
