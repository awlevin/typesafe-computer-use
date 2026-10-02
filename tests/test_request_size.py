"""The classifier's request on a real OSWorld step: what it sent, and what it sends now.

TypeSafe bills every input token and caches none, so the request's size is the step's cost. The
fixture is Chrome's settings page as one step's payload file recorded it: the state and the item
criteria that went out, and every item with its box. The items make up most of both, and the
rest of the request, the other questions and their instructions, is the same for every step.
The merge that built those items is gone with the capture, so the test drops what the merge now
drops, `is_icon` over the plain OCR items, and renders the rest as a step does.
"""

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

from PIL import Image

from typesafe_computer_use.decide import base_state, item_criteria
from typesafe_computer_use.models import Item, Screen
from typesafe_computer_use.perception import is_icon

FIXTURE = Path(__file__).parent / "fixtures" / "osworld" / "chrome-settings-step.json"


def wire(value) -> int:
    """Bytes on the wire, encoded as the SDK encodes a request body: compact, UTF-8."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def test_a_captured_step_sends_an_eighth_less_and_loses_no_word():
    captured = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sent = captured["state"]
    screen = Screen(
        image=Image.new("RGB", captured["size"]),
        scale=1.0,
        app=sent["frontmost_app"],
        field=None,
        url=sent["browser_active_tab_url"],
    )
    items = [Item(i, *row) for i, row in enumerate(captured["items"])]
    controls = [it for it in items if it.from_ax]
    kept = [replace(it, index=i) for i, it in enumerate(it for it in items if it.from_ax or not is_icon(it, controls))]

    state = base_state(sent["goal"], screen, kept, sent["previous_actions"], sent["already_tried_on_this_screen"])
    state["now"] = sent["now"]  # the clock the step ran at
    state.update({key: sent[key] for key in ("current_focus", "focused_field")})
    criteria = item_criteria(screen, kept)
    before, after = wire(sent) + wire(captured["item_criteria"]), wire(state) + wire(criteria)
    assert after < 0.88 * before, f"{before} -> {after} bytes"

    # What went: the clock's date on the tabs and buttons under it, and symbols read off buttons.
    assert any("dated" in text for text in captured["item_criteria"].values())
    assert not any("dated" in text for text in criteria.values())
    gone = Counter(it.text for it in items) - Counter(it.text for it in kept)
    assert gone == Counter({"\u00d7": 2, "+": 2, "←": 1, "→": 1, "☆": 1, "……": 1, "▶": 1})
    # What stayed: every control, every word, and the window's own close box, which no control covers.
    assert Counter(it.text for it in kept if any(ch.isalnum() for ch in it.text)) == Counter(
        it.text for it in items if any(ch.isalnum() for ch in it.text)
    )
    assert "\u00d7" in [it.text for it in kept]
