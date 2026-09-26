# How a step works

A step reads the screen, adds the facts code can decide, asks TypeSafe one question in three
parts, and acts. This page follows it from the capture to the stop that ends a run.

```
screencapture ─► Vision OCR ─► merge lines into blocks ─► drop lines echoing the goal
accessibility ─► actionable elements (role, label, frame), pruned to the display,
                 the labelled pressable ones it pruned kept as off-screen controls
                     │
                     └─► one numbered list of items, each carrying its source
                     │
accessibility ─► focused field (role, label, placeholder, value, frame)
AppleScript   ─► frontmost app and pid, active tab URL
clock         ─► local date and time
dates.py      ─► "dated 2026-10-13 (in 27 days)" on any block containing a date,
                 "near a line dated ..." on its neighbours
layout        ─► "in the row of ..." on any label that appears more than once
runner.py     ─► the actions already tried on this same screen, each of which led back here
                     │
                     ▼
        one TypeSafe request, three Choices, four with off-screen controls
        ┌────────────────────────────────────────────────────────────┐
        │ kind      : click_item | use_browser | type_text | scroll… │
        │ item      : which item (used only for click_item)          │
        │ site      : which website (used only for use_browser)      │
        │ offscreen : which hidden control (only for press_offscreen)│
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

Splitting the decision into three questions keeps screen noise out of the action
choice. Every stall found while building this came from two options that meant the
same thing. Confidence measures concentration, so overlapping options always read as
doubt. Keep the action set mutually exclusive.

## OCR cost

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

## Accessibility tree

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

### Off-screen controls

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

## Action space

| key | does |
|---|---|
| `click_item` | press the element through the accessibility tree when the item came from it, so the press lands on the control rather than on whatever covers it; a mouse click at the center of the box otherwise, and as the fallback when the press is refused |
| `press_offscreen` | `AXPress` a labelled control the app exposes but does not show, chosen from the off-screen list; offered only when that list is not empty, and a refusal counts as a no-op since there is no pixel to fall back on |
| `use_browser` | go to the browser, showing the website the `site` answer names: `none` brings it forward on the page already open there, a `SITES` catalog key opens that URL through AppleScript `open location`, and `other` opens a URL the writer proposes |
| `type_text` | the writer composes the string; it is set on the focused element through the accessibility tree, with keystrokes as the fallback when the value does not read back, and a TypeSafe Noul then checks the field's value |
| `type_email` | fills in `$CLICKER_EMAIL` the same way; refused unless a text field is focused |
| `press_enter`, `press_escape` | keyboard |
| `go_back` | Cmd-[, the browser's Back, when the last click led somewhere unhelpful |
| `scroll_down`, `scroll_up` | 10 lines, after parking the cursor over the frontmost window |
| `wait` | screen still loading: 3 s, then the step's own delay, so three waits cover a slow page |
| `done`, `none` | stop |

## Where free text comes from

The classifier never generates text. The writer model runs in three places, each with a
small packet and a structured reply. Each packet also carries the current focus and what
the user said, once there are any:

- **`type_text`** receives the goal, recent actions, the focused field's label and
  placeholder, and the OCR lines near the field. It returns `{fill, text}`. Credential
  fields come back `fill: false` and nothing is typed. The text is set as the field's
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

Passwords are never typed. Rely on the browser's password manager or an SSO button
the OCR can read.

## Stopping a live run

Ctrl-C when the terminal has focus, or slam the mouse into the
top-left corner of the screen from any app. The corner is checked before every click, key,
scroll, app switch and accessibility action, and between typed characters, so a run stops
mid-word. A key or mouse button goes back up even when Ctrl-C lands between its down and
its up. The loop also stops itself on `done` or `none`, on confidence under
`--min-confidence` (0.4), when it stalls, or at `--steps`. Each of those stops goes to the
writer, which answers and may hand the run back (below).

## Stalls

Nothing in an action's description says what came of it; only the next capture
does. So each step keeps a signature of the screen (the app, the page, the text on it) and
the loop stops after three actions in a row that left the screen as it was (a refused
action, a wait on a page still loading, a scroll that has run out of page) or after two in a
row that were already taken on the same screen earlier in the run (a click that does
nothing, or a cycle through two pages). Two captures count as the same screen when at most
one line differs, and that one is one line in ten or fewer: a clock or a ticker does not
hide a stall, and a two-line modal on a dense page is not mistaken for nothing happening.
When more than that changes every step, a run that is getting nowhere runs to `--steps`:
the rules err toward running on, never toward stopping a run that is making progress.

## The answer

When the classifier stops, the writer reads the screen it stopped on
and prints the result: the information the goal asked for, or where things stand and
the next step when the screen does not hold it. A dry run that would have acted, and
an aborted run, print no answer.

## The hand-off

A stop is not the end when the goal is not reached. One sentence of
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

## Who did the work

Every run ends by counting the requests each model took:

```
calls: classifier 14 (82%, 3.9s)  writer 3 (18%, 21.4s)  handoffs 1  questions 0
```

The classifier's share is the number the design stands on. A task it falls on is a
task the writer had to steer at every turn, and the fix belongs in the state the
classifier reads, not in more hand-offs.
