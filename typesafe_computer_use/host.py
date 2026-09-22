"""Select the desktop adapter for the current operating system.

The decision loop uses the historical name ``macos`` for this module.  Keeping
that alias in the callers means the platform-specific boundary stays small and
the existing macOS implementation remains unchanged.
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from .windows import *  # noqa: F403
else:
    from .macos import *  # noqa: F403
