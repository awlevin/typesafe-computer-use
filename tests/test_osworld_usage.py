"""The Luna runner's token count, from Responses API replies as the SDK returns them or as dicts."""

import json
from types import SimpleNamespace

from typesafe_computer_use.osworld.usage import USAGE_FILE, Meter


def reply(input_tokens: int, cached: int, output: int, reasoning: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached),
            output_tokens=output,
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        )
    )


def test_replies_add_up_with_cached_input_counted_apart():
    meter = Meter("gpt-6-luna")
    meter.add(reply(1000, 0, 300, 200), 2.0)
    meter.add(reply(1500, 1000, 100, 60), 1.5)
    assert (meter.requests, meter.input_tokens, meter.cached_input_tokens) == (2, 1500, 1000)
    assert (meter.output_tokens, meter.reasoning_tokens, meter.seconds) == (400, 260, 3.5)


def test_a_dict_reply_counts_the_same():
    meter = Meter("gpt-6-luna")
    meter.add({"usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 4}, "output_tokens": 3}}, 0.1)
    assert (meter.input_tokens, meter.cached_input_tokens, meter.output_tokens, meter.reasoning_tokens) == (6, 4, 3, 0)


def test_a_reply_without_usage_is_still_a_request():
    meter = Meter("gpt-6-luna")
    meter.add(SimpleNamespace(usage=None), 0.5)
    assert (meter.requests, meter.input_tokens, meter.output_tokens) == (1, 0, 0)


def test_usage_json_has_run_jsons_shape_and_the_settings(tmp_path):
    meter = Meter("gpt-6-luna", settings={"reasoning_effort": "xhigh"})
    meter.add(reply(1000, 200, 50, 20), 1.25)
    path = meter.write(tmp_path)
    assert path == tmp_path / USAGE_FILE
    assert json.loads(path.read_text()) == {
        "usage": {
            "gpt-6-luna": {
                "requests": 1,
                "input_tokens": 800,
                "cached_input_tokens": 200,
                "output_tokens": 50,
                "reasoning_tokens": 20,
            }
        },
        "seconds": 1.25,
        "settings": {"reasoning_effort": "xhigh"},
    }
