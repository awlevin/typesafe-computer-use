# Layout

```
typesafe_computer_use/
  platform_adapter.py
                  `desktop`, the one way to the platform: windows.py on Windows,
                  macos.py everywhere else; `Desktop` names what both provide
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
  actions.py      one handler per action, each returning a history line
  runner.py       the step loop, run folder, stop rules, the hand-off to the writer
                  and back
  calls.py        requests counted per model, at the two clients
  report.py       logging, annotated screenshots, payload dump
  timing.py       phase stopwatches, the timing line, run summary
  cli.py          `clicker` and `clicker-inspect`
  osworld/        jev as an OSWorld agent (see [OSWorld](osworld.md))
    agent.py      `JevAgent`: OSWorld's reset() and predict(), jev's loop on a worker thread
    desktop.py    the adapter over OSWorld's observation: screenshots in, pyautogui code out
    a11y.py       the Ubuntu accessibility tree OSWorld returns, in AX terms
    ocr.py        the OCR backends a run names
    results.py    each task's score, steps, time, and tokens, for `scripts/osworld results`
  browser/        the browser backend (see [browser backend](browser-backend.md)), opt-in and independent of macos.py
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
osworld_overlay/  what `scripts/osworld setup` copies into OSWorld: the agent module and
                  the runner that builds it
scripts/          `sandbox` and `osworld`
```

A Linux port adds a third adapter over xdotool, AT-SPI, and PaddleOCR or RapidOCR, and one line
in `platform_adapter.py`. The tree walk takes its children, attributes, and actions as callables,
so only those three bindings change per platform. Nothing else knows which OS it is running on.

The browser backend replaces `macos.py` with `browser/cdp.py` instead. Both are opt-in and
independent: a browser task never needs Screen Recording permission, and a canvas-only task
still wants the OCR path.
