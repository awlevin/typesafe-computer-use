"""The providers the settings can name: what each sends, and what it makes of what comes back.

Every request here goes to the `endpoint` fixture on loopback, which speaks both APIs.
"""

import json

import pytest
from PIL import Image
from typesafe_sdk import Choice, Noul

from typesafe_computer_use import session
from typesafe_computer_use.calls import CLASSIFIER, WRITER, Calls, MeteredClassifier, MeteredWriter
from typesafe_computer_use.chat_classifier import ChatClassifier, ClassifierError, clamp, read_answer
from typesafe_computer_use.models import Item, Screen
from typesafe_computer_use.openai_writer import OpenAIWriter
from typesafe_computer_use.settings import DECISION_PROVIDERS, TEXT_PROVIDERS, Endpoint, SettingsError
from typesafe_computer_use.writer import Configured, client_for, compose_answer, compose_url, reads_images, speaks_to_anthropic


@pytest.fixture
def chat(endpoint):
    return OpenAIWriter(endpoint.url + "/v1", "stub")


def configured(endpoint, model="gpt-x", vision=True) -> Configured:
    return Configured(OpenAIWriter(endpoint.url + "/v1", "stub"), model, vision, f"Test / {model}")


# --- the writer ---------------------------------------------------------------------------------


def test_a_configured_writer_sends_every_request_to_its_own_model(clean_env, endpoint):
    endpoint.state["reply"] = '{"ok": true, "url": "https://example.com/", "reason": ""}'

    assert compose_url(configured(endpoint, "openai/gpt-4.1"), "open example", []) == "https://example.com/"

    assert endpoint.seen[-1]["body"]["model"] == "openai/gpt-4.1"  # not writer_model()'s default
    assert "matching this schema" in endpoint.seen[-1]["body"]["messages"][0]["content"]  # not Anthropic: schema in the prompt


def test_a_writer_whose_model_has_no_eyes_is_sent_the_text_alone(clean_env, endpoint):
    endpoint.state["reply"] = '{"achieved": true, "answer": "done", "focus": "", "question": ""}'
    screen = Screen(image=Image.new("RGB", (200, 100)), scale=2.0, app="Notes", field=None, url=None)
    items = [Item(0, "Saved", 1.0, 0, 0, 10, 10)]

    compose_answer(configured(endpoint, "codestral-latest", vision=False), "save", screen, items, [], "done")
    compose_answer(configured(endpoint, "gpt-4.1"), "save", screen, items, [], "done")

    first, second = (json.dumps(req["body"]["messages"]) for req in endpoint.seen[-2:])
    assert "image_url" not in first and "image_url" in second


