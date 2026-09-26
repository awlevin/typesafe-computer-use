"""The `Desktop` surface over OSWorld's observations, so jev's loop drives an OSWorld VM unchanged.

OSWorld hands its agent one observation per step and takes back a list of actions: pyautogui code
strings, or `WAIT` / `DONE` / `FAIL`. jev's loop reads the screen and acts whenever it likes. This
adapter joins the two at the step boundary:

- Every input (click, key, typing, scroll) appends one pyautogui code string to `actions`.
- The first read of the screen after an input ends the step: the actions go to `next_obs`, which
  hands them to OSWorld and returns the observation taken after they ran. Reads before any input
  use the observation in hand.
- A wait appends `WAIT`, which OSWorld sleeps in the VM. It never sleeps here: time has to pass
  where the page is loading.

The screenshot is the capture at scale 1.0, so a click lands on the capture's own pixels. The app,
window, focused field, URL, and controls come from the accessibility tree, and are simply unknown
when OSWorld sent none; OCR still reads the screenshot. Nothing in the VM can be pressed through
the tree, so the `ax_*` actions refuse and every click is a pointer click.

Text reaches the code strings only through `repr()`, so what the writer composes is data in the VM,
never code. Nothing here touches this machine.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from PIL import Image

from ..models import AxNode, Field
from ..platform_adapter import OcrLine
from . import a11y

NextObs = Callable[[list[str]], dict]  # hands a step's actions to OSWorld, returns the next observation

APP_PID = 1  # stands in for the active app's process: the tree walk reads the active app, not a pid
TYPE_INTERVAL = 0.02  # seconds between keystrokes in the VM
BROWSER_WINDOW_CLASS = "google-chrome"  # the WM_CLASS wmctrl raises for the browser

# macOS key names, as the loop presses them, onto pyautogui's. The Mac's delete key erases backwards.
KEYS = {"return": "enter", "escape": "esc", "delete": "backspace"}


class OSWorldDesktop:
    """One OSWorld run's computer: the observation in hand, and the actions not yet handed over."""

    def __init__(self, obs: dict, recognize_text: Callable[[Image.Image], list[OcrLine]], next_obs: NextObs) -> None:
        self._recognize = recognize_text
        self._next_obs = next_obs
        self.actions: list[str] = []
        self._see(obs)

    def __repr__(self) -> str:
        return f"<OSWorldDesktop, {len(self.actions)} actions pending>"

    # ----- the step boundary -----------------------------------------------------------------

    def _see(self, obs: dict) -> None:
        self._obs = obs
        self._root = a11y.parse(obs.get("accessibility_tree"))
        self._image: Image.Image | None = None

    def _now(self) -> ET.Element | None:
        """The tree of the screen as it is now. Pending input ends the step first."""
        if self.actions:
            actions, self.actions = self.actions, []
            self._see(self._next_obs(actions))
        return self._root

    def _do(self, code: str) -> None:
        self.actions.append(code)

    # ----- the escape hatch ------------------------------------------------------------------

    def check_abort(self) -> None:
        """No corner to slam: OSWorld's own step budget and `reset` stop a run."""

    def sleep_watching(self, seconds: float) -> None:
        if seconds > 0:
            self._do("WAIT")

    def accessibility_trusted(self) -> bool:
        return True

    # ----- input -----------------------------------------------------------------------------

    def click_at(self, point: tuple[float, float]) -> None:
        x, y = point
        self._do(f"pyautogui.click({round(x)}, {round(y)})")

    def press(self, key: str, command: bool = False) -> None:
        if command and key == "[":
            self._do("pyautogui.hotkey('alt', 'left')")  # Back, where Command-[ is Back on a Mac
        elif command:
            self._do(f"pyautogui.hotkey('ctrl', {KEYS.get(key, key)!r})")
        else:
            self._do(f"pyautogui.press({KEYS.get(key, key)!r})")

    def type_text(self, text: str) -> None:
        self._do(f"pyautogui.write({text!r}, interval={TYPE_INTERVAL})")

    def clear_field(self) -> None:
        self._do("pyautogui.hotkey('ctrl', 'a'); pyautogui.press('delete')")

    def scroll(self, lines: int) -> None:
        """Scroll under the pointer, parked over the active window's center when the tree gives it,
        as the Mac adapter parks it: the pointer may sit anywhere after the last click."""
        window = a11y.frame(a11y.active_window(self._root))  # the screen the decision was made on
        if window is None:
            self._do(f"pyautogui.scroll({int(lines)})")
            return
        x, y, w, h = window
        self._do(f"pyautogui.scroll({int(lines)}, x={round(x + w / 2)}, y={round(y + h / 2)})")

    # ----- apps and windows ------------------------------------------------------------------

    def frontmost_app_and_pid(self) -> tuple[str, int]:
        return a11y.name(a11y.active_app(self._now())), APP_PID

    def activate(self, app: str, timeout: float = 3.0) -> bool:
        """Raise the browser's window. No other app can be brought forward in the VM.

        The code checks for wmctrl, so a VM without it skips the raise instead of failing the step.
        """
        if app not in a11y.BROWSER_APPS:
            return False
        self._do(
            "import shutil, subprocess; "
            f"shutil.which('wmctrl') and subprocess.run(['wmctrl', '-xa', {BROWSER_WINDOW_CLASS!r}], check=False)"
        )
        return True

    def open_url(self, browser: str, url: str) -> bool:
        self._do(f"pyautogui.hotkey('ctrl', 'l'); pyautogui.write({url!r}, interval={TYPE_INTERVAL}); pyautogui.press('enter')")
        return True

    def browser_url(self, browser: str) -> str | None:
        return a11y.browser_url(self._now(), browser or None)

    def open_path(self, path: Path, as_text: bool = False) -> None:
        raise NotImplementedError("an OSWorld run shows no file on this machine")

    def frontmost_window_bounds(self, pid: int | None = None) -> tuple[float, float, float, float] | None:
        return a11y.frame(a11y.active_window(self._now()))

    # ----- capture, OCR, and accessibility ---------------------------------------------------

    def screenshot(self) -> Image.Image:
        self._now()
        if self._image is None:
            png = self._obs.get("screenshot")
            if not png:
                raise RuntimeError("OSWorld's observation has no screenshot")
            self._image = Image.open(BytesIO(png)).convert("RGB")
        return self._image

    def display_scale(self, image: Image.Image) -> float:
        return 1.0

    def recognize_text(self, image: Image.Image) -> list[OcrLine]:
        return self._recognize(image)

    def focused_field(self) -> Field | None:
        return a11y.focused_field(self._now())

    def actionable_elements(self, pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
        return a11y.walk(a11y.active_app(self._now()), display_w_pt, display_h_pt)

    # ----- acting on an element: nothing in the VM can be ------------------------------------

    def ax_press(self, ref) -> bool:
        return False

    def ax_focus(self, ref) -> bool:
        return False

    def ax_set_value(self, ref, text: str) -> bool:
        return False

    def ax_value(self, ref) -> str | None:
        return None
