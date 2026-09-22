from types import SimpleNamespace

import pytest

from typesafe_computer_use.calls import Calls, MeteredClassifier, MeteredWriter


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


def test_a_request_that_fails_was_still_made():
    calls = Calls()

    def refuse(**request):
        raise RuntimeError("no")

    writer = MeteredWriter(SimpleNamespace(messages=SimpleNamespace(create=refuse)), calls)

    with pytest.raises(RuntimeError):
        writer.messages.create(model="m")
    assert calls.count["writer"] == 1