def test_the_vision_variable_still_decides_when_it_is_set(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_VISION", "false")
    assert not reads_images(configured(endpoint, vision=True))
    clean_env.delenv("CLICKER_WRITER_VISION")
    assert reads_images(configured(endpoint, vision=True))
    assert reads_images(object())  # a writer that says nothing of itself reads images, as before


def test_what_a_writer_says_of_itself_reads_through_the_meter(clean_env, endpoint):
    metered = MeteredWriter(configured(endpoint, vision=False), Calls())
    assert metered.vision is False and not speaks_to_anthropic(metered)


def test_an_openai_compatible_writer_needs_somewhere_to_go():
    with pytest.raises(ValueError, match="base URL"):
        client_for("openai", None, "k")


def test_every_text_provider_in_the_catalog_builds_a_client(clean_env):
    for key, spec in TEXT_PROVIDERS.items():
        endpoint = Endpoint(provider=key, base_url="http://127.0.0.1:1/v1" if key == "custom" else "")
        writer = session.make_text_writer(endpoint, spec.default_model or "some-model", {spec.key_env: "stub"})
        assert isinstance(writer, Configured) and writer.label.startswith(spec.label)


# --- the chat classifier -------------------------------------------------------------------------


def fill(body: dict) -> str:
    """Answer the schema the way a well-behaved model would: the first label, fairly sure."""
    schema = json.loads(body["messages"][0]["content"].split("matching this schema:\n", 1)[1])
    reply: dict = {}
    for name, prop in schema["properties"].items():
        if prop.get("type") == "object":
            labels = prop["properties"]["choice"]["enum"]
            reply[name] = {
                "choice": labels[0],
                "confidence": 0.82,
                "alternatives": [{"label": labels[0], "probability": 0.82}, {"label": labels[-1], "probability": 0.18}],
            }
        elif prop.get("type") == "number":
            reply[name] = 0.9
        else:
            reply[name] = "because"
    return json.dumps(reply)


def test_a_chat_model_answers_the_step_questions_in_jevs_place(chat, endpoint):
    endpoint.state["reply"] = fill
    questions = {
        "kind": Choice(instructions="what next?", criteria={"click_item": "click one", "wait": "hold on"}),
        "site": Choice(instructions="which site?", criteria={"github": "github", "none": "stay"}),
    }

    answers = (
        ChatClassifier(chat, "openai/gpt-4.1-mini", "Test").system_one(state={"goal": "open a PR"}, questions=questions).answers
    )

    assert answers["kind"].choice == "click_item" and answers["kind"].confidence == 0.82
    assert answers["kind"].probabilities == {"click_item": 0.82, "wait": 0.18}
    assert answers["site"].choice == "github"
    sent = endpoint.seen[-1]["body"]
    assert sent["model"] == "openai/gpt-4.1-mini" and json.loads(sent["messages"][1]["content"][0]["text"])["state"] == {
        "goal": "open a PR"
    }


def test_the_typing_check_is_still_a_probability(chat, endpoint):
    endpoint.state["reply"] = fill

    answers = ChatClassifier(chat, "m", "Test").system_one(
        state={"typed": "hi"}, questions={"ok": Noul(instructions="did it land?")}
    )
    answers = answers.answers

    assert answers["ok"].noul == 0.9


def test_a_chat_classifier_is_counted_as_the_classifier(chat, endpoint):
    endpoint.state["reply"] = fill
    calls = Calls()

    MeteredClassifier(ChatClassifier(chat, "m", "Test"), calls).system_one(
        state={}, questions={"kind": Choice(instructions="?", criteria={"a": "a", "b": "b"})}
    )

    assert calls.count == {CLASSIFIER: 1, WRITER: 0}


def test_a_label_the_model_invented_is_refused_rather_than_clicked():
    with pytest.raises(ClassifierError, match="not one of its labels"):
        read_answer({"choice": "click_the_blue_one", "confidence": 1.0, "alternatives": []}, ["click_item"], "kind")


def test_a_model_that_scores_nothing_still_leaves_a_confidence_to_gate_on():
    answer = read_answer({"choice": "wait", "confidence": 0.35, "alternatives": []}, ["wait", "done"], "kind")

    assert answer.confidence == 0.35 and answer.probabilities == {"wait": 1.0}


def test_probabilities_are_normalised_however_loosely_the_model_scored_them():
    raw = {"choice": "a", "confidence": 0.5, "alternatives": [{"label": "a", "probability": 3}, {"label": "b", "probability": 1}]}

    assert read_answer(raw, ["a", "b"], "kind").probabilities == {"a": 0.75, "b": 0.25}


def test_a_number_the_model_sent_is_clamped_to_a_probability():
    assert clamp(1.4) == 1.0 and clamp(-2) == 0.0 and clamp("not a number") == 0.0


def test_an_endpoint_that_fails_is_a_classifier_error_that_names_it(chat, endpoint):
    endpoint.state["reject"] = lambda body: "model 'nope' not found"

    with pytest.raises(ClassifierError, match="Test failed"):
        ChatClassifier(chat, "nope", "Test").system_one(state={}, questions={"k": Choice(instructions="?", criteria={"a": "a"})})


# --- the model menus -----------------------------------------------------------------------------


def test_the_model_menu_is_filled_from_the_endpoint_itself(endpoint):
    endpoint.state["models"] = ["openai/gpt-4.1", "grok-4"]

    listed = session.list_models(Endpoint(provider="custom", base_url=endpoint.url + "/v1"), TEXT_PROVIDERS)

    assert listed == ["grok-4", "openai/gpt-4.1"]
    assert endpoint.seen[-1]["path"] == "/v1/models"


def test_anthropic_lists_its_models_through_its_own_sdk(clean_env, endpoint):
    endpoint.state["models"] = ["claude-haiku-4-5", "claude-sonnet-5"]

    listed = session.list_models(
        Endpoint(provider="anthropic", base_url=endpoint.url), TEXT_PROVIDERS, {"ANTHROPIC_API_KEY": "k"}
    )

    assert listed == ["claude-haiku-4-5", "claude-sonnet-5"]


def test_a_menu_that_needs_a_key_says_which_one_is_missing(clean_env):
    clean_env.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SettingsError, match="OPENROUTER_API_KEY"):
        session.list_models(Endpoint(provider="openrouter"), DECISION_PROVIDERS)


# --- the run ------------------------------------------------------------------------------------


def test_a_run_opens_the_classifier_the_settings_chose_and_never_the_default(monkeypatch, tmp_path):
    from world import FakeTypeSafe, Page, World, scripted

    from typesafe_computer_use import runner
    from typesafe_computer_use.actions import Context

    world = World([Page(name="home", items=["Order placed"])])
    world.install(monkeypatch)
    monkeypatch.setattr(runner, "TypeSafeClient", lambda: pytest.fail("the default classifier must not be opened"))
    chosen = FakeTypeSafe(scripted(("done", None)))
    cfg = runner.RunConfig(goal="place the order", out=tmp_path / "run", act=True, delay=0)

    state = runner.run(
        cfg,
        lambda typesafe, history: Context(
            goal=cfg.goal, browser="Safari", email=None, typesafe=typesafe, writer=None, history=history
        ),
        lambda: chosen,
    )

    assert state.outcome == "done" and len(chosen.states) == 1
    assert state.calls.count[CLASSIFIER] == 1
