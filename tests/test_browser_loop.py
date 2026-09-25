"""The browser step loop, end to end, with Chrome faked at the session boundary.

No Chrome, no network, no API key. `FakeBrowser` answers the JavaScript the backend sends
from a page it holds, the way a page would, and records every input event; `FakeTypeSafe`
answers the classifier from a script. So these tests run the real `run_goal`, `decide`,
`perceive` and run folder, and assert on what reached the classifier, the writer, the page
and the disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer

from typesafe_computer_use.browser.decide import base_state, decide, element_criteria
from typesafe_computer_use.browser.perceive import INTERACTIVE_JS, perceive, to_json
from typesafe_computer_use.browser.report import RunFolder, load_step
from typesafe_computer_use.browser.runner import run_goal, typing_target

SECRET = "hunter2-do-not-leak"


def item(index, name, tag="input", **kwargs):
    base = {
        "index": index,
        "tag": tag,
        "role": "",
        "name": name,
        "x": 10,
        "y": 20 + 30 * index,
        "w": 200,
        "h": 24,
        "in_view": True,
        "covered": False,
        "href": "",
        "field": tag in {"input", "textarea"},
        "secret": False,
    }
    base.update(kwargs)
    return base


def login_page(**extra):
    """A sign-in form. The password field carries a value, as the PR's first version of the
    page script reported it, so a test can see whether any of it travels further."""
    items = [
        item(0, "Username", role="text"),
        item(1, "Password", role="password", secret=True, value=SECRET),
        item(2, "Sign in", tag="button", role="submit", field=False),
    ]
    return {
        "url": "https://example.test/login",
        "title": "Sign in",
        "vw": 1200,
        "vh": 800,
        "count": len(items),
        "items": items,
        "scroll_y": 0,
        "scroll_max": 0,
        "candidates": 3,
        "below_fold": 0,
        "can_scroll": False,
        "history_len": 1,
        "fields": 1,
        **extra,
    }


def text_block(eid, text, **kwargs):
    base = {"e": eid, "text": text, "x": 10, "y": 300, "w": 420, "h": 20}
    base.update(kwargs)
    return base


ANSWER = "SEP 19"


def listing_page(**extra):
    """A concert page whose answer lives in plain text: no control on the page names it,
    so a state built from interactive elements alone has nothing to check."""
    page = {
        "url": "https://example.test/concerts",
        "title": "Concerts",
        "vw": 1200,
        "vh": 800,
        "count": 2,
        "items": [
            item(0, "Home", tag="a", role="link"),
            item(1, "Buy tickets", tag="button", role="button"),
        ],
        "text": [
            text_block("t0", "Upcoming shows"),
            text_block("t1", "The Echo Parade - SEP 19 at the Fillmore, doors 8 PM"),
            text_block("t2", "Sold out: JUN 4 at the Fox"),
        ],
        "scroll_y": 0,
        "scroll_max": 0,
        "candidates": 5,
        "below_fold": 0,
        "can_scroll": False,
        "history_len": 1,
        "fields": 0,
    }
    page.update(extra)
    return page


class FakeBrowser:
    """A page behind a CDP session: answers the page script, focus and field reads, and
    records every Input event the loop sends."""

    def __init__(self, page: dict, *, values: dict[int, str] | None = None):
        self.page = page
        self.values = values or {}
        self.inputs: list[tuple[str, dict]] = []
        self.navigations: list[str] = []

    def evaluate(self, expression, **kwargs):
        if expression is INTERACTIVE_JS:
            return json.loads(json.dumps(self.page))
        if expression == "location.href":
            return self.page["url"]
        if expression == "document.readyState":
            return "complete"
        if "el.focus()" in expression:
            return True
        if "String(el.value)" in expression:
            return self.values.get(0, "")
        return None

    def call(self, method, params=None):
        if method == "Page.navigate":
            self.navigations.append(params["url"])
        else:
            self.inputs.append((method, params or {}))
        return {}

    @property
    def typed(self) -> list[str]:
        return [p["text"] for m, p in self.inputs if m == "Input.insertText"]


def choice(key: str, confidence: float = 0.9) -> ChoiceAnswer:
    return ChoiceAnswer(choice=key, probabilities={key: confidence}, confidence=confidence)


class FakeTypeSafe:
    """The classifier, from a script of (kind, element) pairs, one per step, then done."""

    def __init__(self, *steps: tuple[str, str | None]):
        self.steps = list(steps)
        self.requests: list[dict] = []

    def system_one(self, *, state, questions, model=None):
        self.requests.append({"state": state, "questions": questions})
        if set(questions) == {"ok"}:  # verify_typed
            return SimpleNamespace(answers={"ok": NoulAnswer(noul=0.95)})
        kind, element = self.steps.pop(0) if self.steps else ("done", None)
        answers = {"kind": choice(kind), "satisfied": NoulAnswer(noul=0.0)}
        if "element" in questions:
            answers["element"] = choice(element or "0")
        return SimpleNamespace(answers=answers)

    def sent(self) -> str:
        """Everything the classifier was sent, as text."""
        return json.dumps(
            [{"state": r["state"], "questions": {k: repr(q) for k, q in r["questions"].items()}} for r in self.requests],
            default=str,
        )


class FakeWriter:
    """The Anthropic client, stubbed where `_structured` calls it. Records every request."""

    def __init__(self, reply: dict):
        self.reply = reply
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        block = SimpleNamespace(type="text", text=json.dumps(self.reply))
        return SimpleNamespace(content=[block])


def run(browser, client, tmp_path: Path, writer=None, steps: int = 3):
    folder = RunFolder.create(tmp_path)
    result = run_goal(
        browser,
        client,
        "sign in as alice",
        max_steps=steps,
        change_timeout_ms=0,
        verbose=False,
        writer=writer,
        runfolder=folder,
    )
    return result, folder


def folder_text(folder: RunFolder) -> str:
    return "\n".join(p.read_text() for p in sorted(folder.root.iterdir()))


# ------------------------------------------------------------ password values
def test_a_password_value_never_leaves_the_page(tmp_path):
    """Whatever the page script hands back, a field's value reaches nothing: not the element,
    its label, the classifier's state or criteria, the run folder, or the step log."""
    browser = FakeBrowser(login_page())
    client = FakeTypeSafe(("click", "1"))
    result, folder = run(browser, client, tmp_path)

    page = perceive(FakeBrowser(login_page()))
    assert SECRET not in repr(page.items)
    assert SECRET not in to_json(page)
    assert SECRET not in json.dumps(element_criteria(page))
    assert SECRET not in json.dumps(base_state("g", page, [], url_catalog=None))
    assert SECRET not in client.sent()
    assert SECRET not in folder_text(folder)
    assert SECRET not in "\n".join(s.line() for s in result.steps)


