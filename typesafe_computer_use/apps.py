"""Application names: which installed app a name means, platform-free.

The adapters list what is installed and running; this is only the matching, so the classifier's
options, the browser setting and the confirmation that an app came forward all agree on one rule.
"""

from __future__ import annotations

from collections.abc import Iterable


def same_app(process: str, app: str) -> bool:
    """Whether a frontmost process name means this app.

    The two differ often enough to matter: the app called Visual Studio Code runs as a process named
    Code. Either name containing the other is close enough, and the cost of being wrong is one wasted
    step rather than a wrong click.
    """
    a, b = process.casefold().strip(), app.casefold().strip()
    return bool(a and b) and (a == b or a in b or b in a)


def resolve_app(name: str, apps: Iterable[str]) -> str:
    """The installed app a name means: "Chrome" is Google Chrome, and Safari is itself.

    Names are typed by people and read from settings files, and launching refuses anything that is
    not the application's real name. Given what is installed, a near miss resolves instead of failing
    the run. The real name always wins, and among near misses the shortest, the least elaborated one.
    """
    wanted = name.strip()
    if not wanted:
        return wanted
    installed = list(apps)
    for candidate in installed:
        if candidate.casefold() == wanted.casefold():
            return candidate
    near = [candidate for candidate in installed if same_app(candidate, wanted)]
    return min(near, key=len) if near else wanted
