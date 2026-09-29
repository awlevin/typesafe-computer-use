"""The `Desktop` surface over OSWorld's observations, so jev's loop drives an OSWorld VM unchanged.

OSWorld hands its agent one observation per step and takes back a list of actions: pyautogui code
strings, or `WAIT` / `DONE` / `FAIL`. jev's loop reads the screen and acts whenever it likes. This
adapter joins the two at the step boundary:

- Every input (click, key, typing, scroll) adds its pyautogui code to the step's code, as lines of
  its own. OSWorld runs each action in the list as a step of its own, with its fixed pause
  (`sleep_after_execution`) and an observation after it, and hands the agent only the last
  observation; so the inputs between two reads (empty a field, type, press Return) go out as one
  action.
- The first read of the screen after an input ends the step: the actions go to `next_obs`, which
  hands them to OSWorld and returns the observation taken after they ran. Reads before any input
  use the observation in hand.
- A pause is a `time.sleep` in the step's code: between two of its inputs, such as closing a popup
  and clicking what it covered, or the step's whole code when a wait is all it does. It never
  sleeps here: time has to pass where the page is loading. Nor is it OSWorld's `WAIT`, which sleeps
  OSWorld's fixed pause, and no time at all while that pause is 0.

The screenshot is the capture at scale 1.0, so a click lands on the capture's own pixels. The app,
window, focused field, URL, and controls come from the accessibility tree, and are simply unknown
when there is none; OCR still reads the screenshot. Nothing in the VM can be pressed through the
tree, so the `ax_*` actions refuse and every click is a pointer click.

The tree comes with the observation, or, given `fetch_tree`, jev fetches it itself (see `tree.py`):
each observation starts its own fetch on a thread as it arrives, so the capture's OCR reads the
screenshot while it runs, and only a read of the tree waits for it. A fetch belongs to the one
observation that started it, which OSWorld took after the step's actions ran, and the step's
actions go to OSWorld only once the fetch before them is over, so no fetch runs while the VM acts.

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
from .tree import Fetched, Later

NextObs = Callable[[list[str]], dict]  # hands a step's actions to OSWorld, returns the next observation
FetchTree = Callable[[], Fetched]  # fetches the VM's tree as it is now

APP_PID = 1  # stands in for the active app's process: the tree walk reads the active app, not a pid
TYPE_INTERVAL = 0.02  # seconds between keystrokes in the VM
BROWSER_WINDOW_CLASS = "google-chrome"  # the WM_CLASS wmctrl raises for the browser
BROWSER_COMMAND = "google-chrome"  # how OSWorld's Chrome tasks start the browser
RAISE_SECONDS = 0.5  # for the window manager to hand the raised window the keyboard

# macOS key names, as the loop presses them, onto pyautogui's. The Mac's delete key erases backwards.
KEYS = {"return": "enter", "escape": "esc", "delete": "backspace"}


class OSWorldDesktop:
    """One OSWorld run's computer: the observation in hand, and the actions not yet handed over."""

    def __init__(
        self,
        obs: dict,
        recognize_text: Callable[[Image.Image], list[OcrLine]],
        next_obs: NextObs,
        *,
        fetch_tree: FetchTree | None = None,
        save_a11y: Path | None = None,
    ) -> None:
        """`fetch_tree` fetches the VM's tree for each observation; without it, the tree is the
        observation's own. `save_a11y` names a folder that gets each observation's raw accessibility
        tree, as `obs-NNN-a11y.xml` (NNN counts observations from 000, the task's first), and OSWorld's
        full tree beside it as `obs-NNN-a11y-full.xml` when a check fetched one. OSWorld keeps none,
        and the raw tree is what a mismatch in `a11y` is diagnosed from. None saves nothing."""
        self._recognize = recognize_text
        self._next_obs = next_obs
        self._fetch_tree = fetch_tree
        self._save_a11y = save_a11y
        self._seen = 0
        self.fetched: list[Fetched] = []  # every tree `fetch_tree` brought, in the order the fetches ended
        self.actions: list[str] = []
        self._see(obs)

    def __repr__(self) -> str:
        return f"<OSWorldDesktop, {len(self.actions)} actions pending>"

    # ----- the step boundary -----------------------------------------------------------------

    def _see(self, obs: dict) -> None:
        index, self._seen = self._seen, self._seen + 1
        self._obs = obs
        self._image: Image.Image | None = None
        if self._fetch_tree is None:
            xml = obs.get("accessibility_tree")
            self._save(index, xml)
            self._tree = Later(value=a11y.parse(xml))
        else:
            self._tree = Later(lambda: self._fetch(index))

    def _fetch(self, index: int) -> ET.Element | None:
        """On the fetching thread: one observation's tree, recorded and saved."""
        fetched = self._fetch_tree()
        self.fetched.append(fetched)
        self._save(index, fetched.xml)
        self._save(index, fetched.full, "-full")
        return fetched.root

    def _save(self, index: int, xml: str | bytes | None, suffix: str = "") -> None:
        if self._save_a11y is None or not xml:
            return
        self._save_a11y.mkdir(parents=True, exist_ok=True)
        data = xml.encode("utf-8") if isinstance(xml, str) else xml
        (self._save_a11y / f"obs-{index:03d}-a11y{suffix}.xml").write_bytes(data)

    def _step(self) -> None:
        """Pending input ends the step: once the last fetch is over, the actions go to OSWorld, and
        the observation it takes after running them comes back."""
        if self.actions:
            actions, self.actions = self.actions, []
            self._tree.wait()
            self._see(self._next_obs(actions))

    def _now(self) -> ET.Element | None:
        """The tree of the screen as it is now. Pending input ends the step first."""
        self._step()
        return self._tree.result()

    def _do(self, code: str) -> None:
        """Add an input's code to the step's. A new line, not `; `, joins them: the browser's code
        branches over several lines, and code after it must run whichever way it branched."""
        if self.actions:
            self.actions[-1] += "\n" + code
        else:
            self.actions.append(code)

    # ----- the escape hatch ------------------------------------------------------------------

    def check_abort(self) -> None:
        """No corner to slam: OSWorld's own step budget and `reset` stop a run."""

    def abort_hint(self) -> str:
        return "OSWorld's step limit, or Ctrl-C on its runner"

    def sleep_watching(self, seconds: float) -> None:
        """A pause goes into the step's code, so it passes in the VM whatever OSWorld's own pause is:
        between two inputs, they stay one action, and a wait alone is an action of its own."""
        if seconds > 0:
            self._do(f"time.sleep({seconds})")

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
        window = a11y.frame(a11y.active_window(self._tree.result()))  # the screen the decision was made on
        if window is None:
            self._do(f"pyautogui.scroll({int(lines)})")
            return
        x, y, w, h = window
        self._do(f"pyautogui.scroll({int(lines)}, x={round(x + w / 2)}, y={round(y + h / 2)})")

    # ----- apps and windows ------------------------------------------------------------------

    def frontmost_app_and_pid(self) -> tuple[str, int]:
        return a11y.name(a11y.active_app(self._now())), APP_PID

    def activate(self, app: str, timeout: float = 3.0) -> bool:
        """Bring the browser forward, starting it when it is not running (a run can close its last
        window). No other app can be brought forward in the VM."""
        if app not in a11y.BROWSER_APPS:
            return False
        self._do(_browser_code(None))
        return True

    def open_url(self, browser: str, url: str) -> bool:
        """Open `url` in the browser: in the front tab of its raised window when it runs, as the
        address bar would, or as the page it starts on when it does not."""
        self._do(_browser_code(url))
        return True

    def browser_url(self, browser: str) -> str | None:
        return a11y.browser_url(self._now(), browser or None)

    def open_path(self, path: Path, as_text: bool = False) -> None:
        raise NotImplementedError("an OSWorld run shows no file on this machine")

    def frontmost_window_bounds(self, pid: int | None = None) -> tuple[float, float, float, float] | None:
        return a11y.frame(a11y.active_window(self._now()))

    # ----- capture, OCR, and accessibility ---------------------------------------------------

    def screenshot(self) -> Image.Image:
        self._step()  # and not the tree: the capture's OCR reads the screenshot while it comes
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


