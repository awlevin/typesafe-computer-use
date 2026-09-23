"""Count the requests each model takes, at the two clients every request goes through."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

CLASSIFIER = "classifier"
WRITER = "writer"
MODELS = (CLASSIFIER, WRITER)


@dataclass
class Calls:
    """How many requests went to each model, and the seconds they took.

    The point of the design is that the classifier takes nearly all of them. This is the number
    that says whether it does: a run the writer has to steer at every turn shows up here.
    """

    count: dict[str, int] = field(default_factory=lambda: dict.fromkeys(MODELS, 0))
    seconds: dict[str, float] = field(default_factory=lambda: dict.fromkeys(MODELS, 0.0))

    @contextmanager
    def record(self, model: str) -> Iterator[None]:
        """Count one request to `model`. A request that fails was still made, so it still counts."""
        started = time.perf_counter()
        try:
            yield
        finally:
            self.count[model] += 1
            self.seconds[model] += time.perf_counter() - started

    def share(self, model: str) -> float | None:
        """The fraction of all requests that went to `model`, or None before any request."""
        total = sum(self.count.values())
        return self.count[model] / total if total else None

    def summary(self) -> dict:
        return {
            model: {
                "calls": self.count[model],
                "share": None if self.share(model) is None else round(self.share(model), 3),
                "seconds": round(self.seconds[model], 3),
            }
            for model in MODELS
        }

    def line(self) -> str:
        """One log line: `calls: classifier 12 (86%, 3.1s)  writer 2 (14%, 8.0s)`."""
        parts = [f"{model} {self.count[model]} ({(self.share(model) or 0):.0%}, {self.seconds[model]:.1f}s)" for model in MODELS]
        return "calls: " + "  ".join(parts)


class MeteredClassifier:
    """The TypeSafe client, counting each request. `system_one` is the only call the package makes."""

    def __init__(self, client, calls: Calls):
        self._client = client
        self._calls = calls

    def system_one(self, **request):
        with self._calls.record(CLASSIFIER):
            return self._client.system_one(**request)


class MeteredWriter:
    """The Anthropic client, counting each request. `messages.create` is the only call the package makes."""

    def __init__(self, client, calls: Calls):
        self._client = client
        self._calls = calls
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        with self._calls.record(WRITER):
            return self._client.messages.create(**request)

    def __getattr__(self, name: str):
        """What the writer says of itself (its model, whether it reads images) reads through the meter."""
        return getattr(self._client, name)
