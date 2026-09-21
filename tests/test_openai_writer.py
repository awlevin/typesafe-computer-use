from types import SimpleNamespace

import pytest
from PIL import Image

from typesafe_computer_use import openai_writer
from typesafe_computer_use.openai_writer import OpenAICompatibleWriterBackend
from typesafe_computer_use.writer_backend import StructuredRequest, WriterResponseError


class FakeAPIError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class FakeCompletions:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response))])


class FakeClient:
    def __init__(self, *responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(*responses))


def response(content: str):
    return content


def request(image=None):
    return StructuredRequest(
        system="Return the result.",
        packet={"goal": "open Gemini"},
        properties={"ok": {"type": "boolean"}},
        max_tokens=32,
        model="deepseek-v4.1-flash",
        image=image,
    )


def backend(monkeypatch, client, **kwargs):
    monkeypatch.setattr(openai_writer, "OpenAI", lambda **_: client)
    return OpenAICompatibleWriterBackend(api_key="secret", base_url="http://proxy/v1", **kwargs)


def test_auto_mode_uses_json_schema_first(monkeypatch):
    client = FakeClient(response('{"ok":true}'))
    writer = backend(monkeypatch, client)

    assert writer.generate(request()) == '{"ok":true}'
    assert client.chat.completions.requests[0]["response_format"]["type"] == "json_schema"


def test_auto_mode_falls_back_to_json_object_after_unsupported_format(monkeypatch):
    client = FakeClient(FakeAPIError("response_format json_schema is not supported"), response('{"ok":true}'))
    writer = backend(monkeypatch, client)

    assert writer.generate(request()) == '{"ok":true}'
    formats = [call["response_format"]["type"] for call in client.chat.completions.requests]
    assert formats == ["json_schema", "json_object"]


def test_auto_mode_falls_back_to_prompt_only(monkeypatch):
    client = FakeClient(
        FakeAPIError("unsupported response_format json_schema"),
        FakeAPIError("unsupported json_object parameter"),
        response('{"ok":true}'),
    )
    writer = backend(monkeypatch, client)

    assert writer.generate(request()) == '{"ok":true}'
    assert "response_format" not in client.chat.completions.requests[2]
    prompt = client.chat.completions.requests[2]["messages"][0]["content"]
    assert "Return only one JSON object matching the supplied schema" in prompt
    assert '"properties": {"ok": {"type": "boolean"}}' in prompt
    assert '"required": ["ok"]' in prompt


def test_auto_mode_falls_back_after_invalid_structured_content(monkeypatch):
    client = FakeClient("false", response('{"ok":true}'))
    writer = backend(monkeypatch, client)

    assert writer.generate(request()) == '{"ok":true}'
    formats = [call["response_format"]["type"] for call in client.chat.completions.requests]
    assert formats == ["json_schema", "json_object"]
    assert writer.resolved_structured_mode == "json_object"


def test_cached_mode_is_invalidated_when_later_content_is_invalid(monkeypatch):
    client = FakeClient(response('{"ok":true}'), "false", response('{"ok":true}'))
    writer = backend(monkeypatch, client)

    assert writer.generate(request()) == '{"ok":true}'
    assert writer.generate(request()) == '{"ok":true}'

    formats = [call["response_format"]["type"] for call in client.chat.completions.requests]
    assert formats == ["json_schema", "json_schema", "json_object"]
    assert writer.resolved_structured_mode == "json_object"


def test_successful_mode_is_cached(monkeypatch):
    client = FakeClient(response('{"ok":true}'), response('{"ok":true}'))
    writer = backend(monkeypatch, client)

    writer.generate(request())
    writer.generate(request())

    assert writer.resolved_structured_mode == "json_schema"
    assert len(client.chat.completions.requests) == 2
    assert all(call["response_format"]["type"] == "json_schema" for call in client.chat.completions.requests)


def test_sends_image_by_default(monkeypatch):
    client = FakeClient(response('{"ok":true}'))
    writer = backend(monkeypatch, client)

    writer.generate(request(Image.new("RGB", (2, 2), "white")))

    content = client.chat.completions.requests[0]["messages"][1]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_confirmed_vision_error_retries_without_image(monkeypatch):
    client = FakeClient(FakeAPIError("image_url is not supported"), response('{"ok":true}'))
    writer = backend(monkeypatch, client)

    assert writer.generate(request(Image.new("RGB", (2, 2), "white"))) == '{"ok":true}'
    assert writer.vision_enabled is False
    assert len(client.chat.completions.requests[0]["messages"][1]["content"]) == 2
    assert client.chat.completions.requests[1]["messages"][1]["content"][0]["type"] == "text"


def test_non_vision_bad_request_is_not_silenced(monkeypatch):
    error = FakeAPIError("invalid model parameter")
    client = FakeClient(error)
    writer = backend(monkeypatch, client)

    with pytest.raises(FakeAPIError):
        writer.generate(request(Image.new("RGB", (2, 2), "white")))
    assert writer.vision_enabled is True
    assert len(client.chat.completions.requests) == 1


def test_one_empty_response_is_retried(monkeypatch):
    client = FakeClient("", response('{"ok":true}'))
    writer = backend(monkeypatch, client)

    assert writer.generate(request()) == '{"ok":true}'
    assert len(client.chat.completions.requests) == 2


def test_two_empty_responses_raise_writer_response_error(monkeypatch):
    client = FakeClient("", "")
    writer = backend(monkeypatch, client)

    with pytest.raises(WriterResponseError, match="writer returned empty content twice"):
        writer.generate(request())
