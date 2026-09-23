<p align="center">
  <img src="docs/banner.svg" alt="typesafe-computer-use" width="100%">
</p>

<p align="center">
  <a href="https://github.com/awlevin/typesafe-computer-use/actions/workflows/ci.yaml"><img alt="CI" src="https://github.com/awlevin/typesafe-computer-use/actions/workflows/ci.yaml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white">
  <img alt="macOS, Windows experimental" src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20(experimental)-000000">
  <a href="https://docs.typesafe.ai"><img alt="TypeSafe" src="https://img.shields.io/badge/decisions-TypeSafe%20jev-8b5cf6"></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
</p>

**typesafe-computer-use** drives a Mac toward a goal you type in plain English, for about a
fiftieth of a cent per step. It reads the screen deterministically, asks a small classifier
which action comes next, and only calls a writing model when a text field genuinely needs
free text or the classifier has stopped and the screen needs reading.

```
clicker "go to techcrunch and take me to the checkout page for the cheapest tickets to their next upcoming event" --act
```

> **Beta.** This is under heavy development. Expect rough edges, and expect settings and
> behavior to change between 0.x [releases](https://github.com/awlevin/typesafe-computer-use/releases).
> It drives your real mouse and keyboard, so start with a dry run.

## Why

Frontier-model computer use is capable and expensive: every step ships a screenshot and
waits several seconds for a plan. Most steps do not need a plan. They need one choice
from a short list, made quickly and cheaply, with a confidence number you can gate on.

[TypeSafe](https://docs.typesafe.ai) sells exactly that: a decision model that answers
a `Choice` over up to 255 options with a full probability distribution and a calibrated
confidence, in a few hundred milliseconds, with free output tokens. This project is a
computer-use loop built around it.

Measured on the same screenshot and goal, one decision each:

| | typesafe (jev) | Claude Opus 5, bare screenshot | multiplier |
|---|---|---|---|
| input tokens | 4,882 | 4,785 | same |
| cost per decision | $0.0002 | $0.032 | 155x cheaper |
| cost per decision, realistic loop with history | $0.0002 | $0.035 to $0.08 | 170x to 390x cheaper |
| cost per 12-step task | $0.003 | $0.40 to $0.90 | 130x to 300x cheaper |
| model latency | 0.13 to 0.38 s | 5.2 s | 14x to 40x faster |
| end-to-end step, with capture and OCR | about 1.5 s | about 5.5 s | 3.7x faster |

The honest caveat: the big model read the event dates off the pixels and compared them
unaided. The classifier needed the date parsing described below. Every piece of
reasoning the frontier model does for free has to be rebuilt here as deterministic state.

## Install

macOS 14 or newer, Python 3.12 or newer, [uv](https://docs.astral.sh/uv/). Windows 10 and 11
are experimental; see [Windows](#windows-experimental) below.

```
git clone https://github.com/awlevin/typesafe-computer-use
cd typesafe-computer-use
uv sync
cp .env.example .env     # fill in the keys
```

| variable | required | purpose |
|---|---|---|
| `TYPESAFE_API_KEY` | yes | every decision |
| `ANTHROPIC_API_KEY` | no | `type_text`, writer-proposed URLs, and the final answer |
| `CLICKER_EMAIL` | no | enables the `type_email` action |
| `CLICKER_BROWSER` | no | defaults to `Google Chrome` |
| `CLICKER_WRITER_BASE_URL` | no | send the writer to another endpoint; unset means `api.anthropic.com` |
| `CLICKER_WRITER_API_KEY` | no | the key for `CLICKER_WRITER_BASE_URL`, if it checks one |
| `CLICKER_WRITER_API` | no | what that endpoint speaks: `anthropic` (the default) or `openai` |
| `CLICKER_WRITER_MODEL` | no | types text and proposes URLs; defaults to `claude-haiku-4-5` |
| `CLICKER_ANSWER_MODEL` | no | reads the screen whenever the classifier stops; defaults to `claude-sonnet-5` |
| `CLICKER_WRITER_VISION` | no | `false` for an answer model that reads text only; defaults to `true` |

**Other models.** Point `CLICKER_WRITER_BASE_URL` at any endpoint that speaks the Anthropic
Messages API or, with `CLICKER_WRITER_API=openai`, OpenAI's Chat Completions API: LM Studio,
Ollama, vLLM, a LiteLLM proxy, DeepSeek. Name the models it serves. The full request URL works as
well as the root; for the OpenAI API keep the `/v1`. Such an endpoint may ignore structured-output
parameters, so the schema is also spelled out in the prompt, and code fences or a sentence around
the JSON are tolerated. On the OpenAI API a `json_schema` response format is asked for first, then
`json_object`, then none, stepping down only when the endpoint refuses one. Thinking is turned off,
since a model that thinks by default spends the writer's small token budgets on it and returns no
text. The answer model reads a screenshot; for a model that reads text only, set
`CLICKER_WRITER_VISION=false` and it gets the screen's text alone. A reply that cannot be read
refuses the step it was for, and the run goes on.

Keys never cross over: `CLICKER_WRITER_API_KEY` goes only to `CLICKER_WRITER_BASE_URL`, and
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY` never go there. On the Anthropic API it is sent in both
the `x-api-key` and `Authorization` headers, since proxies differ. Leave it empty for an endpoint
that checks no key.

```
# LM Studio, either of its two APIs
CLICKER_WRITER_BASE_URL=http://localhost:1234
CLICKER_WRITER_MODEL=qwen3.8-flash-next
CLICKER_ANSWER_MODEL=qwen3.8-flash-next

# any OpenAI-compatible server
CLICKER_WRITER_API=openai
CLICKER_WRITER_BASE_URL=https://api.deepseek.com/v1
CLICKER_WRITER_API_KEY=sk-...
CLICKER_WRITER_MODEL=deepseek-v4.1-flash
CLICKER_ANSWER_MODEL=deepseek-v4.1-flash
CLICKER_WRITER_VISION=false
```

`.env` lives at the repo root and is read by every entry point (`clicker`, `clicker-inspect`,
`clicker-gui`).

### Providers and the settings file

The three requests a run makes, the classifier's, the writer's and the answer's, can each be
pointed at a provider of its own: from the window's Settings, by editing
`~/.config/typesafe-computer-use/settings.json` (written readable only by you, since it can hold
keys), or for one run with `--classifier`, `--writer` and `--answer`, each with a `--*-model`.

| provider | writer and answer | classifier | key |
|---|---|---|---|
| Anthropic | yes (the default) | | `ANTHROPIC_API_KEY` |
| TypeSafe (jev) | | yes (the default) | `TYPESAFE_API_KEY` |
| Vercel AI Gateway | yes | yes | `AI_GATEWAY_API_KEY` |
| OpenAI | yes | yes | `OPENAI_API_KEY` |
| OpenRouter | yes | yes | `OPENROUTER_API_KEY` |
| Mistral | yes, text only | | `MISTRAL_API_KEY` |
| xAI | yes | | `XAI_API_KEY` |
| Groq | yes, text only | | `GROQ_API_KEY` |
| Ollama | yes, text only | yes | none |
| custom | yes, any Chat Completions URL | yes, `CLICKER_CLASSIFIER_BASE_URL` | `CLICKER_WRITER_API_KEY`, `CLICKER_CLASSIFIER_API_KEY` |

Every one but Anthropic and TypeSafe is reached through the same Chat Completions client
described above, with the same response-format fallback. The environment always wins: a key in
the shell or `.env` beats one typed into the app, `CLICKER_WRITER_MODEL` and
`CLICKER_ANSWER_MODEL` beat the file's models, and when `CLICKER_WRITER_BASE_URL` or
`CLICKER_WRITER_API` is set, the writer and the answer go to that endpoint as described above,
whatever the file says. With no file and nothing but `ANTHROPIC_API_KEY`, a run is exactly what
it was. An Anthropic key typed into the app goes to `api.anthropic.com` and nowhere else.

A classifier other than jev is a chat model asked the same questions through a JSON schema. It
works, and it is slower and dearer, and its confidence is the model's own opinion of itself
rather than a calibrated number, so `--min-confidence` means less with one. It is counted as the
classifier on the `calls:` line. A label it was not offered is refused, never acted on.

Grant your terminal **Screen Recording** and **Accessibility** in System Settings >
Privacy & Security. Without the first, captures are wallpaper. Without the second,
synthetic clicks are silently dropped, and `--act` refuses to start.

### Windows (experimental)

`windows.py` provides the same adapter over UI Automation, Win32 `SendInput`, and
Windows.Media.Ocr, and `uv sync` installs its packages in place of the macOS ones. It is
untested on Windows: CI runs only its pure rules, on macOS and Linux. Expect it to break, and
please report what you see. Install the English OCR language once, from an elevated PowerShell:

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
```

No permission prompt is needed. What differs from macOS:

- OCR reads English only, and reports no confidence, so every line counts as certain.
- An app is its process: `activate` matches the executable name exactly (`notepad.exe`), or a
  known browser by its product name (`Google Chrome` is `chrome.exe`). Window titles never count.
- A click reads the cursor back first, and refuses to press, ending the run, when the cursor did
  not reach the target (a UAC prompt or the lock screen has the input).
- The browser URL is read off the address bar, which Chrome and Edge show without the scheme.
- The monitor holding the front window is captured. Command-[ and Command-] (back and forward)
  are Alt-Left and Alt-Right, and Command-Up and Command-Down are Control-Home and Control-End.
- `press_menu` is never offered: UI Automation lists a menu only once it is open on screen.
- `open_app` offers only apps that already have a window, since an app is brought forward
  through its window and nothing here launches one by name.
- The window and dictation are macOS only. `clicker` with every flag, the providers and the
  settings file work the same.
- When a policy blocks the installed `clicker.exe`, run `uv run python -m typesafe_computer_use`
  with the same arguments, or `... typesafe_computer_use inspect` for `clicker-inspect`.

## Use

```
uv run clicker "open the Playground"                 # dry run: one step, prints what it would do
uv run clicker "open the Playground" --act           # drives the machine, up to 100 steps
uv run clicker "log in" --act --steps 20 --delay 3   # longer and slower
uv run clicker "log in" --act --handoffs 0           # the classifier alone: its first stop ends the run
uv run clicker-inspect "any goal"                    # 3-2-1, capture, open the annotated screen + payload
uv run clicker "carry the total into Numbers" --act --share-clipboard   # the classifier reads the clipboard
uv run clicker "archive the thread" --act --name-icons                  # name icon-only buttons (below)
uv run clicker-gui                                   # the window (macOS; below)
```

Clear the terminal first. It is on screen, so its text is OCR input.

**Stopping a live run.** Ctrl-C when the terminal has focus, or slam the mouse into the
top-left corner of the screen from any app. The corner is checked before every click, key,
scroll, app switch and accessibility action, and between typed characters, so a run stops
mid-word. A key or mouse button goes back up even when Ctrl-C lands between its down and
its up. The loop also stops itself on `done` or `none`, on confidence under
`--min-confidence` (0.4), when it stalls, or at `--steps`. Each of those stops goes to the
writer, which answers and may hand the run back (below).

**Stalls.** Nothing in an action's description says what came of it; only the next capture
does. So each step keeps a signature of the screen (the app, the page, the text on it) and
the loop stops after three actions in a row that left the screen as it was (a refused
action, a wait on a page still loading, a scroll that has run out of page) or after two in a
row that were already taken on the same screen earlier in the run (a click that does
nothing, or a cycle through two pages). Two captures count as the same screen when at most
one line differs, and that one is one line in ten or fewer: a clock or a ticker does not
hide a stall, and a two-line modal on a dense page is not mistaken for nothing happening.
When more than that changes every step, a run that is getting nowhere runs to `--steps`:
the rules err toward running on, never toward stopping a run that is making progress.

**The answer.** When the classifier stops, the writer reads the screen it stopped on
and prints the result: the information the goal asked for, or where things stand and
the next step when the screen does not hold it. A dry run that would have acted, and
an aborted run, print no answer.

**The hand-off.** A stop is not the end when the goal is not reached. One sentence of
goal does not say which of two good moves comes first ("cheapest, and here in under a
week": open the cheapest listing, or filter by delivery?), and a classifier split
between them reads as low confidence. So the writer's answer may carry a **focus**,
one move in terms of the screen ("Click the 'Arrives in 2-4 days' filter"), and the
classifier goes back to work with the goal and the focus both in its state. On the
capture that stopped such a run at 0.39, the same classifier picks the filter at
0.92 under that focus. It may instead carry a **question**, put to you in the terminal
when there is something only you can say ("13 or 15 inch?"); your reply joins the
state for the rest of the run, the writer reads the screen again with it, and the app
you were in comes back to the front. An empty reply declines, and the answer stands.
The writer never picks a click: every action is still the classifier's.

The exchange cannot go round on itself. A focus the classifier takes no action under
leaves the answer it came with standing, without a second reading of the same screen.
`--handoffs` (10) bounds the trips, three questions bound the asking, and a stop on
the last step is final. A `done` the writer does not see on the screen is sent back
like any other stop.

**Who did the work.** Every run ends by counting the requests each model took:

```
calls: classifier 14 (82%, 3.9s)  writer 3 (18%, 21.4s)  handoffs 1  questions 0
```

The classifier's share is the number the design stands on. A task it falls on is a
task the writer had to steer at every turn, and the fix belongs in the state the
classifier reads, not in more hand-offs.

## The window, and dictation (macOS)

`clicker-gui` opens a window over the same loop: a goal box, the run controls `clicker` takes,
each step's log line and annotated capture as it happens, the answer, a Stop that is looked at
before every step and every quarter second of a wait, and sheets for the providers and the API keys, where every model
menu is filled by asking the provider what it serves. While a run acts, the window hides itself,
since it would otherwise be read as part of the screen, and it comes back when the run ends. The
Dock icon brings it back mid-run. Give the app, or the terminal it starts from, the same Screen
Recording and Accessibility permission `clicker` needs.

Dictation runs Whisper on the Mac through MLX, and the audio never leaves it. It is an extra,
Apple Silicon only, since MLX is heavy: `uv sync --extra voice`. The model downloads on first use.

- **Dictate** records until pressed again, then puts what was said in the goal box.
- **Listen & go** starts the run on the first finished sentence and keeps listening. The growing
  recording is transcribed again every second and a half, and the goal is replaced with the whole
  transcript each time, never appended to: Whisper rewrites what it heard as more arrives, so
  "then..." becomes "then write a summary." rather than both. While you are still talking, a
  classifier that says `done` or `none`, or is under the confidence floor, holds the run instead
  of ending it; the hold costs no step, and the classifier is told the goal is still arriving.
  Stopping the listening lets the run finish.

Control-Option-D starts and stops either from any app (record another chord in Settings); a red
dot in the top right and a system sound say the microphone is live. macOS delivers that chord to
the app only with Accessibility permission.

## Browser backend: DOM perception, no OCR

`macos.py` drives whatever is on screen. For the browser there is a second backend that
never looks at pixels: it reads the DOM over the Chrome DevTools Protocol, so it needs no
Screen Recording permission and cannot fight you for the cursor. It is opt-in: `clicker`
never uses it. `clicker-bench` starts its own Chrome on a fresh, temporary profile (never
yours), whose debugging socket listens on the loopback address and accepts one origin, and
removes the profile when it closes. It reads `TYPESAFE_API_KEY` and the writer's settings
the way `clicker` does, from the environment or `./.env`.

```
uv run clicker-bench loop --fixture --runs runs         # end-to-end step loop
uv run clicker-bench perception --url https://news.ycombinator.com
uv run clicker-bench replay --run runs/<ts> --step 2    # re-decide a saved step offline
```

Perception, same page, same machine, same moment, one decision each. The TypeSafe call is
identical in both paths, so the difference is purely how the screen is read:

| page | DOM (browser backend) | screencapture + Vision OCR | ratio |
| --- | --- | --- | --- |
| local fixture | 1.3 ms (28 elements) | 288.0 ms (31 blocks) | 221x |
| news.ycombinator.com | 4.5 ms (120 elements) | 697.3 ms (46 blocks) | 155x |
| en.wikipedia.org/wiki/Singapore | 14.4 ms (91 elements) | 897.0 ms (50 blocks) | 62x |

The OCR column is roughly 143-177 ms capture + 573-710 ms Vision OCR + ~0.2 ms merge.

Speed is the smaller half of it. OCR reproduced only 7/13, 7/88 and 3/85 of the DOM's labels
verbatim across those pages — under 10% on real sites. A classifier choosing between OCR
blocks is choosing between garbled strings; a classifier choosing between DOM elements is
choosing between the page's actual labels.

End-to-end loop, p50: **302-380 ms per step** (2.6-3.5 steps/sec), of which ~280-350 ms is
the TypeSafe decision. Perception is now ~0% of a step.

```
Runtime.evaluate ─► ordered element list (text, role, click point, on-screen, covered)
                     │
        ONE TypeSafe request, two Choices
        kind    : click | type_text | navigate | press_enter | scroll_down | ... | done
        element : which on-screen element (used only when kind is click)
                     │
        real Input events ─► observe-until-changed ─► next step
```

The action set is filtered to what the page can actually do: no `type_text` without a field
and a writer, no `navigate` without a writer, no `scroll_down` when the document does not scroll, no `back` with
empty history. An option the loop cannot execute is a guaranteed stall, and it reads as
model doubt when the model was never at fault.

The post-action observation and the next step's perception are the same call, so waiting
costs no extra round trip.

### Where browser free text comes from

The writer, as above: `compose_browser_text` for a field and `compose_url` for an address,
each with a structured reply. There is no other source, so with no writer the loop does not
offer `type_text` or `navigate`. Every step records where its text came from (`writer`,
`writer_declined`, `writer_error(...)`, `refused_credential`, `no_writer`) in the step line
and the run folder.

Perception never reads what is in a field: an input's value is not collected, and no element
is named after it, so a password on the page cannot reach the classifier, the writer, or the
disk. Password inputs and fields whose `autocomplete` asks for a credential or card data are
marked, and the loop refuses to type into them, or into any field labelled like one, before
the writer is asked.

### Browser run folder and replay

`runs/<timestamp>/` in the same shape, with `step-NN-elements.json` as the replayable
artefact, since a browser step has no pixels to re-capture:

```
run.log, run.json
step-NN-payload.txt   the exact `state` and every Choice criteria sent
step-NN-state.json    the state, for comparison on replay
step-NN-answers.json  every probability returned
step-NN-elements.json everything perception returned, for offline replay
```

`clicker-bench replay` rebuilds the page from that file and reports whether the
reconstructed state matches the saved one, so a stall can be re-decided without a browser.

### Browser limits

Viewport only, the same as OCR only saw the visible screen; elements below the fold need
`scroll_down` first. The DOM sees elements rather than paint, so canvas-drawn UI and text
baked into images are invisible here and **are** visible to OCR — use `macos.py` for those.
One tab, one page target, no iframes or shadow-DOM piercing.

## How a step works

```
screencapture ─► Vision OCR ─► merge lines into blocks ─► drop lines echoing the goal
accessibility ─► actionable elements (role, label, frame), pruned to the display,
                 the labelled pressable ones it pruned kept as off-screen controls
                     │
                     └─► one numbered list of items, each carrying its source
                     │
accessibility ─► focused field (role, label, placeholder, value, frame)
AppleScript   ─► frontmost app and pid, active tab URL
accessibility ─► the menu bar's enabled commands, the app's titled windows
workspace     ─► the running apps; the clipboard, only when shared
clock         ─► local date and time
dates.py      ─► "dated 2026-10-13 (in 27 days)" on any block containing a date,
                 "near a line dated ..." on its neighbours
layout        ─► "in the row of ..." on any label that appears more than once
runner.py     ─► the actions already tried on this same screen, each of which led back here
                     │
                     ▼
        one TypeSafe request, four Choices, and one more for each of these on offer
        ┌────────────────────────────────────────────────────────────┐
        │ kind      : click_item | use_browser | type_text | scroll… │
        │ item      : which item (click, double or right click)      │
        │ site      : which website (used only for use_browser)      │
        │ key       : which named shortcut (only for press_key)      │
        │ offscreen : which hidden control (only for press_offscreen)│
        │ menu      : which menu command (only for press_menu)       │
        │ window    : which other window (only for focus_window)     │
        │ app       : which app (only for open_app)                  │
        └────────────────────────────────────────────────────────────┘
                     │
                     ▼
        deterministic action ─► wait ─► next step
```

Items carry where they came from: `ocr` for a text block, `ax` for a control the app
declared, `ax+ocr` when both found the same thing. An `ax` item reads as
`button 'Share' (top-right)` in the criteria, so the classifier can tell a real control
from a line of text. A label that appears more than once carries its row as well:
`'Buy' (middle-right; in the row of 'Coldplay', 'Oct 2')`, since the label says nothing
about which and the layout does.

Splitting the decision into questions keeps screen noise out of the action
choice. Every stall found while building this came from two options that meant the
same thing. Confidence measures concentration, so overlapping options always read as
doubt. Keep the action set mutually exclusive.

### OCR cost

Vision is about two thirds of a step, and it charges by the amount of text rather than
the number of pixels, so the only real saving is reading less of the screen.

- **Crop.** Each step reads the frontmost window with an 8 pt margin, plus the menu bar
  strip over the same columns, clamped to the display. Text on the desktop and in
  background windows is noise to the decision. Clipping the strip to the window's width is
  what makes the crop pay on a full-height window. The cost: the clock and the menu extras
  to the right of the window go unread. They stay clickable through the accessibility tree.
- **Reuse.** The capture is compared with the previous one at 1/8 scale, in 256 px tiles.
  Unchanged tiles keep the lines they produced last step. The changed tiles are clustered
  into blobs, sides and corners counting as touching, and each blob becomes a rectangle
  read on its own. Scattered change is the ordinary case, a clock digit plus one repaint,
  and one rectangle around both would span the display. Each rectangle grows until no known
  line straddles its edge, because a crop through a line returns the half it can see; ones
  that meet after growing merge, and more than four merge by closest pair down to four.
  Past 60% changed tiles, past 60% of the region in summed rectangle area, or on an app
  switch or a window move, the whole region is read instead.

The timing line says how much was read, and in how many pieces: `ocr 0.31s (22% of screen,
2 rects)`. A replay (`--image`) always reads the whole image and never reuses, so an offline
repro matches the original run.

### Accessibility tree

OCR cannot see an icon. The accessibility tree can, so each step also walks the frontmost
process for labelled, on-screen controls. Coverage is uneven, measured on ten apps on one
Mac: Finder 100% of on-screen controls labelled, Chrome 88%, Slack 85%, Notion 68%,
Spotify 0 (its CEF shell exposes three window buttons and nothing else). Terminals expose
the grid as one text area. So AX is a bonus source, never a replacement.

Labels live in `AXDescription` for web and Electron, `AXTitle` for AppKit, and a short
`AXValue` otherwise. A decorative image takes the label of the control around it; a list
row takes it from a shallow `AXStaticText`.

Frames lie, so the walk prunes hard:

- skip any subtree whose real frame misses the display (Notes reports rows 200 screens
  down, Chrome parks scrolled-out nodes above the viewport)
- skip any node under 4 pt wide or tall (Chromium clamps scrolled-out web nodes to slivers)
- skip `AXMenu` subtrees, which are thousands of zero-sized items behind a closed menu
- skip nameless `AXGroup` layout boxes, even pressable ones
- stop at 4000 nodes or 0.6 s and say so

Walks measured here: Finder 152 controls in 0.08 s, Chrome 172 in 0.59 s. The assistive
handshake attributes (`AXManualAccessibility`, `AXEnhancedUserInterface`) are unsupported
on this macOS, so nothing relies on them.

#### Off-screen controls

`AXPress` does not need an element to be visible. Notes selects a row parked thousands of
points below the display, Chromium delivers a click to a link it clamped to a 1 px sliver
because the page is scrolled past it, and an auto-hidden Dock hands over all 37 of its
items from 5 pt below the bottom edge. So the same walk keeps the labelled, pressable nodes
it pruned, and offers them as a separate capped list rather than mixing them into the items:
nothing on the capture points at them, and a mouse click would land somewhere else entirely.

The list is deduplicated by role and label, drops any label the visible items already carry,
and stops at 120 controls, after which those subtrees are pruned as before, so the walk costs
what it always did. It is offered only when it is not empty, as a `press_offscreen` action
plus an `offscreen` question, and the step log counts it next to `ax=`. A refusal is the end
of it: there is no pixel to fall back on, so it reads as a no-op. What a walk finds depends
on the app, and the node and time caps bind first on a big tree: Notes and Chrome spend all
4000 nodes on what is already on screen and report nothing hidden.

### Action space

| key | does |
|---|---|
| `click_item` | press the element through the accessibility tree when the item came from it, so the press lands on the control rather than on whatever covers it; a mouse click at the center of the box otherwise, and as the fallback when the press is refused |
| `press_offscreen` | `AXPress` a labelled control the app exposes but does not show, chosen from the off-screen list; offered only when that list is not empty, and a refusal counts as a no-op since there is no pixel to fall back on |
| `double_click_item`, `right_click_item` | the item's pixel, twice or with the other button, never an accessibility press; a right click's context menu is items on the next step |
| `use_browser` | go to the browser, showing the website the `site` answer names: `none` brings it forward on the page already open there, a `SITES` catalog key opens that URL with `open -a`, the URL an argument and never spliced into a script, and `other` opens a URL the writer proposes |
| `open_app` | bring up an app by name, launching it if it is not running, chosen from the apps on this machine, running ones first; never the browser, which is `use_browser`'s, nor the app already in front |
| `press_menu` | run a command of the frontmost app's menu bar through accessibility, without the menu opening; a command whose shortcut a named key sends is left to the key, and nothing that fills in a saved password is offered |
| `focus_window` | raise another titled window of the frontmost app; the one in front is not on offer |
| `press_key` | a named shortcut: save, undo, redo, copy, cut, paste, select all, find, new, new tab, close, forward, reload, the arrows, Tab, top and bottom, zoom. Back is `go_back` and a page at a time is `scroll_down`, so neither is here; paste is never offered into a credential field, nor pressed there if asked |
| `type_text` | the writer composes the string; it is set on the focused element through the accessibility tree, with keystrokes as the fallback when the value does not read back, and a TypeSafe Noul then checks the field's value. Offered only while a text field that is not a credential field has the focus |
| `type_email` | fills in `$CLICKER_EMAIL` the same way, on the same terms |
| `press_enter`, `press_escape` | keyboard |
| `go_back` | Cmd-[, the browser's Back, when the last click led somewhere unhelpful |
| `scroll_down`, `scroll_up` | 10 lines, after parking the cursor over the frontmost window |
| `wait` | screen still loading: 3 s, then the step's own delay, so three waits cover a slow page |
| `done`, `none` | stop |

### Where free text comes from

The classifier never generates text. The writer model runs in three places, a fourth with
`--name-icons`, each with a small packet and a structured reply. Each packet also carries the current focus and what
the user said, once there are any:

- **`type_text`** receives the goal, recent actions, the focused field's label and
  placeholder, and the OCR lines near the field. It returns `{fill, text}`. A credential
  field is refused before the writer is asked: one the platform marks secure
  (`AXSecureTextField`, UI Automation's `IsPassword`), or one whose label or placeholder names a
  credential, the same hints the browser backend uses. The model is also told to answer
  `fill: false` for one; that is the second line, not the first. The text is set as the field's
  value where the element accepts one; otherwise the field is emptied and the text
  typed, since keystrokes land after whatever it already holds. After typing, a Noul
  scores whether the field now holds a sensible value. Under 0.5 the field gets back
  the value it had before, set through the same element, and only while it still holds
  exactly the text just typed. When the element refuses the value, is gone, or holds
  something else by then, the unverified text stays in the field and the history line
  says so. Recovery never presses keys: the focus may have moved to another field.
- **`use_browser`** with `site: other` receives the goal and returns `{ok, url}`.
  Code rejects anything that is not a clean https URL with a hostname.
- **The answer**, each time the classifier stops. It receives the goal, every action
  taken, why the run stopped, the earlier stops with the focus given at each, whether
  anybody is at the terminal to be asked, the text of the last screen, the capture itself, because
  OCR misreads a letter here and there and drops layout, and the text of the distinct
  screens before it, newest first up to 600 lines, because the goal may ask for a price
  that was on the listing and not on the checkout. It returns `{achieved, answer, focus, question}`,
  and is told to take the answer from those screens and the user's replies alone, to give a focus
  as one move and not a plan, and never to ask for a credential. When an action ran after the last capture, the
  screen is captured again first. This one call uses `CLICKER_ANSWER_MODEL`, a stronger
  reader than the per-step writer.

- **Icon names**, with `--name-icons` or the setting. A pressable control with no label of any
  kind is invisible to OCR and nameless in the tree. Those on screen are boxed and numbered on a
  crop of their corner of the capture, and one request to the answer's model names them; a name
  joins the items and is pressed through its element. A layout is named once per run, a failure
  costs the icons and never the step, and each naming counts as the writer's on the `calls:`
  line, which is why it is off by default. It needs a model that reads images.

Passwords are never typed, or pasted. Rely on the browser's password manager or an SSO button
the OCR can read. The value of a secure field never reaches the classifier.

**The clipboard** reaches the classifier only with `--share-clipboard` or the setting, up to 500
characters a step, so a `copy` in one app and a `paste` in another can carry text between them.
It is off by default because the clipboard holds whatever was copied last, a password from a
password manager included, and no field label says so: the credential guard sees fields, never
the clipboard.

## Run folder

Every run writes `runs/<timestamp>/` so a stall can be replayed and fixed offline:

| file | contents |
|---|---|
| `run.log`, `run.json` | everything printed; goal, outcome (`done`, `nothing helps`, `low confidence`, `stalled`, `step limit`, `dry run`, `aborted`, `crashed`), `answer` and `goal_achieved`, seconds, `calls` (requests, share and seconds per model), `handoffs` (step, why the classifier stopped, the focus given), `questions` and replies, every action, config, and `timing` (mean and max seconds per phase, with `steps_timed`) |
| `step-NNN-review.json` | what the writer made of a stop on that step: each answer, focus or question, your reply, and whether the run was handed back |
| `answer-raw.png` | the capture the answer was read from, when an action made the last step's capture stale |
| `step-NNN-raw.png` | the capture |
| `step-NNN.png` | items numbered in blue, accessibility ones orange, the chosen one red, the focused field green |
| `step-NNN-payload.txt` | the exact `state` and criteria sent to TypeSafe, then every item with source, role, box, click point, confidence, then the off-screen controls |
| `step-NNN-answers.json` | every probability the classifier returned, the off-screen controls it was offered, the key, menu command, window or app it named and the menu it was offered, the actions already tried on that screen, the idle and repeat counts the stop rules stood at, plus `timing` for that step |

A step that held for more of a dictated goal writes its files too, and the steps after it are
numbered past them.

Each step also logs what it cost, so a slow phase is obvious:

```
  timing: capture 0.31s  screenshot 0.28s  app 0.01s  window 0.02s  field 0.01s  url 0.01s  ocr 0.31s (22% of screen)  ax 0.06s  decide 0.21s  act 0.05s  total 0.95s
```

`capture` covers the four round trips under it; `act` is left out when the step did not act.

Replay a saved capture as if it were live, without touching the screen:

```
uv run clicker "same goal" --image runs/<ts>/step-003-raw.png --app "Google Chrome" --url "https://example.com/"
```

## Layout

```
typesafe_computer_use/
  platform_adapter.py
                  `desktop`, the one way to the platform: windows.py on Windows,
                  macos.py everywhere else; `Desktop` names what both provide
  apps.py         which installed app a name means, platform-free
  macos.py        the macOS adapter: Quartz, AX, AppleScript, Vision OCR
  windows.py      the Windows adapter (experimental): UI Automation, SendInput,
                  Windows.Media.Ocr
  ax_walk.py      the bounded accessibility-tree walk, shared by both adapters
  perception.py   capture, OCR, the read region and the changed-tile cache,
                  block merging, goal-echo filter, the accessibility item
                  source, and the merge of the two
  dates.py        date parsing and "in N days" hints
  decide.py       state, criteria, the three-Choice request, the Noul check
  writer.py       the writer model, structured replies, URL validation, the answer
                  with its focus or question
  openai_writer.py the writer's requests on an OpenAI-compatible endpoint
  chat_classifier.py
                  the classifier's questions asked of a chat model, for an endpoint
                  that does not serve jev
  settings.py     the provider catalog and the settings file
  session.py      settings into the services one run needs, for the CLI and the window
  labels.py       naming icon-only controls, once per layout
  goal.py         a goal still being dictated while the loop works on it
  voice.py        Whisper through MLX: a growing recording into settled sentences
  actions.py      one handler per action, each returning a history line
  runner.py       the step loop, run folder, stop rules, the hand-off to the writer
                  and back
  calls.py        requests counted per model, at the two clients
  report.py       logging, annotated screenshots, payload dump
  timing.py       phase stopwatches, the timing line, run summary
  cli.py          `clicker`, `clicker-inspect` and `clicker-gui`
  gui/            the macOS window (AppKit), dictation, the microphone, the
                  dictation chord; it reaches the machine being driven only
                  through `desktop`
  browser/        the browser backend (see above), opt-in and independent of macos.py
    cdp.py        the only module that touches the browser        (platform adapter)
    perceive.py   DOM collection: ordered elements, click points, occlusion
    decide.py     browser action set, two-Choice request, answer serialization
    act.py        real Input events, observe-until-changed
    runner.py     step loop, provenance, run folder
    report.py     run folder writing and offline replay
    bench.py      `clicker-bench`: DOM vs OCR, the step loop, replay
tests/            pure logic: dates, merging, reading order, echo filter, config,
                  decisions, the tree walk against a fake tree; for the browser
                  backend, parsing, action filtering, change detection, replay
  world.py        a simulated computer: pages, controls, fields, and what each action
                  does to them, driven by the real step loop with a policy as classifier
  test_scenarios.py
                  tasks of increasing difficulty on that computer, L1 upward; a failure
                  here says the architecture cannot do that task
```

A Linux port adds a third adapter over xdotool, AT-SPI, and PaddleOCR or RapidOCR, and one line
in `platform_adapter.py`. The tree walk takes its children, attributes, and actions as callables,
so only those three bindings change per platform. Nothing else knows which OS it is running on.

The browser backend replaces `macos.py` with `browser/cdp.py` instead. Both are opt-in and
independent: a browser task never needs Screen Recording permission, and a canvas-only task
still wants the OCR path.

## Known limits

- OCR only sees text, and the accessibility tree only covers apps that publish one.
  In a terminal, a canvas, or Spotify, an icon-only button reaches neither source.
- Two identical labels in one row, or in no row at all, get only a coarse region hint and
  split the vote. Ones in different rows are told apart by the text beside them.
- One display is captured each step: the one holding the front window.
- A repeated action whose effect never shows on screen (a third "New note" in an app that
  lists nothing) reads as a cycle and stops the run: the capture is the only witness.
- Using the machine during an `--act` run fights it for focus and the cursor.
- The site catalog is small on purpose; the writer covers the rest.
- A menu command is left out when a named key sends its shortcut; one whose shortcut the menu
  shows as a glyph rather than a character (an arrow, Delete) is offered anyway, beside its key.
- Mistral is reached through its OpenAI-compatible endpoint rather than its own SDK. Its
  response-format handling is the fallback's, and it has not been run against the live API
  since the switch.
- Stacked short lines merge into one item, so a list of checkboxes ("Arrives in 2-4
  days", "Free Shipping", "Local Pickup") that the app does not publish through
  accessibility is one click target, aimed at its middle.

## Development

```
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

CI runs the same on macOS, and the tests again on Linux: they are pure logic, and
`tests/conftest.py` stands in for the platform modules where they cannot be installed.
See [CONTRIBUTING.md](CONTRIBUTING.md).

### Growing the architecture

`tests/test_scenarios.py` is the place to show that a task is beyond the loop. Write the
page graph and a policy for it, assert the outcome, and leave it failing with `xfail`
until the loop can do it; then fix the loop, not the scenario. Every stop rule above was
found or fixed that way.

## License

[MIT](LICENSE)
