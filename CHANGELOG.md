# Changelog — `jev-features`

Everything on the `jev-features` branch that is not on upstream `main` (`c96dbdd`, "A corner abort
stops every input, and a failed check never empties another field (#10)").

The features were built in the fork on top of the older commit `cc7b506`, then rebuilt onto
`c96dbdd` so they fit upstream's current architecture: the `Desktop` protocol in
`platform_adapter.py`, the shared tree walk in `ax_walk.py`, call counting in `calls.py`, the
`OpenAIWriter`, and the simulated-computer test harness. The branch contains 9 commits, from
23 Sep 2026 08:37 to 09:07: 56 files changed, 7,402 insertions, 214 deletions.

Checks: `uv run ruff check .` and `uv run ruff format --check .` are clean. `uv run pytest -q`
gives 678 passed and 1 xfailed; the xfail is upstream's own L40 known-limit scenario, unchanged.
None of this was verified on a live machine: there was no `--act` run, no window opened, no
microphone used, and no provider request made. Nothing has been run on Windows.

---

## New features

### A macOS window: `clicker-gui`
- A native AppKit window over the same loop `clicker` drives (`runner.run`, on a background thread).
- It has a goal box, the run controls (act, steps, min confidence, delay), and a live step log.
  Each step's annotated screenshot and caption appear as they happen, the answer when the run
  ends, and a Stop button. Stop is checked before every step and every quarter second of a wait.
- It hides itself while it drives, because otherwise its own text would be read as part of the
  screen. It comes back when the run ends; the Dock icon brings it back mid-run.
- A Settings sheet chooses the classifier, writer and answer providers. Every model menu is
  filled by asking the provider itself what it serves. The sheet also holds the browser, the
  email, dictation options, and the two opt-in switches below.
- An API keys sheet has one secure field per key. A key already set in your shell or `.env` is
  shown as such and can't be overridden.
- A real menu bar: Settings (⌘,), API keys (⌥⌘,), Run (⌘R), Stop (⌘.), Dictate (⌘D), Listen & go (⌘L).
- Closing the window during a run asks first: stop and close, or keep running hidden.
- Files: `gui/app.py`, `gui/settings_panel.py`, `gui/keys_panel.py`, `gui/widgets.py`,
  `gui/overlay.py`, `gui/hotkey.py`, `gui/sounds.py`, `gui/listening.py`, `gui/audio.py`.

### Local dictation (the `voice` extra, Apple Silicon only)
- Whisper runs on the Mac through MLX, and the audio never leaves the machine. Install it with
  `uv sync --extra voice`. The model downloads on first use; it defaults to
  `mlx-community/whisper-large-v3-turbo`, and the language can be set or detected automatically.
- **Dictate** records until you stop it, then puts the transcript in the goal box, either
  appended or replacing what's there (a setting).
- **Listen & go** starts the run on your first finished sentence and keeps listening. The
  recording is re-transcribed every 1.5 s, and the goal is replaced with the whole transcript
  each time, never appended to. Whisper corrects itself ("then…" → "then write a summary."),
  and appending would double the text. Recordings rotate before Whisper's 30 s window.
- A global chord starts and stops dictation from any app. The default is ⌃⌥D, and you can
  record another in Settings. A pulsing red dot in the top-right corner and a system sound show
  when the microphone is live.
- WAV files that are still being written are read to their end, with no ffmpeg dependency.
- Files: `voice.py`, `gui/listening.py`, `gui/audio.py`, `gui/hotkey.py`.

### A goal that is still being spoken (`LiveGoal`)
- `runner.run(..., live=LiveGoal)` reads the goal again after every capture, so words spoken
  during a capture count.
- While you are still talking, a `done`, a `none`, or a decision below the confidence floor
  **holds** the run instead of ending it:
  - a hold costs no step from `--steps`;
  - it doesn't count as an action that changed nothing;
  - it writes its own step files, numbered past so none are overwritten.
- The classifier is told the goal is still arriving (`the_user_is_still_speaking_this_goal`),
  and `done`/`none` are described as "so far" while it is.
- The writer and the typing check use the goal as it reads now, not as it read at the start.
- File: `goal.py`.

### Choose any provider for each job: settings file and catalog
- The classifier, the writer, and the answer can each point at their own provider. Choose them
  in the window, in `~/.config/typesafe-computer-use/settings.json` (mode 0600, since it can
  hold keys), or for one run with `--classifier`, `--writer`, `--answer` and `--*-model`.
- The catalog: Anthropic (default writer), TypeSafe jev (default classifier), Vercel AI Gateway,
  OpenAI, OpenRouter, Mistral, xAI, Groq, Ollama, and a custom OpenAI-compatible URL. Every
  provider except Anthropic and TypeSafe goes through upstream's `OpenAIWriter`, and gets its
  json_schema → json_object → none response-format fallback.
- **A chat model can be the classifier** in jev's place (`chat_classifier.py`). It answers
  `system_one` in the TypeSafe SDK's own `ChoiceAnswer`/`NoulAnswer` types. It refuses a label
  it wasn't offered instead of acting on it. Its confidence is the model's own opinion rather
  than a calibrated probability.
- The writer and the answer can be on different providers, and each carries its own model and
  whether that model reads images. The answer falls back to the writer when it has no key.
- Precedence is: the environment beats the settings file, which beats the catalog.
  `CLICKER_WRITER_BASE_URL` / `CLICKER_WRITER_API` still hand the writer to upstream's
  `make_writer`, unchanged. With no settings file and only `ANTHROPIC_API_KEY`, a run behaves
  exactly as before (Haiku writer, Sonnet answer).
- New variables: `CLICKER_CLASSIFIER_BASE_URL` and `CLICKER_CLASSIFIER_API_KEY` for a custom
  chat classifier. `CLICKER_WRITER_API_KEY` is the custom writer's key.
- Files: `settings.py`, `session.py`, `chat_classifier.py`.

### Six new actions, each offered only when it has a target this step
| action | what it does |
|---|---|
| `open_app` | Brings up an installed app by name, launching it if needed, with running apps listed first. Never offers the browser (that's `use_browser`) or the app already in front. |
| `press_menu` | Runs a command from the frontmost app's menu bar through accessibility, without opening the menu. The Apple menu and destructive items are never offered. |
| `focus_window` | Raises another titled window of the frontmost app. The window already in front isn't offered. |
| `press_key` | Presses a named shortcut: save, undo, redo, copy, cut, paste, select all, find, new, new tab, close, forward, reload, the arrows, Tab/Shift-Tab, backspace, top/bottom, zoom in/out. |
| `double_click_item` | Double-clicks an item's pixel, sending a real click count, to open files and folders. |
| `right_click_item` | Right-clicks an item. The context menu's entries become items on the next step. |

A key, a menu command, or a window must pass the same confidence gate as a click. `open_app`
doesn't, for the same reason `use_browser` doesn't: the wrong app can simply be left again.

### Multi-display capture
- Each step captures the display that holds the front window, and records that display's
  origin. Clicks and accessibility frames are converted through it, so a window on a second
  screen works (`Screen.origin`, `Screen.to_pixels`).
- The menu bar, the app's windows, and the running apps are read into each step's screen.

### Clipboard sharing (opt-in: `--share-clipboard` or the setting)
- Shows the classifier up to 500 characters of clipboard text each step, so a `copy` in one app
  and a `paste` in another can carry text between them.
- **Off by default.** The clipboard can hold a password copied from a password manager, and the
  credential guard only sees fields, never the clipboard.

### Naming icon-only buttons (opt-in: `--name-icons` or the setting)
- A pressable control with no label at all is invisible to OCR and nameless in the
  accessibility tree. These controls are boxed and numbered on a crop of their corner of the
  screenshot, and one request to the answer's model names them. Each name becomes an item, and
  pressing it presses the real element.
- Each layout is named once per run. A failed naming costs the icons, never the step. A name for
  a box that was never drawn is ignored.
- Each naming counts as the writer's work on the `calls:` line, which is why it's off by
  default. It needs a model that reads images.
- The request lives in `writer.compose_icon_names`, where all free text comes from. The
  crop/draw/cache logic is in `labels.py`.

### Following and stopping a run: `RunHooks`
- `runner.run(..., hooks=RunHooks(...))` delivers every log line, a `StepEvent` per step (with
  its annotated image), the final answer, and a Stop that is checked every step and every
  quarter second of a wait. The window uses this; the terminal doesn't need it.

---

## Desktop protocol: new platform calls, in both adapters

Added to `Desktop` in `platform_adapter.py`, implemented in `macos.py` and `windows.py`, and
checked parameter by parameter by `test_platform_adapter.py`:

| call | macOS | Windows |
|---|---|---|
| `click_at(point, clicks=1, right=False)` | Quartz, with a click-count field | SendInput, left or right button |
| `press(key, command=False, shift=False)` | Quartz flags | Shift held around the chord; ⌘[ / ⌘] → Alt-Left/Right, ⌘↑ / ⌘↓ → Ctrl-Home/End |
| `installed_apps()` | disk scan of /Applications and related folders, plus Finder | apps with a window (activate can only reach those) |
| `running_apps()` | NSWorkspace, Dock apps only | same as installed |
| `app_windows(pid)` / `raise_window(ref)` | AXWindows / AXRaise + AXMain | EnumWindows / SetForegroundWindow |
| `menu_items(pid, limit)` | AXMenuBar walk, with each item's shortcut | returns `[]`: UIA lists a menu only once it's open on screen, so `press_menu` is never offered |
| `clipboard_text()` | NSPasteboard | win32clipboard |
| `active_displays()` / `screenshot(display)` / `display_scale(image, bounds)` | CGGetActiveDisplayList, `screencapture -D` | EnumDisplayMonitors, ImageGrab with a bounding box |
| `nameless_elements(pid, w, h)` | the same bounded walk, collecting unlabelled icons | same |
| `focused_field().secure` | `AXSecureTextField` | UIA `IsPassword` |

- `actionable_elements` keeps upstream's 3-tuple. Unlabelled icons come from a separate walk,
  so runs without icon naming pay nothing extra.
- New platform-free helper `apps.py` (`same_app`, `resolve_app`). "Chrome" resolves to Google
  Chrome, and "Code" counts as Visual Studio Code.
- New dependencies: `pyobjc-framework-Cocoa` and `pyobjc-framework-AVFoundation` (both darwin
  only).

---

## Fixes and safety

- **Nothing is typed or pasted into a credential field on any path.** A field counts as a
  credential field when the platform marks it secure, or when its label or placeholder names a
  credential. `type_text` and `type_email` refuse it before the writer is asked, and
  `compose_text` refuses it too. Upstream only guarded the browser backend.
- **A secure field's value never reaches the classifier's state.**
- **The password-label check is fixed for short hints.** Hints of four letters or fewer
  (`pin`, `otp`, `cvv`, …) now match whole words only. Before, "pin" flagged "Shipping address",
  "Typing speed", and "Your opinion". Longer hints still match anywhere, so "NewPassword" is
  still caught. This fixes the browser backend's guard too.
- **No AppleScript injection from URLs.** `open_url` used to paste a writer-proposed URL into an
  AppleScript string, and a `"` in the URL got through. URLs and app names now go to `open -a`
  as separate arguments.
- **Typing is offered only when it could run:** `type_text`/`type_email` need a typable,
  non-credential field with the focus. On a live run in the fork, an always-available
  `type_text` split the vote and held a good click to 0.38, below the floor; without it, the
  same step chose the click at 0.73.
- **Duplicate actions removed** (they split the vote):
  - `press_key` no longer has "back" (⌘[, which is `go_back`) or page up/down (which are
    `scroll_up`/`scroll_down`).
  - `press_menu` drops any command whose shortcut a named key already sends.
  - `open_app` drops the browser and the front app; `focus_window` drops the front window.
- **Never offered from a menu:** AutoFill, password and passkey commands. Paste isn't offered
  into a credential field, either as a key or from the menu.
- **The annotated screenshot** now marks a double- or right-clicked item, and no longer marks an
  on-screen item that happens to share its number with an off-screen control being pressed.
- **The test guard covers everything new.** Each of these refuses during tests, and has a
  refusal test in `test_no_real_machine.py`:
  - `open -a`, the clipboard, raising a window, and the new click and key shapes;
  - where AppKit exists: the microphone and its permission prompt, the global key monitor,
    system sounds, any window put on screen, modal alerts, and hiding or raising the app.
- `AGENTS.md` now also lists `clicker-gui`, microphone capture, and global keyboard monitors as
  needing approval.

---

## Tests
- **New scenarios L54–L63** run the real `runner.run` loop on the simulated computer:
  - opening an app; open_app not offered when there's no other app;
  - a menu command (with Undo and AutoFill withheld); another window (and a single window
    offering none);
  - a named shortcut; double and right clicks;
  - nothing offered that could put text into a password field;
  - a dictated goal that grows mid-run, with two holds, then typing and finishing; a live goal
    that has already finished.
- `tests/world.py` gained a menu bar, windows, other apps, chords, double and right clicks,
  secure fields, and speech arriving between captures. Every earlier scenario is unchanged.
- New test files: `test_desktop_reads.py`, `test_reach.py`, `test_settings.py`,
  `test_providers.py`, `test_labels.py`, `test_goal.py`, `test_voice.py`,
  `test_listening.py`, `test_gui.py`.
- The loopback `endpoint` fixture now also serves `GET /models` and can compute a reply per
  request.
- One upstream test was changed: `test_kind_criteria_offers_email_only_when_set` now passes
  the focused field that `type_email` needs, and also asserts the action is absent without one.
- `numpy` was added to the dev group, so dictation's WAV and sentence logic is tested without MLX.

---

## Command line and packaging
- New entry point: `clicker-gui` (macOS only; it exits with a message elsewhere).
- New `clicker` flags: `--classifier`, `--classifier-model`, `--writer`, `--writer-model`,
  `--answer`, `--answer-model`, `--share-clipboard`, `--name-icons`.
- On startup `clicker` prints the classifier, writer and answer it will use, plus notes: a
  missing writer key, an answer model without vision, icon naming being on.
- New optional extra `voice`: `mlx-whisper==0.4.3` and `numpy==2.5.3`, for darwin/arm64 only.
- Removed the fork's dependency on `mistralai`. Mistral goes through its OpenAI-compatible
  endpoint, which **has not been run against the live Mistral API**.
- `README.md`, `CONTRIBUTING.md` and `.env.example` describe all of the above, including what a
  Windows user does and doesn't get.

---

## Deliberately not carried over from the fork
- The `techcrunch` entry in `SITES`: the site catalog is curated, and the writer covers the rest.
- The catch-all that turned any exception during an action into a no-op. It would also have
  swallowed the test guard's refusals, so a test reaching the real machine would pass quietly.
- The fork's `is_noop` stall rule, which upstream's screen-signature stall rules replace.
- The deletion of `.env.example`: it's kept, with the new variables added.

## Known limits
- One display is captured per step: the one holding the front window.
- A menu command whose shortcut is shown as a glyph (an arrow, Delete) is still offered
  alongside its named key.
- A chat-model classifier's confidence isn't calibrated, so `--min-confidence` means less with one.
- Windows: no `press_menu`, `open_app` only for apps that have a window, and no window or
  dictation. None of it has been run on Windows.