def test_the_page_script_never_reads_what_was_typed():
    """The script reads `el.value` once, for a button input's label, and never names a text
    control after its own contents."""
    assert INTERACTIVE_JS.count("el.value") == 1
    assert "BUTTON_TYPES.has(type) ? el.value" in INTERACTIVE_JS
    assert "value:" not in INTERACTIVE_JS


def test_a_credential_field_is_marked_in_what_the_classifier_reads():
    page = perceive(FakeBrowser(login_page()))
    assert "credential field" in element_criteria(page)["1"]
    assert base_state("g", page, [], url_catalog=None)["elements"][1]["credential_field"] is True


# ------------------------------------------------------------- typing guard
def test_no_writer_offers_no_typing_and_types_nothing(tmp_path):
    browser = FakeBrowser(login_page())
    client = FakeTypeSafe(("type_text", "0"))
    result, _ = run(browser, client, tmp_path)

    offered = client.requests[0]["questions"]["kind"].criteria
    assert "type_text" not in offered and "navigate" not in offered
    assert browser.typed == []
    assert result.steps[0].text_source == "no_writer"


def test_the_named_password_field_is_refused_before_the_writer_is_asked(tmp_path):
    browser = FakeBrowser(login_page())
    client = FakeTypeSafe(("type_text", "1"))
    writer = FakeWriter({"fill": True, "text": "hunter2", "reason": "the goal"})
    result, folder = run(browser, client, tmp_path, writer=writer, steps=1)

    assert browser.typed == []
    assert writer.requests == []
    assert result.steps[0].text_source == "refused_credential"
    assert json.loads((folder.root / "run.json").read_text())["steps"][0]["text_source"] == "refused_credential"


def test_a_password_field_is_never_the_fallback_target():
    """With no field named, typing goes to the first field that is not a credential field,
    and a page whose only field asks for a password gets nothing."""
    page = perceive(FakeBrowser(login_page()))
    target, _ = typing_target(page, None)
    assert target is not None and target.name == "Username"

    only_password = login_page()
    only_password["items"] = [only_password["items"][1]]
    target, why = typing_target(perceive(FakeBrowser(only_password)), None)
    assert target is None and why == "no field"


def test_a_field_labelled_like_a_credential_is_refused_even_when_not_a_password_input():
    page = login_page()
    page["items"][0] = item(0, "One-time code", role="text")
    target, why = typing_target(perceive(FakeBrowser(page)), 0)
    assert target is None and why == "refused_credential"


def test_the_writer_types_into_an_ordinary_field_and_the_step_says_so(tmp_path):
    browser = FakeBrowser(login_page(), values={0: "alice"})
    client = FakeTypeSafe(("type_text", "0"))
    writer = FakeWriter({"fill": True, "text": "alice", "reason": "the goal"})
    result, folder = run(browser, client, tmp_path, writer=writer, steps=1)

    assert browser.typed == ["alice"]
    assert result.steps[0].text_source == "writer"
    assert "text=writer" in result.steps[0].line()
    assert load_step(folder.root, 1)["can_write"] is True


