# Observations

What we have learned from runs. Each observation is one sentence in one bullet; split anything
longer into separate bullets. Keep the whole page short.

## jev against LLM agents

- An LLM agent sees earlier screenshots, so it can notice a layout shift or a click that did nothing.
- jev sees only its last 8 actions as text, so it cannot tell whether an action worked.
- jev clicks about 0.2 s after it looks, against 1–2 s for an LLM, so a layout shift may catch it less often.
- On the bookmark task, jev takes 9 steps and Luna 6, because jev has no right-click or keyboard shortcuts.
- The classifier's input is now most of jev's cost, and a fuller tree makes it larger.
- The classifier picks the same item for the same request only about 95% of the time.

## OSWorld

- The fixed 2 s wait after each action charges per step, so it penalizes an agent that takes more, cheaper steps.
- With no wait, OSWorld's screenshot often shows the screen from before Chrome drew the menu or page a click opened.
- With no wait, jev's tree, read just after the screenshot, can disagree with it, such as a menu half in the tree and not on the screenshot.
- A refused action sends OSWorld nothing, so jev gets no new observation and reads the same stale screen until it stops.
- With three VMs at once, no wait cut OSWorld's part of a jev step from 3.0 s to about 1.3 s, and OCR still takes 2–3 s.
- OSWorld's tree fetch walks the whole desktop (about 2,500 nodes, 2.4 s), but jev needs only the front app's.
- OSWorld's tree request has no timeout, and one hung a run for 9.5 hours.
- OSWorld records video in H.264 4:4:4, which QuickTime cannot play.

## Chrome

- Chrome builds its accessibility tree only after a client asks the app or its window for attributes.
- Chrome's "Restore pages?" bubble covers page controls, and its "Restore" button tempts the classifier.
- A typed value is not saved until Return or leaving the field, and checkers read the saved value.