def _browser_code(url: str | None) -> str:
    """VM code that raises the browser's window, and types `url` into its address bar, when it has
    one, and otherwise starts the browser, on `url` when there is one.

    wmctrl both raises the window and says whether there was one to raise. It starts on a line of
    its own, after OSWorld's one-line prefix, so it can branch. The browser starts in a session of
    its own with no pipe to the step: OSWorld waits for a step's output to close, and a browser
    holding it would hold the step up until OSWorld's timeout. The URL is data here too, through
    `repr()`, in the address bar and on the command line alike.
    """
    raised = [f"time.sleep({RAISE_SECONDS})"]
    if url is not None:
        raised.append(
            f"pyautogui.hotkey('ctrl', 'l'); pyautogui.write({url!r}, interval={TYPE_INTERVAL}); pyautogui.press('enter')"
        )
    start = [BROWSER_COMMAND] if url is None else [BROWSER_COMMAND, url]
    lines = [
        "",
        "import subprocess",
        f"if subprocess.run(['wmctrl', '-xa', {BROWSER_WINDOW_CLASS!r}]).returncode == 0:",
        *(f"    {line}" for line in raised),
        "else:",
        f"    subprocess.Popen({start!r}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
        " stderr=subprocess.DEVNULL, start_new_session=True)",
    ]
    return "\n".join(lines)
