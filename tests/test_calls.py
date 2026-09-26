import json
from types import SimpleNamespace

import pytest
from world import FakeWriter, Page, World, drive, scripted

from typesafe_computer_use.calls import Calls, MeteredClassifier, MeteredWriter, Usage


def test_no_request_yet_has_no_share():
    calls = Calls()

    assert calls.share("classifier") is None
    assert calls.summary()["writer"] == {"calls": 0, "share": None, "seconds": 0.0}
    assert calls.line() == "calls: classifier 0 (0%, 0.0s)  writer 0 (0%, 0.0s)"


def test_each_client_counts_its_own_requests_and_passes_them_through():
    calls = Calls()
    classifier = MeteredClassifier(SimpleNamespace(system_one=lambda **request: request), calls)
    writer = MeteredWriter(SimpleNamespace(messages=SimpleNamespace(create=lambda **request: request)), calls)

    for _ in range(3):
        assert classifier.system_one(state={}, questions={}) == {"state": {}, "questions": {}}
    assert writer.messages.create(model="m") == {"model": "m"}

    assert calls.count == {"classifier": 3, "writer": 1}
    assert calls.share("classifier") == 0.75
    assert calls.line().startswith("calls: classifier 3 (75%, ")
    # Neither reply reports usage or, for the classifier, a model: each is still a request, with zero tokens.
    assert calls.usage == {"classifier": Usage(requests=3), "m": Usage(requests=1)}


def test_a_request_that_fails_was_still_made():
    calls = Calls()

    def refuse(**request):
        raise RuntimeError("no")

    writer = MeteredWriter(SimpleNamespace(messages=SimpleNamespace(create=refuse)), calls)

    with pytest.raises(RuntimeError):
        writer.messages.create(model="m")
    assert calls.count["writer"] == 1
    assert calls.usage == {}  # no reply, so no tokens reported


def test_the_classifier_counts_tokens_under_the_model_its_reply_names():
    calls = Calls()
    replies = iter(
        [
            SimpleNamespace(model="ts-small", usage=SimpleNamespace(input_tokens=900, output_tokens=4)),
            SimpleNamespace(model="ts-small", usage=SimpleNamespace(input_tokens=None, output_tokens=None)),
            SimpleNamespace(model="ts-large", usage=SimpleNamespace(input_tokens=2000, output_tokens=6)),
        ]
    )
    classifier = MeteredClassifier(SimpleNamespace(system_one=lambda **request: next(replies)), calls)

    for _ in range(3):
        classifier.system_one(state={}, questions={})

    assert calls.usage == {
        "ts-small": Usage(requests=2, input_tokens=900, output_tokens=4),
        "ts-large": Usage(requests=1, input_tokens=2000, output_tokens=6),
    }
    assert calls.count == {"classifier": 3, "writer": 0}


def test_the_writer_counts_cache_reads_apart_and_cache_writes_as_uncached_input():
    calls = Calls()
    usage = SimpleNamespace(input_tokens=50, cache_creation_input_tokens=1000, cache_read_input_tokens=3000, output_tokens=80)
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **request: SimpleNamespace(usage=usage)))
    writer = MeteredWriter(client, calls)

    writer.messages.create(model="writer-model")
    writer.messages.create(model="answer-model")
    writer.messages.create(model="answer-model")

    assert calls.tokens() == {
        "writer-model": {"requests": 1, "input_tokens": 1050, "cached_input_tokens": 3000, "output_tokens": 80},
        "answer-model": {"requests": 2, "input_tokens": 2100, "cached_input_tokens": 6000, "output_tokens": 160},
    }


class CountingWriter(FakeWriter):
    """The scenario writer, with Anthropic usage on every reply."""

    def _create(self, **request):
        reply = super()._create(**request)
        reply.usage = SimpleNamespace(input_tokens=700, cache_read_input_tokens=300, output_tokens=40)
        return reply


def test_run_json_shows_tokens_per_model_and_the_calls_line_is_unchanged(monkeypatch, tmp_path):
    world = World([Page(name="done", items=["Order 4821"], url="https://example.com/done")])
    writer = CountingWriter()

    drive(world, scripted(("done", None)), monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer)

    run = json.loads((tmp_path / "run" / "run.json").read_text())
    (model,) = {request["model"] for request in writer.requests}
    assert run["usage"] == {
        "classifier": {"requests": 1, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        model: {"requests": len(writer.requests), "input_tokens": 700, "cached_input_tokens": 300, "output_tokens": 40},
    }
    assert run["calls"]["classifier"]["calls"] == 1
    log = (tmp_path / "run" / "run.log").read_text()
    assert "calls: classifier 1 (50%, " in log and "writer 1 (50%, " in log
