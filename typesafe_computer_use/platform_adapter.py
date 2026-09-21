"""Picks the platform adapter at import time, so the rest of the package never checks sys.platform.

Both macos.py and windows.py expose the same function surface (input synthesis, app/window control,
capture, and the accessibility tree via ax_walk.py), so every other module imports this instead of
either one directly and keeps calling it `macos`, the name the whole codebase already uses.
"""

from __future__ import annotations

import sys

if sys.platform == "darwin":
    from . import macos as adapter
elif sys.platform == "win32":
    from . import windows as adapter
else:
    raise RuntimeError(f"typesafe-computer-use has no adapter for platform {sys.platform!r} (supported: darwin, win32)")

globals().update({name: getattr(adapter, name) for name in dir(adapter) if not name.startswith("_")})
