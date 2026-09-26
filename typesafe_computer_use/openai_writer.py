"""The writer's requests, sent to an endpoint that speaks OpenAI's Chat Completions API instead."""

from __future__ import annotations

from types import SimpleNamespace

import openai

# Most specific first. An endpoint that refuses one is asked with the next, for the rest of the run;
# the schema is in the prompt as well, so even no format at all still gets JSON back.
RESPONSE_FORMATS = ("json_schema", "json_object", "none")
FORMAT_WORDS = ("response_format", "json_schema", "json_object")


class OpenAIWriter:
    """An OpenAI-compatible client behind the one call the writer makes, `messages.create`.

    writer.py builds each request as an Anthropic Messages call and reads text blocks back, so this
    translates both ways, and nothing else in the package knows which API is on the other end.
    `thinking` is accepted and dropped: Chat Completions has no such switch.
    """

    def __init__(self, base_url: str, api_key: str):
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key)
        self.base_url = self._client.base_url
        self.messages = SimpleNamespace(create=self._create)
        self._formats = list(RESPONSE_FORMATS)  # those this endpoint has not refused yet
        self._token_limit = "max_tokens"  # OpenAI's newer models take max_completion_tokens instead

    def _create(self, *, model: str, max_tokens: int, system: str, messages: list[dict], output_config=None, thinking=None):
        chat = [{"role": "system", "content": system}, *(_chat_message(m) for m in messages)]
        schema = output_config["format"]["schema"] if output_config else None
        while True:
            try:
                reply = self._client.chat.completions.create(
                    model=model,
                    messages=chat,
                    **{self._token_limit: max_tokens},
                    **_response_format(self._formats[0], schema),
                )
            except openai.BadRequestError as e:
                said = str(e).lower()
                if self._token_limit == "max_tokens" and "max_completion_tokens" in said:
                    self._token_limit = "max_completion_tokens"
                    continue
                if len(self._formats) > 1 and any(word in said for word in FORMAT_WORDS):
                    self._formats.pop(0)
                    continue
                raise
            text = reply.choices[0].message.content if reply.choices else None
            usage = _usage(getattr(reply, "usage", None))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text or "")], usage=usage)


def _usage(usage) -> SimpleNamespace | None:
    """Chat Completions usage in the Messages API's shape: `prompt_tokens` counts the cached ones too."""
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None) or 0
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None) or 0
    return SimpleNamespace(
        input_tokens=max(prompt - cached, 0),
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
        output_tokens=getattr(usage, "completion_tokens", None) or 0,
    )


def _response_format(kind: str, schema: dict | None) -> dict:
    if kind == "json_schema" and schema is not None:
        return {"response_format": {"type": "json_schema", "json_schema": {"name": "reply", "strict": True, "schema": schema}}}
    if kind in ("json_schema", "json_object"):
        return {"response_format": {"type": "json_object"}}
    return {}


def _chat_message(message: dict) -> dict:
    """One Messages turn as a Chat Completions one: text parts stay text, a base64 image becomes a data URL."""
    content = message["content"]
    if isinstance(content, str):
        return {"role": message["role"], "content": content}
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image":
            source = block["source"]
            parts.append({"type": "image_url", "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"}})
        else:
            raise ValueError(f"no Chat Completions form for a {block['type']!r} block")
    return {"role": message["role"], "content": parts}
