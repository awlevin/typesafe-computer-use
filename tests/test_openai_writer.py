import json

import pytest

from typesafe_computer_use.calls import Calls, MeteredWriter
from typesafe_computer_use.config import writer_api, writer_base_url
from typesafe_computer_use.writer import WriterError, compose_answer, compose_url, make_writer, parse_json

ANSWER = '{"achieved": true, "answer": "Sep 19 in Miami.", "focus": "", "question": ""}'


@pytest.fixture
def openai_env(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_API", "openai")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", f"{endpoint.url}/v1")
    return clean_env


def writer():
    """Metered, as the runner hands it out."""
    return MeteredWriter(make_writer(), Calls())


def test_a_request_goes_to_chat_completions_with_the_schema_and_only_the_endpoint_key(openai_env, endpoint):
    openai_env.setenv("CLICKER_WRITER_API_KEY", "proxy-key")
    openai_env.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    openai_env.setenv("OPENAI_API_KEY", "sk-openai-real")

    assert compose_url(writer(), "open example", []) == "https://example.com"

    (request,) = endpoint.seen
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == "Bearer proxy-key"
    assert not any("real" in v for v in request["headers"].values())
    body = request["body"]
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert '"required": ["ok", "url", "reason"]' in body["messages"][0]["content"]  # json_object needs "JSON" in the prompt
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["ok", "url", "reason"]
    assert body["max_tokens"] == 200
    assert "thinking" not in body


def test_usage_comes_back_in_the_messages_shape_with_cached_tokens_apart(openai_env, endpoint):
    endpoint.state["usage"] = {
        "prompt_tokens": 1200,
        "completion_tokens": 30,
        "total_tokens": 1230,
        "prompt_tokens_details": {"cached_tokens": 1000},
    }
    calls = Calls()
    compose_url(MeteredWriter(make_writer(), calls), "open example", [])
    (usage,) = calls.usage.values()
    assert (usage.requests, usage.input_tokens, usage.cached_input_tokens, usage.output_tokens) == (1, 200, 1000, 30)


def test_an_endpoint_that_reports_no_usage_still_counts_the_request(openai_env, endpoint):
    calls = Calls()
    compose_url(MeteredWriter(make_writer(), calls), "open example", [])
    (usage,) = calls.usage.values()
    assert (usage.requests, usage.input_tokens, usage.cached_input_tokens, usage.output_tokens) == (1, 0, 0, 0)


def test_a_refused_format_steps_down_once_and_stays_down(openai_env, endpoint):
    endpoint.state["reject"] = lambda body: (
        "response_format json_schema is not supported" if body.get("response_format", {}).get("type") == "json_schema" else None
    )
    w = writer()
    assert compose_url(w, "open example", []) == "https://example.com"
    assert compose_url(w, "open example again", []) == "https://example.com"
    formats = [r["body"].get("response_format", {}).get("type") for r in endpoint.seen]
    assert formats == ["json_schema", "json_object", "json_object"]


def test_an_endpoint_with_no_json_mode_at_all_still_gets_the_schema_in_the_prompt(openai_env, endpoint):
    endpoint.state["reject"] = lambda body: "unknown parameter: response_format" if "response_format" in body else None
    endpoint.state["reply"] = 'Sure:\n```json\n{"ok": true, "url": "https://example.com", "reason": ""}\n```'
    assert compose_url(writer(), "open example", []) == "https://example.com"
    assert "response_format" not in endpoint.seen[-1]["body"]


def test_a_model_that_wants_max_completion_tokens_gets_them(openai_env, endpoint):
    endpoint.state["reject"] = lambda body: (
        "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead." if "max_tokens" in body else None
    )
    assert compose_url(writer(), "open example", []) == "https://example.com"
    assert endpoint.seen[-1]["body"]["max_completion_tokens"] == 200


def test_any_other_refusal_is_a_writer_error_and_not_a_retry_loop(openai_env, endpoint):
    endpoint.state["reject"] = lambda body: "model 'nope' not found"
    with pytest.raises(WriterError, match="not found"):
        compose_url(writer(), "open example", [])
    assert len(endpoint.seen) == 1


def test_the_answer_sends_the_capture_as_a_data_url(openai_env, endpoint, screen, make_item):
    endpoint.state["reply"] = ANSWER
    answer = compose_answer(writer(), "find the concert", screen, [make_item(0, "SEP 19")], [], "the goal is achieved")
    assert answer.text == "Sep 19 in Miami." and answer.achieved
    image, text = endpoint.seen[0]["body"]["messages"][1]["content"]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/png;base64,")
    assert json.loads(text["text"])["screen_text_in_reading_order"] == ["SEP 19"]


def test_a_text_only_model_gets_the_screen_text_and_no_capture(openai_env, endpoint, screen, make_item):
    openai_env.setenv("CLICKER_WRITER_VISION", "false")
    endpoint.state["reply"] = ANSWER
    compose_answer(writer(), "find the concert", screen, [make_item(0, "SEP 19")], [], "the goal is achieved")
    assert [part["type"] for part in endpoint.seen[0]["body"]["messages"][1]["content"]] == ["text"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://localhost:1234/v1", "http://localhost:1234/v1"),
        ("http://localhost:1234/v1/", "http://localhost:1234/v1"),
        ("http://localhost:1234/v1/chat/completions", "http://localhost:1234/v1"),
    ],
)
def test_the_openai_endpoint_keeps_its_v1(clean_env, given, expected):
    clean_env.setenv("CLICKER_WRITER_API", "openai")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", given)
    assert writer_base_url() == expected


def test_the_openai_api_needs_its_endpoint_named(clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "openai")
    clean_env.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")  # never read: it may point anywhere
    with pytest.raises(ValueError, match="needs CLICKER_WRITER_BASE_URL"):
        make_writer()


def test_an_unknown_api_is_named_in_the_error(clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "openai-compatible")
    with pytest.raises(ValueError, match="anthropic, openai"):
        writer_api()


@pytest.mark.parametrize(
    "reply",
    [
        '{"ok": false, "url": "https://a.example"',  # cut off before it closes
        "Not https://evil.example/x",  # prose is not a proposal
        "",
    ],
)
def test_nothing_is_guessed_out_of_a_reply_without_an_object(reply):
    with pytest.raises(WriterError):
        parse_json(reply)


def test_a_missing_flag_is_an_error_and_a_missing_string_is_empty(openai_env, endpoint):
    endpoint.state["reply"] = '{"url": "https://example.com"}'
    with pytest.raises(WriterError, match="no boolean 'ok'"):
        compose_url(writer(), "open example", [])
    endpoint.state["reply"] = '{"ok": true}'
    assert compose_url(writer(), "open example", []) == ""
