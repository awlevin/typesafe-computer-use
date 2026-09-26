# Browser backend: DOM perception, no OCR

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

## Where browser free text comes from

The writer, as in [how a step works](how-a-step-works.md#where-free-text-comes-from): `compose_browser_text` for a field and `compose_url` for an address,
each with a structured reply. There is no other source, so with no writer the loop does not
offer `type_text` or `navigate`. Every step records where its text came from (`writer`,
`writer_declined`, `writer_error(...)`, `refused_credential`, `no_writer`) in the step line
and the run folder.

Perception never reads what is in a field: an input's value is not collected, and no element
is named after it, so a password on the page cannot reach the classifier, the writer, or the
disk. Password inputs and fields whose `autocomplete` asks for a credential or card data are
marked, and the loop refuses to type into them, or into any field labelled like one, before
the writer is asked.

## Browser run folder and replay

`runs/<timestamp>/` in the same shape as the [desktop run folder](run-folder.md), with `step-NN-elements.json` as the replayable
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

## Browser limits

Viewport only, the same as OCR only saw the visible screen; elements below the fold need
`scroll_down` first. The DOM sees elements rather than paint, so canvas-drawn UI and text
baked into images are invisible here and **are** visible to OCR — use `macos.py` for those.
One tab, one page target, no iframes or shadow-DOM piercing.
