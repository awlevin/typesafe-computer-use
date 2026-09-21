import pytest

from typesafe_computer_use.structured_output import StructuredOutputError, decode_json_object

URL_PROPERTIES = {
    "ok": {"type": "boolean"},
    "url": {"type": "string"},
    "reason": {"type": "string"},
}


def test_decodes_plain_json():
    result = decode_json_object(
        '{"ok":true,"url":"https://gemini.google.com/","reason":"open"}',
        URL_PROPERTIES,
    )
    assert result == {"ok": True, "url": "https://gemini.google.com/", "reason": "open"}


def test_decodes_fenced_json():
    raw = '```json\n{"ok":true,"url":"https://gemini.google.com/","reason":"open"}\n```'
    assert decode_json_object(raw, URL_PROPERTIES)["url"] == "https://gemini.google.com/"


def test_decodes_json_surrounded_by_explanation():
    raw = 'Use this result: {"ok":true,"url":"https://gemini.google.com/","reason":"open"} Done.'
    assert decode_json_object(raw, URL_PROPERTIES)["ok"] is True


def test_empty_response_carries_the_raw_text():
    with pytest.raises(StructuredOutputError) as caught:
        decode_json_object("", URL_PROPERTIES)
    assert caught.value.raw == ""


def test_rejects_missing_required_property():
    with pytest.raises(StructuredOutputError):
        decode_json_object('{"ok":true,"url":"https://gemini.google.com/"}', URL_PROPERTIES)


def test_allows_missing_optional_property():
    result = decode_json_object(
        '{"ok":true,"url":"https://gemini.google.com/"}',
        URL_PROPERTIES,
        required=("ok", "url"),
    )

    assert result == {"ok": True, "url": "https://gemini.google.com/"}


def test_rejects_wrong_property_type():
    with pytest.raises(StructuredOutputError):
        decode_json_object(
            '{"ok":"yes","url":"https://gemini.google.com/","reason":"open"}',
            URL_PROPERTIES,
        )


def test_rejects_array_response():
    with pytest.raises(StructuredOutputError):
        decode_json_object("[]", URL_PROPERTIES)