def test_navigate_opens_only_the_writers_https_address(tmp_path):
    browser = FakeBrowser(login_page())
    client = FakeTypeSafe(("navigate", None))
    writer = FakeWriter({"ok": True, "url": "https://example.test/", "reason": "the site"})
    result, _ = run(browser, client, tmp_path, writer=writer, steps=1)
    assert browser.navigations == ["https://example.test/"]
    assert result.steps[0].text_source == "writer"

    browser = FakeBrowser(login_page())
    writer = FakeWriter({"ok": True, "url": "file:///etc/passwd", "reason": "no"})
    result, _ = run(browser, FakeTypeSafe(("navigate", None)), tmp_path, writer=writer, steps=1)
    assert browser.navigations == []
    assert result.steps[0].text_source == "writer_declined"


# ---------------------------------------------------------------- decide
def test_decide_survives_an_answer_without_a_satisfied_noul():
    """The fallback for a missing `satisfied` answer builds a NoulAnswer the SDK accepts."""

    class Partial(FakeTypeSafe):
        def system_one(self, *, state, questions, model=None):
            assert isinstance(questions["kind"], Choice) and isinstance(questions["satisfied"], Noul)
            return SimpleNamespace(answers={"kind": choice("wait"), "satisfied": None})

    decision = decide(Partial(), "g", perceive(FakeBrowser(login_page())), [])
    assert decision.satisfied.noul == 0.0 and decision.kind.choice == "wait"


# ------------------------------------------------------------- page text
def test_an_answer_that_lives_only_in_plain_text_reaches_done(tmp_path):
    """The issue's done-when: the answer is in no control's label, only in the page's
    visible text, and the run still finishes - because the text reached the classifier."""
    browser = FakeBrowser(listing_page())
    client = FakeTypeSafe(("done", None))
    result, folder = run(browser, client, tmp_path, steps=1)

    state = client.requests[0]["state"]
    assert any(ANSWER in block["text"] for block in state["page_text"])
    assert ANSWER not in json.dumps(state["elements"])
    assert result.outcome == "done"
    # The step payload and the saved state in the run folder show the new text.
    assert ANSWER in load_step(folder.root, 1)["payload"]
    saved = json.loads((folder.root / "step-01-state.json").read_text())
    assert any(ANSWER in block["text"] for block in saved["page_text"])


def test_page_text_is_evidence_separate_from_click_targets():
    """Text blocks get evidence ids, not element indexes: nothing in the element question
    or the elements list can point the loop at one."""
    page = perceive(FakeBrowser(listing_page()))
    assert [tb.evidence_id for tb in page.text] == ["t0", "t1", "t2"]

    criteria = element_criteria(page)
    assert set(criteria) == {"0", "1"}
    assert ANSWER not in json.dumps(criteria)

    state = base_state("g", page, [], url_catalog=None)
    assert [el["i"] for el in state["elements"]] == [0, 1]
    assert ANSWER not in json.dumps(state["elements"])
    assert {block["e"] for block in state["page_text"]} == {"t0", "t1", "t2"}


def test_a_page_with_no_text_sends_no_page_text():
    """Pages without readable text keep the lean payload: no empty list in the state."""
    page = login_page()
    del page["items"][1]  # drop the password field, keep username + button
    state = base_state("g", perceive(FakeBrowser(page)), [], url_catalog=None)
    assert state["page_text"] is None


def test_page_text_is_capped():
    page = listing_page(text=[text_block(f"t{i}", f"block number {i} of the page") for i in range(300)])
    assert len(perceive(FakeBrowser(page)).text) == 120


def test_a_password_value_stays_out_of_the_state_when_text_is_collected():
    """Done-when two, with text collection on: the field's value reaches nothing, while
    ordinary page text around the form does."""
    page = perceive(FakeBrowser(login_page(text=[text_block("t0", "Sign in to continue"), text_block("t1", "Welcome back")])))
    blob = json.dumps(base_state("g", page, [], url_catalog=None))
    assert SECRET not in blob
    assert "Welcome back" in blob
    assert SECRET not in json.dumps([tb.__dict__ for tb in page.text]) and SECRET not in to_json(page)


def test_the_page_script_never_collects_what_was_typed_or_drafted():
    """The text pass excludes text controls and editable regions, so a field's contents or
    an unsent contenteditable draft cannot leave the page as 'evidence'."""
    js = INTERACTIVE_JS
    assert "createTreeWalker" in js
    assert "isContentEditable" in js
    assert "TEXTAREA" in js and "SELECT" in js
    assert "textbox" in js and "searchbox" in js
    # Still exactly one read of el.value in the whole script: the button's own label.
    assert js.count("el.value") == 1
